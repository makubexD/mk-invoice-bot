"""Integration tests for button actions and their intake path (R-1.8, R-3.5, R-7.8, R-7.13)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import InboundMessage
from invoicebot.channels.whatsapp.payloads import button_id
from invoicebot.pipeline.intake import handle_inbound
from invoicebot.rules.messages_es import CANCELLED_BY_USER, MESSAGES, state_desc
from invoicebot.store.flow_b import get_batch_state, open_batch
from invoicebot.store.invoices import FileRecord, add_file, create_invoice, get_file_path, set_flow, set_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

MAX_QUEUED = 10
FLOW = "work_microsoft"
CONFIRMING = "CONFIRMING"
BATCHED = "BATCHED"
RULE_CAP = "BR-D06"
RULE_BATCHED = "BR-D07"
RULE_SENT = "BR-D08"
DONE = "DONE"


def _profile() -> str:
    """Return a profile id no other test uses, so tests cannot interfere."""
    return f"press-{uuid4().hex}"


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


async def _waiting_invoice(pool: AsyncConnectionPool[AsyncConnection[Any]], profile_id: str) -> int:
    """Create a BR-D06 invoice in CONFIRMING and return its id."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await set_flow(conn, invoice_id, FLOW)
        await set_state(conn, invoice_id, CONFIRMING, rule_id=RULE_CAP)
    return invoice_id


async def _batched_invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    tmp_path: Path,
) -> tuple[int, Path]:
    """Create a BATCHED invoice with a prepared batch and one synthetic photo."""
    photo = tmp_path / f"{uuid4().hex}.jpg"
    photo.write_bytes(b"\xff\xd8\xff synthetic photo\n")
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await set_flow(conn, invoice_id, FLOW)
        await set_state(conn, invoice_id, BATCHED, rule_id=RULE_BATCHED)
        await open_batch(conn, invoice_id, FLOW, "prepared")
        await add_file(
            conn,
            invoice_id,
            FileRecord(
                kind="photo",
                local_path=str(photo),
                remote_ref=None,
                sha256=hashlib.sha256(photo.read_bytes()).hexdigest(),
                size=photo.stat().st_size,
            ),
        )
    return invoice_id, photo


async def _invoice_row(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str, str | None]:
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


async def test_continue_moves_the_invoice_to_fetching_docs(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.13, BR-D06: [Continuar] on a BR-D06 invoice moves it on with a fetch job."""
    profile_id = _profile()
    invoice_id = await _waiting_invoice(db_pool, profile_id)
    msg = _press(invoice_id, "continue", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert invoice == ("FETCHING_DOCS", RULE_CAP)
    assert jobs == ["fetch"]
    assert texts == []


async def test_cancel_cancels_the_invoice_and_replies(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.13, BR-D06: [Cancelar] moves the invoice to CANCELLED and sends one reply."""
    profile_id = _profile()
    invoice_id = await _waiting_invoice(db_pool, profile_id)
    msg = _press(invoice_id, "cancel", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert invoice == ("CANCELLED", RULE_CAP)
    assert jobs == []
    assert texts == [(None, CANCELLED_BY_USER.format(id=invoice_id))]


async def test_sent_closes_a_prepared_batch_and_deletes_the_photo(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """R-7.8, BR-D08: [Ya lo envié] marks the batch sent, the invoice DONE and deletes the photo."""
    profile_id = _profile()
    invoice_id, photo = await _batched_invoice(db_pool, profile_id, tmp_path)
    msg = _press(invoice_id, "sent", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        photo_path = await get_file_path(conn, invoice_id, "photo")
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert invoice == (DONE, RULE_SENT)
    assert batch_state == "sent"
    assert photo_path is None
    assert texts == [(RULE_SENT, MESSAGES[RULE_SENT])]
    assert not photo.exists()


async def test_a_second_sent_press_is_stale_and_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """BR-S02: [Ya lo envié] again on the DONE invoice answers BR-S02 and changes nothing."""
    profile_id = _profile()
    invoice_id, _ = await _batched_invoice(db_pool, profile_id, tmp_path)
    async with db_pool.connection() as conn:
        await handle_inbound(conn, _press(invoice_id, "sent", profile_id), max_queued_per_sender=MAX_QUEUED)

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, _press(invoice_id, "sent", profile_id), max_queued_per_sender=MAX_QUEUED
        )
        invoice = await _invoice_row(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert invoice == (DONE, RULE_SENT)
    assert batch_state == "sent"
    assert jobs == []
    assert texts == [
        (RULE_SENT, MESSAGES[RULE_SENT]),
        ("BR-S02", MESSAGES["BR-S02"].format(id=invoice_id, state_desc=state_desc(DONE))),
    ]


async def test_a_press_from_another_profile_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.5: another profile's button changes no state, job or reply."""
    owner = _profile()
    other = _profile()
    invoice_id = await _waiting_invoice(db_pool, owner)
    msg = _press(invoice_id, "continue", other)

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        owner_texts = await _texts(conn, owner)
        other_texts = await _texts(conn, other)

    assert result.status == "stored"
    assert result.invoice_id is None
    assert invoice == (CONFIRMING, RULE_CAP)
    assert jobs == []
    assert owner_texts == []
    assert other_texts == []


async def test_a_redelivered_press_acts_once(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """R-1.8: the same (channel, external_id) press twice acts once and stores one message."""
    profile_id = _profile()
    invoice_id, photo = await _batched_invoice(db_pool, profile_id, tmp_path)
    msg = _press(invoice_id, "sent", profile_id)

    async with db_pool.connection() as conn:
        first = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        second = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert first.status == "stored"
    assert second.status == "duplicate"
    assert second.invoice_id is None
    assert invoice == (DONE, RULE_SENT)
    assert batch_state == "sent"
    assert texts == [(RULE_SENT, MESSAGES[RULE_SENT])]
    assert not photo.exists()
