"""Integration tests for inbound message storage (R-1.8) and attachment matching (R-1.5)."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import Attachment, AttachmentKind, ChannelName, InboundMessage
from invoicebot.store.messages import (
    find_open_invoice_for_attachment,
    link_message_to_invoice,
    store_inbound,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _profile() -> str:
    """Return a profile id no other test uses, so tests cannot interfere."""
    return f"profile-{uuid4().hex}"


def _external_id() -> str:
    """Return a channel message id no other test uses."""
    return f"ext-{uuid4().hex}"


def _attachment(kind: AttachmentKind = "pdf") -> Attachment:
    """Return a media attachment of kind."""
    return Attachment(kind=kind, media_ref=f"ref-{uuid4().hex}", mime="application/pdf", size=16)


def _message(
    profile_id: str,
    *,
    external_id: str,
    channel: ChannelName = "whatsapp",
    text: str = "",
    attachments: list[Attachment] | None = None,
) -> InboundMessage:
    """Build a canonical inbound message for profile_id."""
    return InboundMessage(
        channel=channel,
        external_id=external_id,
        profile_id=profile_id,
        sender_role="owner",
        text=text,
        attachments=[] if attachments is None else attachments,
        received_at=datetime.now(UTC),
    )


async def _insert_invoice(
    conn: AsyncConnection[Any],
    profile_id: str,
    *,
    state: str = "MANUAL_REQUIRED",
    origin_channel: ChannelName = "whatsapp",
    created_at: datetime | None = None,
) -> int:
    """Insert an invoice for profile_id and return its id."""
    cur = await conn.execute(
        """
        INSERT INTO invoices (profile_id, origin_channel, state, created_at)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (profile_id, origin_channel, state, created_at or datetime.now(UTC)),
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def test_duplicate_delivery_is_stored_once(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.8: a redelivered (channel, external_id) is ignored, not stored twice."""
    profile_id = _profile()
    msg = _message(profile_id, external_id=_external_id())

    async with db_pool.connection() as conn:
        assert await store_inbound(conn, msg) is True
        assert await store_inbound(conn, msg) is False
        cur = await conn.execute(
            "SELECT count(*) FROM messages WHERE channel = %s AND external_id = %s",
            (msg.channel, msg.external_id),
        )
        row = await cur.fetchone()

    assert row is not None
    assert row[0] == 1


async def test_same_external_id_on_another_channel_is_stored(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.8: idempotency is per channel, so the same id on two channels is two messages."""
    profile_id = _profile()
    external_id = _external_id()
    whatsapp = _message(profile_id, external_id=external_id, channel="whatsapp")
    telegram = _message(profile_id, external_id=external_id, channel="telegram")

    async with db_pool.connection() as conn:
        assert await store_inbound(conn, whatsapp) is True
        assert await store_inbound(conn, telegram) is True
        cur = await conn.execute("SELECT count(*) FROM messages WHERE external_id = %s", (external_id,))
        row = await cur.fetchone()

    assert row is not None
    assert row[0] == 2


async def test_kind_is_text_or_the_first_attachment_kind(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.8: a stored message is text without attachments, else its first attachment kind."""
    profile_id = _profile()
    text = _message(profile_id, external_id=_external_id(), text="hola")
    with_files = _message(
        profile_id, external_id=_external_id(), attachments=[_attachment("pdf"), _attachment("xml")]
    )

    async with db_pool.connection() as conn:
        assert await store_inbound(conn, text) is True
        assert await store_inbound(conn, with_files) is True
        cur = await conn.execute(
            "SELECT external_id, kind FROM messages WHERE profile_id = %s",
            (profile_id,),
        )
        rows = await cur.fetchall()

    kinds = {row[0]: row[1] for row in rows}
    assert kinds == {text.external_id: "text", with_files.external_id: "pdf"}


async def test_telegram_document_matches_whatsapp_invoice_of_same_profile(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.5: matching is per profile across channels, not per channel."""
    profile_id = _profile()

    async with db_pool.connection() as conn:
        invoice_id = await _insert_invoice(conn, profile_id, origin_channel="whatsapp")
        document = _message(
            profile_id, external_id=_external_id(), channel="telegram", attachments=[_attachment()]
        )
        assert await store_inbound(conn, document) is True

        matched = await find_open_invoice_for_attachment(conn, profile_id, None)

    assert matched == invoice_id


async def test_quoted_invoice_wins_over_a_more_recent_one(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.5: the invoice the message quotes beats the most recent open invoice."""
    profile_id = _profile()
    now = datetime.now(UTC)

    async with db_pool.connection() as conn:
        quoted = await _insert_invoice(conn, profile_id, created_at=now - timedelta(minutes=5))
        await _insert_invoice(conn, profile_id, created_at=now)
        source = _message(profile_id, external_id=_external_id())
        assert await store_inbound(conn, source) is True
        await link_message_to_invoice(conn, source.channel, source.external_id, quoted)

        matched = await find_open_invoice_for_attachment(
            conn, profile_id, (source.channel, source.external_id)
        )

    assert matched == quoted


async def test_quoted_invoice_of_another_profile_is_ignored(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.5: a profile never matches an invoice quoted by another profile."""
    profile_id = _profile()
    other_profile_id = _profile()
    now = datetime.now(UTC)

    async with db_pool.connection() as conn:
        own = await _insert_invoice(conn, profile_id, created_at=now - timedelta(minutes=5))
        foreign = await _insert_invoice(conn, other_profile_id, created_at=now)
        source = _message(other_profile_id, external_id=_external_id())
        assert await store_inbound(conn, source) is True
        await link_message_to_invoice(conn, source.channel, source.external_id, foreign)

        matched = await find_open_invoice_for_attachment(
            conn, profile_id, (source.channel, source.external_id)
        )

    assert matched == own
    assert matched != foreign


async def test_invoice_in_another_state_is_not_matched(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.5: only MANUAL_REQUIRED and CONFIRMING invoices accept attachments."""
    profile_id = _profile()

    async with db_pool.connection() as conn:
        await _insert_invoice(conn, profile_id, state="DONE")
        matched = await find_open_invoice_for_attachment(conn, profile_id, None)

    assert matched is None
