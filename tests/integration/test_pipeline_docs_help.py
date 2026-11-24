"""Integration tests for the actions behind BR-F13's buttons (D189, BR-F13)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import InboundMessage
from invoicebot.channels.buttons import button_id
from invoicebot.config.loader import load_config
from invoicebot.config.models import MediaMaxBytes, StoreDirectory
from invoicebot.extract.fields import Field, Reading
from invoicebot.fetchers.base import FetchInvoice, FetchReason, FetchResult
from invoicebot.pipeline.fetch import FETCH_STEP, make_fetch_handler
from invoicebot.pipeline.handlers import MEGABYTE
from invoicebot.pipeline.intake import handle_inbound
from invoicebot.pipeline.questions import (
    RULE_DOCS_NEEDED,
    SEND_PDF_ACTION,
    TEACH_PORTAL_ACTION,
    WAIT_MAIL_ACTION,
)
from invoicebot.queue.jobs import Job
from invoicebot.queue.worker import Handler
from invoicebot.rules.messages_es import MESSAGES, state_desc
from invoicebot.states import DELIVERING, FETCHING_DOCS, MANUAL_REQUIRED
from invoicebot.store.invoices import create_invoice, save_reading, set_flow, set_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
FLOW = "work_microsoft"
MAX_QUEUED = 10
MEDIA_MAX_BYTES = MediaMaxBytes(image=10 * MEGABYTE, pdf=10 * MEGABYTE, xml=MEGABYTE, zip=10 * MEGABYTE)
#: The owner timezone, for the dates a command prints (S11.4).
TIMEZONE = "America/Lima"
RULE_STALE_BUTTON = "BR-S02"
RULE_PORTAL_OFFER = "BR-F14"
RULE_ASK_PORTAL = "BR-F02"
#: From tests/synthetic_rucs.txt; no real RUC may appear here.
ISSUER_RUC = "20000000001"
RAZON_SOCIAL = "Comercio Sintético"
PRINTED_URL = "https://portal.example.pe/consulta"
SERIE = "F001"
NUMERO = "15160"


def _field(value: str) -> Field:
    return Field(value=value, source="qr", confidence=1.0)


def _reading() -> Reading:
    """Build a complete synthetic reading the fetcher needs."""
    return Reading(
        fields={
            "ruc_emisor": _field(ISSUER_RUC),
            "tipo": _field("01"),
            "serie": _field(SERIE),
            "numero": _field(NUMERO),
            "fecha_emision": _field("2026-11-13"),
            "total": _field("49.90"),
        },
        document_url=None,
    )


def _portal_reading(printed_url: str | None) -> Reading:
    """Build the reading BR-F13's [🔗] offers a portal from (BR-F14, BR-F02)."""
    return Reading(
        fields={
            "ruc_emisor": _field(ISSUER_RUC),
            "razon_social": _field(RAZON_SOCIAL),
        },
        document_url=None,
        printed_url=printed_url,
    )


def _profile() -> str:
    """Return a profile id no other test uses, so tests cannot interfere."""
    return f"docs-{uuid4().hex}"


def _press(invoice_id: int, action: str, profile_id: str) -> InboundMessage:
    """Build a canonical button press for the invoice."""
    return InboundMessage(
        channel="whatsapp",
        external_id=f"ext-{uuid4().hex}",
        profile_id=profile_id,
        sender_role="owner",
        button_id=button_id(invoice_id, action),
        received_at=datetime.now(UTC),
    )


def _job(invoice_id: int, job_id: int = 1) -> Job:
    """Build the fetch job the handler receives for ``invoice_id``."""
    return Job(
        id=job_id,
        invoice_id=invoice_id,
        step=FETCH_STEP,
        attempts=0,
        crashes=0,
        lease_until=datetime.now(UTC),
    )


class RecordingFetcher:
    """A fetcher that counts its runs and writes nothing (BR-F13, D189)."""

    def __init__(self) -> None:
        self.calls = 0

    async def fetch(self, invoice: FetchInvoice, out_dir: Path) -> FetchResult:
        self.calls += 1
        return FetchResult(FetchReason.OK, (), None)


