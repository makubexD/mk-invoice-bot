"""Integration tests for flow B's review before send (D194, D88, D89).

The folder is an in-memory fake drive and the automatic mode a fake that records its sends;
Postgres is real. A mailer on ``send: review`` posts the mail to the chat until ``reviewed``
is set in the flow data.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import InboundMessage
from invoicebot.channels.buttons import button_id
from invoicebot.config.loader import load_config
from invoicebot.config.models import (
    Brand,
    Config,
    ExcelTemplateSink,
    MailerSink,
    MediaMaxBytes,
    Store,
    StoreDirectory,
)
from invoicebot.extract.fields import Field, Reading
from invoicebot.pipeline.deliver import DELIVER_STEP, DeliverContext, make_deliver_handler
from invoicebot.pipeline.handlers import MEGABYTE
from invoicebot.pipeline.intake import IntakeRoles, handle_inbound
from invoicebot.pipeline.review import mail_digest, remove_review_files, write_review_files
from invoicebot.queue.jobs import Job
from invoicebot.queue.outbox import add, has_pending_document
from invoicebot.queue.worker import Handler
from invoicebot.sinks.base import Attachment, OutgoingMail
from invoicebot.sinks.mailer import DryRun
from invoicebot.sinks.onedrive import GraphError
from invoicebot.store.flow_b import get_batch_state, load_flow_data, merge_flow_data, open_batch
from invoicebot.store.invoices import (
    FileRecord,
    add_file,
    create_invoice,
    get_file_path,
    save_reading,
    set_flow,
    set_state,
)
from xlsx_factory import build_template

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
FLOW = "work_microsoft"
PROFILE = "yo"
# From tests/synthetic_rucs.txt; no real RUC appears here.
STORE_RUC = "20000000001"
COMERCIO = "LaLena"
SERIE = "F001"
NUMERO = "15160"
FECHA = date(2026, 11, 5)
PDF_BYTES = b"%PDF-1.4 synthetic invoice\n"
XML_BYTES = b'<?xml version="1.0" encoding="UTF-8"?>\n<Invoice/>\n'
FAKE_FOLDER_URL = "https://onedrive.live.com/?id=FAKE"
FOLDER_LINKS = [{"title": "📂 Abrir carpeta", "url": FAKE_FOLDER_URL}]
MAX_QUEUED = 10
MEDIA_MAX_BYTES = MediaMaxBytes(image=10 * MEGABYTE, pdf=10 * MEGABYTE, xml=MEGABYTE, zip=10 * MEGABYTE)
TIMEZONE = "America/Lima"


class FakeDrive:
    """An in-memory drive: folders hold their entry names and uploads keep their bytes."""

    def __init__(self, on_upload: Callable[[], Awaitable[None]] | None = None) -> None:
        self.folders: dict[str, list[str]] = {}
        self.uploads: dict[str, bytes] = {}
        self._on_upload = on_upload

    async def list_names(self, path: str) -> list[str]:
        """Return the entries of a folder; a path that does not exist has none."""
        return list(self.folders.get(path, []))

    async def create_folder(self, parent: str, name: str) -> None:
        """List the new folder under its parent."""
        self.folders.setdefault(parent, []).append(name)

    async def upload(self, path: str, data: bytes) -> None:
        """Keep the file's bytes at its absolute path, then run the installed hook."""
        self.uploads[path] = data
        if self._on_upload is not None:
            await self._on_upload()

    async def move(self, path: str, new_parent: str, new_name: str) -> None:
        """Accept the move; the fake does not change its folders."""

    async def web_url(self, path: str) -> str:
        """Return the fake folder link (D194)."""
        return FAKE_FOLDER_URL


class FakeGraphMode:
    """An automatic mode that records each send instead of reaching Graph (D89)."""

    completion = "accepted"
    done_rule = "BR-D10"

    def __init__(self) -> None:
        self.calls: list[tuple[int, OutgoingMail]] = []

    async def deliver(self, invoice_id: int, mail: OutgoingMail, _folder_path: str) -> None:
        """Record the send."""
        self.calls.append((invoice_id, mail))


class FailingGraphMode:
    """An automatic mode whose send fails for good, as Graph does for a bad request (BR-D03)."""

    completion = "accepted"
    done_rule = "BR-D10"

    async def deliver(self, _invoice_id: int, _mail: OutgoingMail, _folder_path: str) -> None:
        """Fail the send."""
        raise GraphError(400)


