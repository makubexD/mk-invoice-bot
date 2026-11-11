"""Integration tests for intake: storage, queueing, back-pressure and rollback.

Covers R-1.1, R-1.6, R-1.8, R-1.10 and BR-S01 against real Postgres.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import Attachment, ChannelName, InboundMessage
from invoicebot.pipeline.intake import handle_inbound
from invoicebot.queue.jobs import Job, claim

pytestmark = pytest.mark.asyncio(loop_scope="session")

MAX_QUEUED = 10
LEASE_S = 60


class _Rollback(Exception):
    """Test-local failure that makes the caller roll its transaction back."""


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


async def _counts(conn: AsyncConnection[Any], profile_id: str) -> tuple[int, int, int, int]:
    """Return (messages, invoices, jobs, outbox rows) owned by profile_id."""
    cur = await conn.execute("SELECT count(*) FROM messages WHERE profile_id = %s", (profile_id,))
    row = await cur.fetchone()
    assert row is not None
    messages = int(row[0])
    cur = await conn.execute("SELECT count(*) FROM invoices WHERE profile_id = %s", (profile_id,))
    row = await cur.fetchone()
    assert row is not None
    invoices = int(row[0])
    cur = await conn.execute(
        """
        SELECT count(*)
        FROM jobs AS j
        JOIN invoices AS i ON i.id = j.invoice_id
        WHERE i.profile_id = %s
        """,
        (profile_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    jobs = int(row[0])
    cur = await conn.execute('SELECT count(*) FROM outbox WHERE "to" = %s', (profile_id,))
    row = await cur.fetchone()
    assert row is not None
    return messages, invoices, jobs, int(row[0])


async def test_photo_creates_invoice_job_and_acknowledgement(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.1: a photo becomes a RECEIVED invoice, one job and one BR-S09 outbox row."""
    profile_id = _profile()
    msg = _message(profile_id, external_id=_external_id(), attachments=[_photo()])

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        assert result.status == "queued"
        invoice_id = result.invoice_id
        assert invoice_id is not None
        cur = await conn.execute(
            "SELECT profile_id, origin_channel, state FROM invoices WHERE id = %s",
            (invoice_id,),
        )
        invoice = await cur.fetchone()
        cur = await conn.execute("SELECT step FROM jobs WHERE invoice_id = %s", (invoice_id,))
        jobs = await cur.fetchall()
        cur = await conn.execute(
            "SELECT state, payload_json->>'rule_id', payload_json->>'text' FROM outbox WHERE \"to\" = %s",
            (profile_id,),
        )
        outbox_rows = await cur.fetchall()
        cur = await conn.execute(
            "SELECT invoice_id FROM messages WHERE channel = %s AND external_id = %s",
            (msg.channel, msg.external_id),
        )
        linked = await cur.fetchone()

    assert invoice == (profile_id, "whatsapp", "RECEIVED")
    assert jobs == [("download_media",)]
    assert len(outbox_rows) == 1
    state, rule_id, text = outbox_rows[0]
    assert state == "pending"
    assert rule_id == "BR-S09"
    assert str(invoice_id) in text
    assert linked == (invoice_id,)


