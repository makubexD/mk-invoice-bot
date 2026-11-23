"""Cross-channel upload and ``/estado`` for one profile (S14.2, D176, R-1.9).

A profile with both a WhatsApp number and a Telegram user id opens an invoice on
WhatsApp, then uses Telegram: its PDF matches the WhatsApp invoice and the reply still
goes out on WhatsApp (the invoice's origin), ``/estado`` answers on Telegram (the
command's channel), and a quote is resolved only within its own channel. Postgres is
real; the Bot API sits behind ``httpx.MockTransport``.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import Attachment, InboundMessage
from invoicebot.channels.fake import FakeChannel
from invoicebot.channels.profiles import ProfileIndex
from invoicebot.channels.telegram.client import TelegramClient
from invoicebot.channels.telegram.media import TelegramMediaSource
from invoicebot.channels.telegram.parse import parse_update
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, MediaLimits, MediaMaxBytes
from invoicebot.extract.fields import Field, Reading
from invoicebot.pipeline.handlers import MEGABYTE
from invoicebot.pipeline.intake import IntakeResult, handle_inbound
from invoicebot.pipeline.steps import ATTACH_STEP
from invoicebot.pipeline.uploads import make_attach_handler
from invoicebot.queue.jobs import Job
from invoicebot.queue.outbox import OutboxSender
from invoicebot.store.invoices import create_invoice, save_reading, set_flow, set_state
from invoicebot.store.messages import link_message_to_invoice, store_inbound

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
#: The example config's full profile, copied under a profile id no other test uses.
FULL_PROFILE = "yo"
#: The flow a stuck invoice of this file was routed to (it needs the PDF and the XML).
FLOW = "work_microsoft"
MAX_QUEUED = 10
MEDIA_MAX_BYTES = MediaMaxBytes(image=10 * MEGABYTE, pdf=10 * MEGABYTE, xml=MEGABYTE, zip=10 * MEGABYTE)
#: The owner timezone, for the dates a command prints (S11.4).
TIMEZONE = "America/Lima"
#: Keyed digest of a sender outside the allowlist (R-1.2); these tests never send one.
HASH_KEY = "synthetic-hash-key"
#: Synthetic Bot API credentials; no real token appears here.
BOT_TOKEN = "123456" + ":ABC-fake-token"
#: From tests/synthetic_rucs.txt; no real RUC appears here.
ISSUER_RUC = "20000000001"
SERIE = "F001"
NUMERO = "15160"
TOTAL = "51.61"
PDF_MIME = "application/pdf"
PDF_BYTES = b"%PDF-1.4 synthetic invoice\n"
#: One id per Telegram update, so no two updates of a chat share one (R-1.8).
_NEXT_MESSAGE_ID = itertools.count(1)


@dataclass(frozen=True)
class _Identity:
    """A synthetic profile and the channel ids that resolve to it (R-1.9)."""

    profile_id: str
    whatsapp: str
    telegram: int

    def index(self) -> ProfileIndex:
        """Return the profile index that maps both synthetic channel ids to this profile."""
        cfg = load_config(EXAMPLE_CONFIG)
        profile = cfg.profiles[FULL_PROFILE].model_copy(
            update={"whatsapp": self.whatsapp, "telegram_user_id": self.telegram}
        )
        return ProfileIndex.from_config(cfg.model_copy(update={"profiles": {self.profile_id: profile}}))


def _identity() -> _Identity:
    """Return a profile id and channel ids no other test uses."""
    return _Identity(
        profile_id=f"profile-{uuid4().hex}",
        whatsapp=f"51{uuid4().int % 10**9:09d}",
        telegram=10**9 + uuid4().int % 10**9,
    )


def _field(value: str) -> Field:
    """Return a field the QR extractor would have read with full confidence."""
    return Field(value=value, source="qr", confidence=1.0)


def _reading() -> Reading:
    """Return the synthetic reading a stuck invoice keeps."""
    return Reading(
        fields={
            "ruc_emisor": _field(ISSUER_RUC),
            "tipo": _field("01"),
            "serie": _field(SERIE),
            "numero": _field(NUMERO),
            "total": _field(TOTAL),
            "fecha_emision": _field("2026-11-05"),
        },
        document_url=None,
    )


def _config() -> Config:
    """Return the example config with the work flow requiring the PDF and the XML."""
    cfg = load_config(EXAMPLE_CONFIG)
    flow = cfg.flows[FLOW].model_copy(
        update={"required_docs": ["xml", "pdf"], "manual_uploads": ["pdf", "xml"]}
    )
    return cfg.model_copy(update={"flows": {**cfg.flows, FLOW: flow}})


def _limits() -> MediaLimits:
    """Return limits with synthetic byte caps larger than every test body."""
    return MediaLimits(
        max_megapixels=40,
        max_bytes=MediaMaxBytes(
            image=8 * MEGABYTE,
            pdf=16 * MEGABYTE,
            xml=2 * MEGABYTE,
            zip=4 * MEGABYTE,
        ),
        zip_max_entries=20,
        zip_max_ratio=50,
    )


def _telegram_update(
    identity: _Identity,
    *,
    text: str = "",
    document: str | None = None,
    reply_to_message_id: int | None = None,
) -> dict[str, Any]:
    """Return a Bot API update carrying one private document or text (R-1.7)."""
    message_id = next(_NEXT_MESSAGE_ID)
    message: dict[str, Any] = {
        "message_id": message_id,
        "from": {"id": identity.telegram},
        "chat": {"id": identity.telegram, "type": "private"},
        "date": int(datetime.now(UTC).timestamp()),
    }
    if document is None:
        message["text"] = text
    else:
        message["document"] = {"file_id": document, "mime_type": PDF_MIME, "file_size": len(PDF_BYTES)}
        if text:
            message["caption"] = text
    if reply_to_message_id is not None:
        message["reply_to_message"] = {"message_id": reply_to_message_id}
    return {"update_id": message_id, "message": message}


def _parse(identity: _Identity, update: dict[str, Any]) -> InboundMessage:
    """Return the one canonical message the Telegram parser builds from update (S14.1)."""
    parsed = parse_update(
        update,
        identity.index(),
        owner_profile=identity.profile_id,
        admin_profiles=frozenset(),
        sender_hash_key=HASH_KEY,
        now=datetime.now(UTC),
    )
    assert len(parsed.messages) == 1
    return parsed.messages[0]


def _telegram_media(file_id: str, body: bytes) -> Callable[[httpx.Request], httpx.Response]:
    """Serve one file's ``getFile`` info and bytes from a fake Bot API (R-1.11)."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": f"documents/{file_id}", "file_size": len(body)}},
            )
        return httpx.Response(200, content=body)

    return handle