class SlowGraphMode(FakeGraphMode):
    """An automatic mode whose send takes a moment, so a second job can overlap it (D89)."""

    async def deliver(self, invoice_id: int, mail: OutgoingMail, _folder_path: str) -> None:
        """Wait, then record the send."""
        await asyncio.sleep(0.2)
        await super().deliver(invoice_id, mail, _folder_path)


def _field(value: str) -> Field:
    return Field(value=value, confidence=0.99, source="ocr")


def _reading() -> Reading:
    """Build a complete synthetic reading for a flow B invoice."""
    return Reading(
        fields={
            "ruc_emisor": _field(STORE_RUC),
            "tipo": _field("01"),
            "serie": _field(SERIE),
            "numero": _field(NUMERO),
            "total": _field("51.61"),
            "fecha_emision": _field(FECHA.isoformat()),
        },
        document_url=None,
    )


def _directory() -> StoreDirectory:
    """Return a store directory with one store whose only brand is COMERCIO."""
    store = Store(
        ruc=STORE_RUC,
        razon_social="Comercio Sintetico",
        tipo="01",
        brands=[Brand(comercio=COMERCIO)],
        portal=None,
    )
    return StoreDirectory(stores={STORE_RUC: store}, portals={})


def _config(tmp_path: Path, profile_id: str, send: str, mode_name: str = "graph") -> Config:
    """Return example.yaml with a synthetic template, one mail mode and the given ``send``."""
    cfg = load_config(EXAMPLE)
    template = build_template()
    template_path = tmp_path / "template.xlsx"
    template_path.write_bytes(template)
    sinks: list[Any] = []
    for sink in cfg.flows[FLOW].sinks:
        if isinstance(sink, ExcelTemplateSink):
            sink = sink.model_copy(
                update={"template": template_path, "template_sha256": hashlib.sha256(template).hexdigest()}
            )
        elif isinstance(sink, MailerSink):
            sink = sink.model_copy(update={"modes": [mode_name], "send": send})
        sinks.append(sink)
    flow = cfg.flows[FLOW].model_copy(update={"sinks": sinks})
    profile = cfg.profiles[PROFILE].model_copy(update={"admin": False})
    return cfg.model_copy(update={"flows": {**cfg.flows, FLOW: flow}, "profiles": {profile_id: profile}})


def _handler(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    cfg: Config,
    data_dir: Path,
    mode: Any,
    mode_name: str = "graph",
    drive: FakeDrive | None = None,
) -> Handler:
    drive = drive or FakeDrive()
    return make_deliver_handler(
        DeliverContext(
            pool, cfg, _directory(), drive, drive, {mode_name: mode}, data_dir, laptop_flows=frozenset()
        )
    )


def _job(invoice_id: int) -> Job:
    return Job(
        id=1, invoice_id=invoice_id, step=DELIVER_STEP, attempts=0, crashes=0, lease_until=datetime.now(UTC)
    )


