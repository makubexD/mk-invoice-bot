"""Conversation-level integration tests delivering through the fake channel.

Covers R-1.1, R-1.4, R-1.6, R-1.8, BR-S01 and BR-S02 against real Postgres,
with the outbox sender handing each message to :class:`FakeChannel`.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import Attachment, InboundMessage
from invoicebot.channels.fake import FakeChannel
from invoicebot.pipeline.intake import handle_inbound
from invoicebot.pipeline.replies import resolve_reply
from invoicebot.queue.outbox import OutboxSender
from invoicebot.store.invoices import create_invoice, set_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

MAX_QUEUED = 10
VALID_STATES: dict[str, frozenset[str]] = {"confirm": frozenset({"CONFIRMING"})}


def _profile() -> str:
    """Return a profile id no other test uses, so tests cannot interfere."""
    return f"profile-{uuid4().hex}"


def _external_id() -> str:
    """Return a channel message id no other test uses."""
    return f"ext-{uuid4().hex}"


def _photo() -> Attachment:
    """Return a photo attachment with a unique media reference."""
    return Attachment(kind="photo", media_ref=f"ref-{uuid4().hex}", mime="image/jpeg", size=1024)


def _message(
    profile_id: str,
    *,
    attachments: list[Attachment] | None = None,
    button_id: str | None = None,
) -> InboundMessage:
    """Build a canonical inbound message for profile_id."""
    return InboundMessage(
        channel="whatsapp",
        external_id=_external_id(),
        profile_id=profile_id,
        sender_role="owner",
        attachments=[] if attachments is None else attachments,
        button_id=button_id,
        received_at=datetime.now(UTC),
    )


async def _clear_outbox(conn: AsyncConnection[Any]) -> None:
    """Remove every outbox row so a test only sends what it queued."""
    await conn.execute("DELETE FROM outbox")


def _sender(pool: AsyncConnectionPool[AsyncConnection[Any]], fake: FakeChannel) -> OutboxSender:
    """Build an outbox sender that delivers whatsapp rows to fake."""
    return OutboxSender(pool, {"whatsapp": fake.transport()})


async def test_one_photo_sends_the_acknowledgement(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.1: one photo sends one BR-S09 whose text carries the invoice id."""
    fake = FakeChannel()
    profile_id = _profile()
    msg = _message(profile_id, attachments=[_photo()])

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)

    sender = _sender(db_pool, fake)
    assert await sender.send_pending() == 1
    assert fake.rule_ids() == ["BR-S09"]
    assert result.invoice_id is not None
    assert str(result.invoice_id) in fake.texts()[0]


async def test_three_photos_with_two_queued_send_back_pressure_last(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.6, BR-S01: two acks then BR-S01, in the order the photos arrived."""
    fake = FakeChannel()
    profile_id = _profile()
    messages = [_message(profile_id, attachments=[_photo()]) for _ in range(3)]

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        results = [await handle_inbound(conn, msg, max_queued_per_sender=2) for msg in messages]

    sender = _sender(db_pool, fake)
    assert await sender.send_pending() == 3
    assert [result.status for result in results] == ["queued", "queued", "back_pressure"]
    assert fake.rule_ids() == ["BR-S09", "BR-S09", "BR-S01"]


async def test_duplicate_photo_sends_nothing_more(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.8: a redelivered photo is stored once and sends no second message."""
    fake = FakeChannel()
    profile_id = _profile()
    msg = _message(profile_id, attachments=[_photo()])
    sender = _sender(db_pool, fake)

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        first = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
    assert await sender.send_pending() == 1

    async with db_pool.connection() as conn:
        second = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)

    assert first.status == "queued"
    assert second.status == "duplicate"
    assert await sender.send_pending() == 0
    assert fake.rule_ids() == ["BR-S09"]


async def test_a_stale_button_sends_br_s02(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S02: a button pressed for a DONE invoice sends one BR-S02 and resolves to None."""
    fake = FakeChannel()
    profile_id = _profile()

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await set_state(conn, invoice_id, "DONE")

    msg = _message(profile_id, button_id=f"{invoice_id}:confirm")
    async with db_pool.connection() as conn:
        resolved = await resolve_reply(conn, msg, VALID_STATES)

    assert resolved is None
    sender = _sender(db_pool, fake)
    assert await sender.send_pending() == 1
    assert fake.rule_ids() == ["BR-S02"]


async def test_fetch_media_returns_registered_paths_only(tmp_path: Path) -> None:
    """R-1.4: fetch_media serves a registered path and raises KeyError for an unknown ref."""
    fake = FakeChannel()
    path = tmp_path / "photo.jpg"
    fake.add_media("ref-known", path)

    assert await fake.fetch_media("ref-known") == path
    with pytest.raises(KeyError, match="ref-unknown"):
        await fake.fetch_media("ref-unknown")
