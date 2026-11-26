"""Integration tests for flow B's review before send (D194, D88, D89).

The folder is an in-memory fake drive and the automatic mode a fake that records its sends;
Postgres is real. A mailer on ``send: review`` posts the mail to the chat until ``reviewed``
is set in the flow data.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.buttons import button_id
from invoicebot.config.loader import load_config
from invoicebot.config.models import (
    Brand,
    Config,
    ExcelTemplateSink,
    MailerSink,
    Store,
    StoreDirectory,
)
from invoicebot.extract.fields import Field, Reading
from invoicebot.pipeline.deliver import DELIVER_STEP, DeliverContext, make_deliver_handler
from invoicebot.queue.jobs import Job
from invoicebot.queue.worker import Handler
from invoicebot.sinks.base import OutgoingMail
from invoicebot.store.flow_b import get_batch_state, merge_flow_data, open_batch
from invoicebot.store.invoices import FileRecord, add_file, create_invoice, save_reading, set_flow, set_state
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


class FakeDrive:
    """An in-memory drive: folders hold their entry names and uploads keep their bytes."""

    def __init__(self) -> None:
        self.folders: dict[str, list[str]] = {}
        self.uploads: dict[str, bytes] = {}

    async def list_names(self, path: str) -> list[str]:
        """Return the entries of a folder; a path that does not exist has none."""
        return list(self.folders.get(path, []))

    async def create_folder(self, parent: str, name: str) -> None:
        """List the new folder under its parent."""
        self.folders.setdefault(parent, []).append(name)

    async def upload(self, path: str, data: bytes) -> None:
        """Keep the file's bytes at its absolute path."""
        self.uploads[path] = data

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


def _config(tmp_path: Path, profile_id: str, send: str) -> Config:
    """Return example.yaml with a synthetic template, the graph mode and the given ``send``."""
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
            sink = sink.model_copy(update={"modes": ["graph"], "send": send})
        sinks.append(sink)
    flow = cfg.flows[FLOW].model_copy(update={"sinks": sinks})
    profile = cfg.profiles[PROFILE].model_copy(update={"admin": False})
    return cfg.model_copy(update={"flows": {**cfg.flows, FLOW: flow}, "profiles": {profile_id: profile}})


def _handler(
    pool: AsyncConnectionPool[AsyncConnection[Any]], cfg: Config, data_dir: Path, mode: FakeGraphMode
) -> Handler:
    drive = FakeDrive()
    return make_deliver_handler(
        DeliverContext(
            pool, cfg, _directory(), drive, drive, {"graph": mode}, data_dir, laptop_flows=frozenset()
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
    ("send", "flow_data"),
    [("review", {"reviewed": True}), ("auto", None)],
    ids=["reviewed", "auto"],
)
async def test_a_reviewed_or_auto_flow_sends_once_with_br_d10(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    send: str,
    flow_data: dict[str, Any] | None,
) -> None:
    """D194: a reviewed mail, like an auto flow's, is sent once and confirmed by BR-D10 alone."""
    profile_id = f"review-{uuid4().hex}"
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id, flow_data)
    mode = FakeGraphMode()

    await _handler(db_pool, _config(tmp_path, profile_id, send), tmp_path, mode)(_job(invoice_id))

    outbox = await _outbox(db_pool, profile_id)
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