async def _delivering_invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    profile_id: str,
    flow_data: dict[str, Any] | None = None,
) -> int:
    """Create a DELIVERING invoice with a reading, a PDF and an XML, and return its id."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "telegram")
        await save_reading(conn, invoice_id, _reading())
        await set_flow(conn, invoice_id, FLOW)
        await set_state(conn, invoice_id, "DELIVERING")
        for kind, data in (("pdf", PDF_BYTES), ("xml", XML_BYTES)):
            path = tmp_path / f"{uuid4().hex}.{kind}"
            path.write_bytes(data)
            record = FileRecord(
                kind=kind,
                local_path=str(path),
                remote_ref=None,
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
            )
            await add_file(conn, invoice_id, record)
        if flow_data:
            await merge_flow_data(conn, invoice_id, flow_data)
    return invoice_id


async def _invoice_state(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int) -> tuple[str, Any]:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT state, rule_id FROM invoices WHERE id = %s", (invoice_id,))
        row = await cur.fetchone()
    assert row is not None
    return str(row[0]), row[1]


async def _outbox(pool: AsyncConnectionPool[AsyncConnection[Any]], profile_id: str) -> list[dict[str, Any]]:
    """Return the payloads queued for the profile, in insertion order."""
    async with pool.connection() as conn:
        cur = await conn.execute('SELECT payload_json FROM outbox WHERE "to" = %s ORDER BY id', (profile_id,))
        return [payload for (payload,) in await cur.fetchall()]


async def _batch_state(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int) -> str | None:
    async with pool.connection() as conn:
        return await get_batch_state(conn, invoice_id)


async def test_a_review_flow_posts_the_mail_and_sends_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D194: a review flow queues the attachments, then BR-D19 with both buttons and the folder link."""
    profile_id = f"review-{uuid4().hex}"
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    mode = FakeGraphMode()

    await _handler(db_pool, _config(tmp_path, profile_id, "review"), tmp_path, mode)(_job(invoice_id))

    outbox = await _outbox(db_pool, profile_id)
    documents, review = outbox[:-1], outbox[-1]
    names = [payload["document"]["filename"] for payload in documents]
    assert mode.calls == []
    assert await _invoice_state(db_pool, invoice_id) == ("BATCHED", "BR-D19")
    assert await _batch_state(db_pool, invoice_id) == "review"
    assert len(names) == 3
    assert names[0].endswith(".xlsx")
    assert names[1].endswith(".pdf")
    assert names[2].endswith(".xml")
    assert [payload["text"] for payload in documents] == names
    assert all(payload["rule_id"] == "BR-D19" for payload in outbox)
    assert review["text"].startswith(f"📧 Revisa el correo de {SERIE}-{NUMERO}")
    for name in names:
        assert name in review["text"]
    assert "Para: " in review["text"]
    assert "Adjunto los documentos solicitados para el reembolso." in review["text"]
    assert review["buttons"] == [
        {"id": button_id(invoice_id, "send"), "title": "Enviar"},
        {"id": button_id(invoice_id, "hold"), "title": "Cancelar"},
    ]
    assert review["links"] == FOLDER_LINKS
    mail_dir = tmp_path / "mail" / str(invoice_id)
    assert (mail_dir / names[1]).read_bytes() == PDF_BYTES
    assert (mail_dir / names[2]).read_bytes() == XML_BYTES
    assert (mail_dir / names[0]).stat().st_size > 0