async def _clear_outbox(conn: AsyncConnection[Any]) -> None:
    """Remove every outbox row so a test only sends what it queued."""
    await conn.execute("DELETE FROM outbox")


async def _stuck_invoice(pool: AsyncConnectionPool[AsyncConnection[Any]], profile_id: str) -> int:
    """Create a WhatsApp-origin invoice waiting for its PDF at BR-F07 and return its id."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await save_reading(conn, invoice_id, _reading())
        await set_flow(conn, invoice_id, FLOW)
        await set_state(conn, invoice_id, "MANUAL_REQUIRED", rule_id="BR-F07")
    return invoice_id


async def _store_whatsapp_photo(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    external_id: str,
    invoice_id: int,
) -> None:
    """Store a WhatsApp photo message and link it to its invoice, as intake does (R-1.5)."""
    msg = InboundMessage(
        channel="whatsapp",
        external_id=external_id,
        profile_id=profile_id,
        sender_role="owner",
        attachments=[Attachment(kind="photo", media_ref=f"ref-{uuid4().hex}", mime="image/jpeg", size=1024)],
        received_at=datetime.now(UTC),
    )
    async with pool.connection() as conn, conn.transaction():
        assert await store_inbound(conn, msg)
        await link_message_to_invoice(conn, "whatsapp", external_id, invoice_id)


async def _intake(pool: AsyncConnectionPool[AsyncConnection[Any]], msg: InboundMessage) -> IntakeResult:
    """Deliver msg once through the pipeline in its own transaction."""
    async with pool.connection() as conn:
        return await handle_inbound(
            conn,
            msg,
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
        )


def _sender(pool: AsyncConnectionPool[AsyncConnection[Any]], fake: FakeChannel) -> OutboxSender:
    """Build an outbox sender that delivers both chat channels to fake."""
    transport = fake.transport()
    return OutboxSender(pool, {"whatsapp": transport, "telegram": transport})


def _job(invoice_id: int, job_id: int) -> Job:
    """Build the attach job the handler receives."""
    return Job(
        id=job_id,
        invoice_id=invoice_id,
        step=ATTACH_STEP,
        attempts=0,
        crashes=0,
        lease_until=datetime.now(UTC),
    )


async def _attach_jobs(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int) -> int:
    """Return how many attach jobs the invoice has queued (R-1.5)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM jobs WHERE invoice_id = %s AND step = %s",
            (invoice_id, ATTACH_STEP),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _newest_attach_job(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int) -> int:
    """Return the id of the invoice's newest attach job (R-1.5)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT max(id) FROM jobs WHERE invoice_id = %s AND step = %s",
            (invoice_id, ATTACH_STEP),
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] is not None
    return int(row[0])


async def _state_rule(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str, str | None]:
    """Return the invoice's ``(state, rule_id)``."""
    cur = await conn.execute("SELECT state, rule_id FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0]), None if row[1] is None else str(row[1])


async def _file_kinds(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int) -> list[str]:
    """Return the kinds of the files stored for the invoice, oldest first."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT kind FROM files WHERE invoice_id = %s ORDER BY id", (invoice_id,))
        return [str(row[0]) for row in await cur.fetchall()]


async def test_telegram_pdf_matches_the_whatsapp_invoice_and_answers_on_whatsapp(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """S14.2, D176, R-1.9, BR-D09: a Telegram PDF attaches to the WhatsApp invoice and replies there."""
    identity = _identity()
    file_id = f"doc{uuid4().hex}"
    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
    invoice_id = await _stuck_invoice(db_pool, identity.profile_id)

    msg = _parse(identity, _telegram_update(identity, document=file_id))
    result = await _intake(db_pool, msg)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert await _attach_jobs(db_pool, invoice_id) == 1

    job_id = await _newest_attach_job(db_pool, invoice_id)
    transport = httpx.MockTransport(_telegram_media(file_id, PDF_BYTES))
    async with httpx.AsyncClient(transport=transport) as http:
        source = TelegramMediaSource(TelegramClient(http, token=BOT_TOKEN))
        attach = make_attach_handler(db_pool, _config(), {"telegram": source}, tmp_path, _limits())
        await attach(_job(invoice_id, job_id))

    async with db_pool.connection() as conn:
        assert await _state_rule(conn, invoice_id) == ("MANUAL_REQUIRED", "BR-F07")
    assert await _file_kinds(db_pool, invoice_id) == ["pdf"]

    fake = FakeChannel()
    assert await _sender(db_pool, fake).send_pending() == 1
    assert fake.rule_ids() == ["BR-D09"]
    assert [message.channel for message in fake.sent] == ["whatsapp"]
    assert "XML" in fake.texts()[0]


async def test_estado_from_telegram_lists_the_whatsapp_invoice_on_telegram(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """S14.2, D176, R-1.9: /estado from Telegram lists the invoice and answers on Telegram."""
    identity = _identity()
    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
    invoice_id = await _stuck_invoice(db_pool, identity.profile_id)

    msg = _parse(identity, _telegram_update(identity, text=f"/estado {invoice_id}"))
    result = await _intake(db_pool, msg)

    fake = FakeChannel()
    assert await _sender(db_pool, fake).send_pending() == 1
    assert result.invoice_id is None
    assert fake.rule_ids() == [None]
    assert [message.channel for message in fake.sent] == ["telegram"]
    assert str(invoice_id) in fake.texts()[0]


async def test_telegram_quote_of_a_whatsapp_message_matches_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """S14.2, D176, R-1.9, BR-D14: a quote resolves only within its own channel."""
    identity = _identity()
    profile_id = identity.profile_id
    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
    first = await _stuck_invoice(db_pool, profile_id)
    second = await _stuck_invoice(db_pool, profile_id)
    # The WhatsApp message's id is shaped like a Telegram external id, so a lookup that
    # ignored the channel would resolve it; D176 keeps the quote inside one channel.
    reply_message_id = next(_NEXT_MESSAGE_ID)
    whatsapp_external_id = f"{identity.telegram}:{reply_message_id}"
    await _store_whatsapp_photo(db_pool, profile_id, whatsapp_external_id, first)

    file_id = f"doc{uuid4().hex}"
    msg = _parse(
        identity,
        _telegram_update(identity, document=file_id, reply_to_message_id=reply_message_id),
    )

    assert msg.reply_to_external_id == whatsapp_external_id
    result = await _intake(db_pool, msg)

    assert result.invoice_id is None
    assert await _attach_jobs(db_pool, first) == 0
    assert await _attach_jobs(db_pool, second) == 0
    fake = FakeChannel()
    assert await _sender(db_pool, fake).send_pending() == 1
    assert fake.rule_ids() == ["BR-D14"]
    assert [message.channel for message in fake.sent] == ["telegram"]
