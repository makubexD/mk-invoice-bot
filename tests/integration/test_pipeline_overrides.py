"""Integration tests for chat overrides: caption, late text, stale and plain text.

Covers R-7.4, D70, D90 and BR-S02 against real Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import Attachment, InboundMessage
from invoicebot.pipeline.intake import handle_inbound
from invoicebot.rules.messages_es import MESSAGES, OVERRIDES_SAVED, state_desc
from invoicebot.store.flow_b import get_batch_state, merge_flow_data, open_batch, set_batch_state
from invoicebot.store.invoices import create_invoice, set_flow, set_state
from invoicebot.store.messages import link_message_to_invoice, store_inbound

pytestmark = pytest.mark.asyncio(loop_scope="session")

MAX_QUEUED = 10
FLOW = "support"
BATCHED = "BATCHED"
DELIVERING = "DELIVERING"
DONE = "DONE"
RULE_BATCHED = "BR-D07"
RULE_SENT = "BR-D08"


def _profile() -> str:
    """Return a profile id no other test uses, so tests cannot interfere."""
    return f"override-{uuid4().hex}"


def _external_id() -> str:
    """Return a channel message id no other test uses."""
    return f"ext-{uuid4().hex}"


def _photo() -> Attachment:
    """Return a photo attachment with a unique media reference."""
    return Attachment(kind="photo", media_ref=f"ref-{uuid4().hex}", mime="image/jpeg", size=1024)


def _message(
    profile_id: str,
    *,
    external_id: str,
    text: str = "",
    attachments: list[Attachment] | None = None,
    reply_to: str | None = None,
) -> InboundMessage:
    """Build a canonical inbound message for profile_id."""
    return InboundMessage(
        channel="whatsapp",
        external_id=external_id,
        profile_id=profile_id,
        sender_role="owner",
        text=text,
        attachments=[] if attachments is None else attachments,
        reply_to_external_id=reply_to,
        received_at=datetime.now(UTC),
    )


async def _overrides(conn: AsyncConnection[Any], invoice_id: int) -> dict[str, Any] | None:
    """Return the invoice's stored overrides, or None when it has none."""
    cur = await conn.execute("SELECT flow_data_json->'overrides' FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _invoice_state(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str, str | None]:
    """Return the invoice's ``(state, rule_id)``."""
    cur = await conn.execute("SELECT state, rule_id FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0]), None if row[1] is None else str(row[1])


async def _jobs(conn: AsyncConnection[Any], invoice_id: int) -> list[str]:
    """Return the steps of the invoice's queued jobs, in insertion order."""
    cur = await conn.execute("SELECT step FROM jobs WHERE invoice_id = %s ORDER BY id", (invoice_id,))
    return [str(step) for (step,) in await cur.fetchall()]


async def _texts(conn: AsyncConnection[Any], profile_id: str) -> list[tuple[str | None, str]]:
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


async def test_photo_caption_override_lands_in_the_new_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.4, D70: a caption ``motivo=Viaje`` is stored in the new invoice's flow data."""
    profile_id = _profile()
    msg = _message(profile_id, external_id=_external_id(), text="motivo=Viaje", attachments=[_photo()])

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        assert result.invoice_id is not None
        overrides = await _overrides(conn, result.invoice_id)
        state = await _invoice_state(conn, result.invoice_id)
        jobs = await _jobs(conn, result.invoice_id)

    assert result.status == "queued"
    assert overrides == {"motivo": "Viaje"}
    assert state == ("RECEIVED", None)
    assert jobs == ["download_media"]


async def test_late_override_reopens_a_prepared_batch(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.4, D70: ``descripcion=`` on a BATCHED prepared invoice moves it to DELIVERING."""
    profile_id = _profile()
    async with db_pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await set_flow(conn, invoice_id, FLOW)
        await set_state(conn, invoice_id, BATCHED, rule_id=RULE_BATCHED)
        await merge_flow_data(conn, invoice_id, {"overrides": {"motivo": "Viaje"}})
        await open_batch(conn, invoice_id, FLOW, "prepared")
    msg = _message(profile_id, external_id=_external_id(), text="descripcion=Almuerzo")

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        overrides = await _overrides(conn, invoice_id)
        state = await _invoice_state(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert overrides == {"motivo": "Viaje", "descripcion": "Almuerzo"}
    assert state == (DELIVERING, None)
    assert jobs == ["deliver"]
    assert batch_state == "prepared"
    assert texts == [(None, OVERRIDES_SAVED.format(id=invoice_id))]


async def test_override_on_a_done_invoice_gets_br_s02(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S02, D90: an override quoting a DONE invoice changes nothing and gets BR-S02."""
    profile_id = _profile()
    quoted = _message(profile_id, external_id=_external_id())
    async with db_pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await set_flow(conn, invoice_id, FLOW)
        await store_inbound(conn, quoted)
        await link_message_to_invoice(conn, quoted.channel, quoted.external_id, invoice_id)
        batch_id = await open_batch(conn, invoice_id, FLOW, "prepared")
        await set_batch_state(conn, batch_id, "sent")
        await set_state(conn, invoice_id, DONE, rule_id=RULE_SENT)
    msg = _message(
        profile_id,
        external_id=_external_id(),
        text="motivo=Viaje",
        reply_to=quoted.external_id,
    )

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        overrides = await _overrides(conn, invoice_id)
        state = await _invoice_state(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    stale = MESSAGES["BR-S02"].format(id=invoice_id, state_desc=state_desc(DONE))
    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert overrides is None
    assert state == (DONE, RULE_SENT)
    assert jobs == []
    assert texts == [("BR-S02", stale)]


async def test_text_without_equals_is_only_stored(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.4, D70: a text with no override line changes nothing and sends nothing."""
    profile_id = _profile()
    async with db_pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await set_flow(conn, invoice_id, FLOW)
        await set_state(conn, invoice_id, BATCHED, rule_id=RULE_BATCHED)
        await open_batch(conn, invoice_id, FLOW, "prepared")
    msg = _message(profile_id, external_id=_external_id(), text="gracias, ya lo reviso")

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        overrides = await _overrides(conn, invoice_id)
        state = await _invoice_state(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)
        cur = await conn.execute("SELECT count(*) FROM messages WHERE external_id = %s", (msg.external_id,))
        row = await cur.fetchone()

    assert result.status == "stored"
    assert result.invoice_id is None
    assert overrides is None
    assert state == (BATCHED, RULE_BATCHED)
    assert jobs == []
    assert texts == []
    assert row is not None
    assert row[0] == 1