async def test_a_retried_review_job_queues_nothing_more(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D194: a retry of the same job finds the invoice BATCHED and leaves the outbox as it is."""
    profile_id = f"review-{uuid4().hex}"
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    mode = FakeGraphMode()
    handler = _handler(db_pool, _config(tmp_path, profile_id, "review"), tmp_path, mode)

    await handler(_job(invoice_id))
    queued = len(await _outbox(db_pool, profile_id))
    await handler(_job(invoice_id))

    assert len(await _outbox(db_pool, profile_id)) == queued
    assert mode.calls == []


@pytest.mark.parametrize(
    ("send", "reviewed"),
    [("review", True), ("auto", False)],
    ids=["reviewed", "auto"],
)
async def test_a_reviewed_or_auto_flow_sends_once_with_br_d10(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    send: str,
    reviewed: bool,
) -> None:
    """D194: a reviewed mail, like an auto flow's, is sent once and confirmed by BR-D10 alone."""
    profile_id = f"review-{uuid4().hex}"
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    mode = FakeGraphMode()
    handler = _handler(db_pool, _config(tmp_path, profile_id, send), tmp_path, mode)
    if reviewed:
        await _post_review_then_press_send(db_pool, handler, invoice_id)
        queued = len(await _outbox(db_pool, profile_id))
    else:
        queued = 0

    await handler(_job(invoice_id))

    outbox = (await _outbox(db_pool, profile_id))[queued:]
    assert len(mode.calls) == 1
    assert await _invoice_state(db_pool, invoice_id) == ("DONE", "BR-D10")
    assert [payload["rule_id"] for payload in outbox] == ["BR-D10"]
    assert outbox[0].get("document") is None


async def test_a_sending_batch_with_reviewed_fails_with_br_d03(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D194, D89: a reviewed mail whose batch was left sending is never sent; BR-D03 asks again."""
    profile_id = f"review-{uuid4().hex}"
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id, {"reviewed": True})
    async with db_pool.connection() as conn, conn.transaction():
        await open_batch(conn, invoice_id, FLOW, "sending")
    mode = FakeGraphMode()

    await _handler(db_pool, _config(tmp_path, profile_id, "review"), tmp_path, mode)(_job(invoice_id))

    assert mode.calls == []
    assert await _invoice_state(db_pool, invoice_id) == ("BATCHED", "BR-D03")
    assert await _batch_state(db_pool, invoice_id) == "failed"


async def _add_photo(
    pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int, tmp_path: Path
) -> Path:
    """Store a synthetic photo file for the invoice and return its local path."""
    photo = tmp_path / f"{uuid4().hex}.jpg"
    photo.write_bytes(b"\xff\xd8\xff synthetic photo\n")
    record = FileRecord(
        kind="photo",
        local_path=str(photo),
        remote_ref=None,
        sha256=hashlib.sha256(photo.read_bytes()).hexdigest(),
        size=photo.stat().st_size,
    )
    async with pool.connection() as conn, conn.transaction():
        await add_file(conn, invoice_id, record)
    return photo


async def _post_review_then_press_send(
    pool: AsyncConnectionPool[AsyncConnection[Any]], handler: Handler, invoice_id: int
) -> None:
    """Post the review, then do what [Enviar] does to the flow data and the state (D194)."""
    await handler(_job(invoice_id))
    await _press_send(pool, invoice_id)


async def _press_send(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int) -> None:
    """Do what [Enviar] does to the flow data and the state (D194)."""
    async with pool.connection() as conn, conn.transaction():
        await merge_flow_data(conn, invoice_id, {"reviewed": True})
        await set_state(conn, invoice_id, "DELIVERING")


async def _mark_documents(
    pool: AsyncConnectionPool[AsyncConnection[Any]], profile_id: str, state: str
) -> None:
    """Set the state of the profile's queued document rows, as the sender would (D195)."""
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE outbox SET state = %s WHERE \"to\" = %s AND payload_json->>'document' IS NOT NULL",
            (state, profile_id),
        )


async def _review_then_send(
    pool: AsyncConnectionPool[AsyncConnection[Any]], handler: Handler, invoice_id: int, profile_id: str
) -> None:
    """Post the review, mark its documents sent, press [Enviar] and deliver again (D194, D195)."""
    await handler(_job(invoice_id))
    await _mark_documents(pool, profile_id, "sent")
    await _press_send(pool, invoice_id)
    await handler(_job(invoice_id))


async def test_a_reviewed_graph_send_removes_the_review_files_and_the_photo(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D194: once the reviewed mail is sent, mail/<id>/ and the photo are gone."""
    profile_id = f"review-{uuid4().hex}"
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    photo = await _add_photo(db_pool, invoice_id, tmp_path)
    mode = FakeGraphMode()
    handler = _handler(db_pool, _config(tmp_path, profile_id, "review"), tmp_path, mode)

    await _review_then_send(db_pool, handler, invoice_id, profile_id)

    async with db_pool.connection() as conn:
        photo_path = await get_file_path(conn, invoice_id, "photo")
    assert len(mode.calls) == 1
    assert not (tmp_path / "mail" / str(invoice_id)).exists()
    assert photo_path is None
    assert not photo.exists()


async def test_a_reviewed_dry_run_draft_removes_the_review_files(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D194: once the reviewed mail's .eml draft is written, mail/<id>/ is gone; the draft holds the files."""
    profile_id = f"review-{uuid4().hex}"
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    cfg = _config(tmp_path, profile_id, "review", mode_name="dry_run")
    handler = _handler(db_pool, cfg, tmp_path, DryRun(tmp_path), mode_name="dry_run")

    await _review_then_send(db_pool, handler, invoice_id, profile_id)

    assert await _invoice_state(db_pool, invoice_id) == ("BATCHED", "BR-D07")
    assert not (tmp_path / "mail" / str(invoice_id)).exists()


async def test_enviar_with_a_pending_review_document_keeps_the_review_files(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D195: [Enviar] sends the mail but leaves mail/<id>/ while a review document row is pending."""
    profile_id = f"review-{uuid4().hex}"
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    mode = FakeGraphMode()
    handler = _handler(db_pool, _config(tmp_path, profile_id, "review"), tmp_path, mode)

    await _post_review_then_press_send(db_pool, handler, invoice_id)
    await handler(_job(invoice_id))

    assert len(mode.calls) == 1
    assert (tmp_path / "mail" / str(invoice_id)).is_dir()


async def test_has_pending_document_sees_only_this_invoices_pending_documents(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D195: text rows, other invoices' rows and sent rows give false; a pending document row gives true."""
    profile_id = f"review-{uuid4().hex}"
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    other_id = await _delivering_invoice(db_pool, tmp_path, f"review-{uuid4().hex}")
    document = {"text": "x", "rule_id": "BR-D19", "document": {"filename": "a.pdf", "path": "a.pdf"}}
    async with db_pool.connection() as conn:
        await add(conn, "telegram", profile_id, {"text": "hola", "document": None}, invoice_id=invoice_id)
        await add(conn, "telegram", profile_id, document, invoice_id=other_id)
        sent_id = await add(conn, "telegram", profile_id, document, invoice_id=invoice_id)
        await conn.execute("UPDATE outbox SET state = 'sent' WHERE id = %s", (sent_id,))
        await add(conn, "telegram", profile_id, {**document, "rule_id": "BR-D07"}, invoice_id=invoice_id)
        before = await has_pending_document(conn, invoice_id, "BR-D19")
        await add(conn, "telegram", profile_id, document, invoice_id=invoice_id)
        after = await has_pending_document(conn, invoice_id, "BR-D19")

    assert before is False
    assert after is True


async def test_removing_absent_review_files_raises_nothing(tmp_path: Path) -> None:
    """D194: an invoice with no review directory is accepted."""
    await remove_review_files(tmp_path, 123456)

    assert not (tmp_path / "mail").exists()


def _press(invoice_id: int, action: str, profile_id: str) -> InboundMessage:
    """Build the canonical press of one of the invoice's buttons."""
    return InboundMessage(
        channel="telegram",
        external_id=f"ext-{uuid4().hex}",
        profile_id=profile_id,
        sender_role="owner",
        button_id=button_id(invoice_id, action),
        received_at=datetime.now(UTC),
    )


def _command(profile_id: str, text: str) -> InboundMessage:
    """Build the canonical text message ``text`` from the profile."""
    return InboundMessage(
        channel="telegram",
        external_id=f"ext-{uuid4().hex}",
        profile_id=profile_id,
        sender_role="owner",
        text=text,
        attachments=[],
        received_at=datetime.now(UTC),
    )


async def _intake(
    pool: AsyncConnectionPool[AsyncConnection[Any]], msg: InboundMessage, roles: IntakeRoles | None = None
) -> None:
    """Handle ``msg`` through the real intake, as the channel's webhook does."""
    async with pool.connection() as conn:
        await handle_inbound(
            conn,
            msg,
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
            roles=roles or IntakeRoles(reprocess_min_interval_s=0),
        )


async def _jobs(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int) -> list[str]:
    """Return the steps of the invoice's queued jobs, in insertion order."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT step FROM jobs WHERE invoice_id = %s ORDER BY id", (invoice_id,))
        return [str(step) for (step,) in await cur.fetchall()]


async def _flow_data(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int) -> dict[str, Any]:
    async with pool.connection() as conn:
        return await load_flow_data(conn, invoice_id)


async def test_a_sending_batch_without_reviewed_fails_with_br_d03(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D194, D89: a batch left sending is never posted for review or sent; BR-D03 asks again."""
    profile_id = f"review-{uuid4().hex}"
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    async with db_pool.connection() as conn, conn.transaction():
        await open_batch(conn, invoice_id, FLOW, "sending")
    mode = FakeGraphMode()

    await _handler(db_pool, _config(tmp_path, profile_id, "review"), tmp_path, mode)(_job(invoice_id))

    outbox = await _outbox(db_pool, profile_id)
    assert mode.calls == []
    assert await _invoice_state(db_pool, invoice_id) == ("BATCHED", "BR-D03")
    assert await _batch_state(db_pool, invoice_id) == "failed"
    assert [payload["rule_id"] for payload in outbox] == ["BR-D03"]


async def test_a_review_for_an_invoice_that_left_delivering_posts_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D194: an invoice moved on while its mail was built gets no review and no batch."""
    profile_id = f"review-{uuid4().hex}"
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)

    async def move_on() -> None:
        async with db_pool.connection() as conn, conn.transaction():
            await set_state(conn, invoice_id, "BATCHED")

    mode = FakeGraphMode()
    handler = _handler(
        db_pool, _config(tmp_path, profile_id, "review"), tmp_path, mode, drive=FakeDrive(on_upload=move_on)
    )

    await handler(_job(invoice_id))

    assert mode.calls == []
    assert await _outbox(db_pool, profile_id) == []
    assert await _batch_state(db_pool, invoice_id) is None
    assert (await _invoice_state(db_pool, invoice_id))[0] == "BATCHED"


@pytest.mark.parametrize("name", ["../escape.pdf", "sub/inside.pdf", "/abs.pdf"])
async def test_a_review_attachment_name_that_leaves_its_directory_raises(tmp_path: Path, name: str) -> None:
    """D194, R-9.2: an attachment name that would land outside the review directory is refused."""
    attachment = Attachment(name, b"data", "application/pdf")

    with pytest.raises(ValueError, match="must not leave its directory"):
        await write_review_files(tmp_path, 7, [attachment])

    assert not (tmp_path / "escape.pdf").exists()


@pytest.mark.parametrize(
    "change",
    ["to", "cc", "subject", "body", "attachment_name", "attachment_bytes", "attachment_added"],
)
async def test_the_mail_digest_changes_with_every_part_of_the_mail(change: str) -> None:
    """D194: the digest of the reviewed mail differs when any part of the mail differs."""
    mail = OutgoingMail(
        to=("a@example.test",),
        cc=("b@example.test",),
        subject="Asunto",
        body="Cuerpo",
        attachments=(Attachment("a.pdf", b"one", "application/pdf"),),
    )
    changed = {
        "to": replace(mail, to=("c@example.test",)),
        "cc": replace(mail, cc=()),
        "subject": replace(mail, subject="Otro"),
        "body": replace(mail, body="Otro"),
        "attachment_name": replace(mail, attachments=(Attachment("b.pdf", b"one", "application/pdf"),)),
        "attachment_bytes": replace(mail, attachments=(Attachment("a.pdf", b"two", "application/pdf"),)),
        "attachment_added": replace(
            mail, attachments=(*mail.attachments, Attachment("c.pdf", b"x", "application/pdf"))
        ),
    }[change]

    assert mail_digest(mail) == mail_digest(replace(mail))
    assert mail_digest(changed) != mail_digest(mail)


async def test_the_real_send_press_runs_the_delivery_and_clears_reviewed(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D194, BR-D21: the real [Enviar] queues deliver, which sends the reviewed mail and clears reviewed."""
    profile_id = f"review-{uuid4().hex}"
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    mode = FakeGraphMode()
    handler = _handler(db_pool, _config(tmp_path, profile_id, "review"), tmp_path, mode)
    await handler(_job(invoice_id))
    await _mark_documents(db_pool, profile_id, "sent")
    posted = await _flow_data(db_pool, invoice_id)
    assert len(posted["review_digest"]) == 64
    assert posted["reviewed"] is False

    await _intake(db_pool, _press(invoice_id, "send", profile_id))
    assert (await _flow_data(db_pool, invoice_id))["reviewed"] is True
    assert await _jobs(db_pool, invoice_id) == ["deliver"]
    await handler(_job(invoice_id))

    assert len(mode.calls) == 1
    assert await _invoice_state(db_pool, invoice_id) == ("DONE", "BR-D10")
    assert (await _flow_data(db_pool, invoice_id))["reviewed"] is False
    assert not (tmp_path / "mail" / str(invoice_id)).exists()


async def test_a_mail_that_changed_since_the_review_is_posted_again_and_not_sent(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D194: [Enviar] on a mail whose attachments no longer match the reviewed ones posts a new review."""
    profile_id = f"review-{uuid4().hex}"
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    mode = FakeGraphMode()
    handler = _handler(db_pool, _config(tmp_path, profile_id, "review"), tmp_path, mode)
    await handler(_job(invoice_id))
    await _intake(db_pool, _press(invoice_id, "send", profile_id))
    async with db_pool.connection() as conn:
        pdf_path = await get_file_path(conn, invoice_id, "pdf")
    assert pdf_path is not None
    await asyncio.to_thread(Path(pdf_path).write_bytes, b"%PDF-1.4 a different file\n")
    queued = len(await _outbox(db_pool, profile_id))

    await handler(_job(invoice_id))

    outbox = (await _outbox(db_pool, profile_id))[queued:]
    assert mode.calls == []
    assert await _invoice_state(db_pool, invoice_id) == ("BATCHED", "BR-D19")
    assert await _batch_state(db_pool, invoice_id) == "review"
    assert (await _flow_data(db_pool, invoice_id))["reviewed"] is False
    assert outbox[-1]["text"].startswith(f"📧 Revisa el correo de {SERIE}-{NUMERO}")
    assert [button["title"] for button in outbox[-1]["buttons"]] == ["Enviar", "Cancelar"]
    assert (tmp_path / "mail" / str(invoice_id)).is_dir()


async def test_a_failed_reviewed_send_clears_reviewed_so_the_next_delivery_posts_a_review(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D194, BR-D03: after a reviewed send fails, a reprocess posts a new review instead of sending."""
    profile_id = f"review-{uuid4().hex}"
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    cfg = _config(tmp_path, profile_id, "review")
    await _handler(db_pool, cfg, tmp_path, FakeGraphMode())(_job(invoice_id))
    await _intake(db_pool, _press(invoice_id, "send", profile_id))
    await _handler(db_pool, cfg, tmp_path, FailingGraphMode())(_job(invoice_id))
    assert await _invoice_state(db_pool, invoice_id) == ("BATCHED", "BR-D03")
    assert (await _flow_data(db_pool, invoice_id))["reviewed"] is False
    async with db_pool.connection() as conn, conn.transaction():
        await set_state(conn, invoice_id, "DELIVERING")
    mode = FakeGraphMode()

    await _handler(db_pool, cfg, tmp_path, mode)(_job(invoice_id))

    assert mode.calls == []
    assert await _invoice_state(db_pool, invoice_id) == ("BATCHED", "BR-D19")
    assert await _batch_state(db_pool, invoice_id) == "review"


async def test_revisar_on_a_prepared_batch_posts_the_review_and_the_old_draft_button_is_stale(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D194, BR-S02: after a reviewed dry_run, /revisar posts the review and [Ya lo envié] is stale."""
    profile_id = f"review-{uuid4().hex}"
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    cfg = _config(tmp_path, profile_id, "review", mode_name="dry_run")
    handler = _handler(db_pool, cfg, tmp_path, DryRun(tmp_path), mode_name="dry_run")
    await _review_then_send(db_pool, handler, invoice_id, profile_id)
    assert await _invoice_state(db_pool, invoice_id) == ("BATCHED", "BR-D07")
    assert await _batch_state(db_pool, invoice_id) == "prepared"
    queued = len(await _outbox(db_pool, profile_id))
    roles = IntakeRoles(
        reprocess_min_interval_s=0, admins=frozenset({profile_id}), review_flows=frozenset({FLOW})
    )

    await _intake(db_pool, _command(profile_id, f"/revisar {invoice_id}"), roles)
    await handler(_job(invoice_id))

    assert await _invoice_state(db_pool, invoice_id) == ("BATCHED", "BR-D19")
    assert await _batch_state(db_pool, invoice_id) == "review"
    await _intake(db_pool, _press(invoice_id, "sent", profile_id))
    outbox = (await _outbox(db_pool, profile_id))[queued:]
    assert outbox[-1]["rule_id"] == "BR-S02"
    assert await _batch_state(db_pool, invoice_id) == "review"
    assert await _invoice_state(db_pool, invoice_id) == ("BATCHED", "BR-D19")


async def test_two_jobs_for_the_same_reviewed_mail_send_it_once(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D194, D89: a leased-out job and the [Enviar] job overlap; the invoice lock lets one of them send."""
    profile_id = f"review-{uuid4().hex}"
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    cfg = _config(tmp_path, profile_id, "review")
    await _handler(db_pool, cfg, tmp_path, FakeGraphMode())(_job(invoice_id))
    await _intake(db_pool, _press(invoice_id, "send", profile_id))
    mode = SlowGraphMode()
    barrier = asyncio.Barrier(2)  # both jobs reach the send check together
    handler = _handler(db_pool, cfg, tmp_path, mode, drive=FakeDrive(on_upload=barrier.wait))

    await asyncio.gather(handler(_job(invoice_id)), handler(_job(invoice_id)))

    assert len(mode.calls) == 1