async def test_redelivered_message_is_a_duplicate(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.8: the same (channel, external_id) twice stores one invoice, job and reply."""
    profile_id = _profile()
    msg = _message(profile_id, external_id=_external_id(), attachments=[_photo()])

    async with db_pool.connection() as conn:
        first = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        second = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        counts = await _counts(conn, profile_id)

    assert first.status == "queued"
    assert second.status == "duplicate"
    assert second.invoice_id is None
    assert counts == (1, 1, 1, 1)


async def test_text_message_is_only_stored(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """R-1.10: a non-photo is stored and opens the window, with no invoice, job or reply."""
    profile_id = _profile()
    msg = _message(profile_id, external_id=_external_id(), text="hola")

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        counts = await _counts(conn, profile_id)
        cur = await conn.execute(
            "SELECT last_inbound_at FROM channel_windows WHERE channel = %s AND recipient = %s",
            (msg.channel, profile_id),
        )
        window = await cur.fetchone()

    assert result.status == "stored"
    assert result.invoice_id is None
    assert counts == (1, 0, 0, 0)
    assert window is not None
    assert window[0] == msg.received_at


async def test_five_photos_are_claimed_in_arrival_order(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.6: five photos make five invoices whose jobs are claimed in arrival order."""
    profile_id = _profile()
    messages = [_message(profile_id, external_id=_external_id(), attachments=[_photo()]) for _ in range(5)]

    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM jobs")
        results = [await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED) for msg in messages]
        claimed: list[Job] = []
        for _ in range(5):
            job = await claim(conn, "worker-intake", LEASE_S)
            assert job is not None
            claimed.append(job)

    assert [result.status for result in results] == ["queued"] * 5
    assert [job.invoice_id for job in claimed] == [result.invoice_id for result in results]
    assert {job.step for job in claimed} == {"download_media"}


async def test_back_pressure_replies_and_keeps_the_message(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S01: a third photo with two active jobs is stored but answered with BR-S01."""
    profile_id = _profile()
    messages = [_message(profile_id, external_id=_external_id(), attachments=[_photo()]) for _ in range(3)]

    async with db_pool.connection() as conn:
        results = [await handle_inbound(conn, msg, max_queued_per_sender=2) for msg in messages]
        counts = await _counts(conn, profile_id)
        cur = await conn.execute(
            """
            SELECT payload_json->>'rule_id', payload_json->>'text'
            FROM outbox
            WHERE "to" = %s
            ORDER BY id
            """,
            (profile_id,),
        )
        outbox_rows = await cur.fetchall()

    assert [result.status for result in results] == ["queued", "queued", "back_pressure"]
    assert results[2].invoice_id is None
    assert counts == (3, 2, 2, 3)
    back_pressure = [text for rule_id, text in outbox_rows if rule_id == "BR-S01"]
    assert len(back_pressure) == 1
    assert "2" in back_pressure[0]


async def test_back_pressure_is_per_profile(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S01: one profile's full queue does not stop another profile's photo."""
    busy_profile = _profile()
    other_profile = _profile()

    async with db_pool.connection() as conn:
        first = await handle_inbound(
            conn,
            _message(busy_profile, external_id=_external_id(), attachments=[_photo()]),
            max_queued_per_sender=1,
        )
        blocked = await handle_inbound(
            conn,
            _message(busy_profile, external_id=_external_id(), attachments=[_photo()]),
            max_queued_per_sender=1,
        )
        other = await handle_inbound(
            conn,
            _message(other_profile, external_id=_external_id(), attachments=[_photo()]),
            max_queued_per_sender=1,
        )
        busy_counts = await _counts(conn, busy_profile)
        other_counts = await _counts(conn, other_profile)

    assert first.status == "queued"
    assert blocked.status == "back_pressure"
    assert other.status == "queued"
    assert other.invoice_id is not None
    assert busy_counts == (2, 1, 1, 2)
    assert other_counts == (1, 1, 1, 1)


async def test_caller_rollback_leaves_nothing_behind(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.8: a caller failure after intake rolls back message, invoice, job and reply."""
    profile_id = _profile()
    msg = _message(profile_id, external_id=_external_id(), attachments=[_photo()])

    async with db_pool.connection() as conn:
        with pytest.raises(_Rollback):
            async with conn.transaction():
                result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
                assert result.status == "queued"
                raise _Rollback
        counts = await _counts(conn, profile_id)
        cur = await conn.execute("SELECT count(*) FROM messages WHERE external_id = %s", (msg.external_id,))
        row = await cur.fetchone()

    assert row is not None
    assert row[0] == 0
    assert counts == (0, 0, 0, 0)