async def _invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    *,
    reading: Reading | None = None,
    state: str = FETCHING_DOCS,
) -> int:
    """Create a routed invoice with a stored reading and return its id."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await save_reading(conn, invoice_id, reading or _reading())
        await set_flow(conn, invoice_id, FLOW)
        await set_state(conn, invoice_id, state)
    return invoice_id


async def _handle(pool: AsyncConnectionPool[AsyncConnection[Any]], msg: InboundMessage) -> None:
    """Run one inbound message through intake in its own transaction."""
    async with pool.connection() as conn:
        await handle_inbound(
            conn,
            msg,
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
        )


def _fetch_handler(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    fetcher: RecordingFetcher,
    data_dir: Path,
) -> Handler:
    """Build the fetch handler with one registered inbox fetcher."""
    return make_fetch_handler(
        pool,
        load_config(EXAMPLE),
        StoreDirectory(stores={}, portals={}),
        {FLOW: {"email_inbox": fetcher}},
        data_dir,
    )


async def _invoice_row(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str, str | None]:
    """Return the invoice's ``(state, rule_id)``."""
    cur = await conn.execute("SELECT state, rule_id FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0]), None if row[1] is None else str(row[1])


async def _outbox(conn: AsyncConnection[Any], profile_id: str) -> list[tuple[str | None, str]]:
    """Return the outbox's ``(rule_id, text)`` rows for profile_id, oldest first."""
    cur = await conn.execute(
        """
        SELECT payload_json->>'rule_id', payload_json->>'text'
        FROM outbox
        WHERE "to" = %s
        ORDER BY id
        """,
        (profile_id,),
    )
    return [(rule_id, str(text)) for rule_id, text in await cur.fetchall()]


async def _attempts(conn: AsyncConnection[Any], invoice_id: int) -> list[str]:
    """Return the invoice's recorded fetch attempts, oldest first."""
    cur = await conn.execute(
        "SELECT fetcher FROM fetch_attempts WHERE invoice_id = %s ORDER BY id", (invoice_id,)
    )
    return [str(fetcher) for (fetcher,) in await cur.fetchall()]


async def _files(conn: AsyncConnection[Any], invoice_id: int) -> int:
    """Return how many files the invoice stored."""
    cur = await conn.execute("SELECT count(*) FROM files WHERE invoice_id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def test_send_pdf_moves_to_manual_required_and_the_pending_fetch_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """D189, BR-F13: [📎] moves the invoice to MANUAL_REQUIRED and the pending fetch writes nothing."""
    profile_id = _profile()
    invoice_id = await _invoice(db_pool, profile_id)
    await _handle(db_pool, _press(invoice_id, SEND_PDF_ACTION, profile_id))

    fetcher = RecordingFetcher()
    await _fetch_handler(db_pool, fetcher, tmp_path)(_job(invoice_id))

    async with db_pool.connection() as conn:
        invoice = await _invoice_row(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
        attempts = await _attempts(conn, invoice_id)
        files = await _files(conn, invoice_id)

    assert invoice == (MANUAL_REQUIRED, RULE_DOCS_NEEDED)
    assert outbox == [(RULE_DOCS_NEEDED, MESSAGES["BR-F13_upload"])]
    assert fetcher.calls == 0
    assert attempts == []
    assert files == 0


async def test_wait_mail_answers_and_keeps_the_invoice_fetching(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D189, BR-F13: [⏳] queues BR-F13_wait and changes no state."""
    profile_id = _profile()
    invoice_id = await _invoice(db_pool, profile_id)
    await _handle(db_pool, _press(invoice_id, WAIT_MAIL_ACTION, profile_id))

    async with db_pool.connection() as conn:
        invoice = await _invoice_row(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)

    assert invoice == (FETCHING_DOCS, None)
    assert outbox == [(RULE_DOCS_NEEDED, MESSAGES["BR-F13_wait"])]


@pytest.mark.parametrize(
    ("printed_url", "rule_id", "text"),
    [
        (
            PRINTED_URL,
            RULE_PORTAL_OFFER,
            MESSAGES[RULE_PORTAL_OFFER].format(razon_social=RAZON_SOCIAL, url=PRINTED_URL),
        ),
        (
            None,
            RULE_ASK_PORTAL,
            MESSAGES[RULE_ASK_PORTAL].format(razon_social=RAZON_SOCIAL, ruc_emisor=ISSUER_RUC),
        ),
    ],
)
async def test_teach_portal_offers_the_printed_url_or_asks_for_it(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    printed_url: str | None,
    rule_id: str,
    text: str,
) -> None:
    """D189, BR-F13: [🔗] queues BR-F14 for the printed URL and BR-F02 when the reading has none."""
    profile_id = _profile()
    invoice_id = await _invoice(db_pool, profile_id, reading=_portal_reading(printed_url))
    await _handle(db_pool, _press(invoice_id, TEACH_PORTAL_ACTION, profile_id))

    async with db_pool.connection() as conn:
        invoice = await _invoice_row(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)

    assert invoice == (FETCHING_DOCS, None)
    assert outbox == [(rule_id, text)]


async def test_send_pdf_on_a_delivering_invoice_is_stale(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D189, BR-S02: [📎] on an invoice already DELIVERING answers BR-S02 and changes nothing."""
    profile_id = _profile()
    invoice_id = await _invoice(db_pool, profile_id, state=DELIVERING)
    await _handle(db_pool, _press(invoice_id, SEND_PDF_ACTION, profile_id))

    async with db_pool.connection() as conn:
        invoice = await _invoice_row(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)

    assert invoice == (DELIVERING, None)
    assert outbox == [
        (
            RULE_STALE_BUTTON,
            MESSAGES[RULE_STALE_BUTTON].format(id=invoice_id, state_desc=state_desc(DELIVERING)),
        )
    ]
