"""Integration tests for per-table retention (R-9.12, S15.1)."""

from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection, sql
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from invoicebot.config.models import RetentionDays
from invoicebot.store.retention import prune

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: The windows config/example.yaml uses (R-9.12).
DAYS = RetentionDays(messages_payload=30, outbox=90, events=90, jobs=90)
#: One day past the longest window: old for every statement.
OLD = 91
#: Well inside every window: young for every statement.
YOUNG = 1


async def _clear(conn: AsyncConnection[Any]) -> None:
    """Empty the pruned tables in the caller's transaction, which the test rolls back."""
    await conn.execute("DELETE FROM outbox")
    await conn.execute("DELETE FROM events")
    await conn.execute("DELETE FROM jobs")
    await conn.execute("DELETE FROM messages")


async def _insert_invoice(conn: AsyncConnection[Any]) -> int:
    """Insert an invoice for the jobs foreign key and return its id."""
    cur = await conn.execute(
        "INSERT INTO invoices (profile_id, origin_channel, state) VALUES (%s, 'whatsapp', 'MANUAL_REQUIRED') "
        "RETURNING id",
        (f"profile-{uuid4().hex}",),
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _insert_message(conn: AsyncConnection[Any], age_days: int, payload: dict[str, Any]) -> str:
    """Insert a message ``age_days`` old and return its external id."""
    external_id = f"ext-{uuid4().hex}"
    await conn.execute(
        """
        INSERT INTO messages (channel, external_id, profile_id, kind, received_at, payload_json)
        VALUES ('whatsapp', %s, %s, 'text', now() - make_interval(days => %s), %s)
        """,
        (external_id, f"profile-{uuid4().hex}", age_days, Jsonb(payload)),
    )
    return external_id


async def _insert_outbox(conn: AsyncConnection[Any], age_days: int, state: str) -> int:
    """Insert an outbox row ``age_days`` old in ``state`` and return its id."""
    cur = await conn.execute(
        """
        INSERT INTO outbox (channel, "to", payload_json, state, created_at)
        VALUES ('whatsapp', %s, %s, %s, now() - make_interval(days => %s))
        RETURNING id
        """,
        ("51900000000", Jsonb({}), state, age_days),
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _insert_event(conn: AsyncConnection[Any], age_days: int) -> int:
    """Insert an event ``age_days`` old and return its id."""
    cur = await conn.execute(
        """
        INSERT INTO events (type, version, payload_json, created_at)
        VALUES ('invoice.created', 1, %s, now() - make_interval(days => %s))
        RETURNING id
        """,
        (Jsonb({}), age_days),
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _insert_job(conn: AsyncConnection[Any], invoice_id: int, age_days: int) -> int:
    """Insert a job for ``invoice_id`` ``age_days`` old and return its id."""
    cur = await conn.execute(
        """
        INSERT INTO jobs (invoice_id, step, created_at)
        VALUES (%s, 'fetch', now() - make_interval(days => %s))
        RETURNING id
        """,
        (invoice_id, age_days),
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _exists(conn: AsyncConnection[Any], table: str, row_id: int) -> bool:
    """Report whether ``table`` still has the row with ``row_id``."""
    query = sql.SQL("SELECT 1 FROM {} WHERE id = %s").format(sql.Identifier(table))
    cur = await conn.execute(query, (row_id,))
    return await cur.fetchone() is not None


async def test_prune_ages_each_table_by_its_window(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.12 rows older than their window go and younger rows stay, with exact counts."""
    async with db_pool.connection() as conn:
        try:
            await _clear(conn)
            invoice_id = await _insert_invoice(conn)
            invoices_before = await _invoice_count(conn)

            old_message = await _insert_message(conn, OLD, {"text": "old"})
            young_message = await _insert_message(conn, YOUNG, {"text": "young"})
            old_sent = await _insert_outbox(conn, OLD, "sent")
            young_sent = await _insert_outbox(conn, YOUNG, "sent")
            pending_old = await _insert_outbox(conn, OLD, "pending")
            old_event = await _insert_event(conn, OLD)
            young_event = await _insert_event(conn, YOUNG)
            old_job = await _insert_job(conn, invoice_id, OLD)
            young_job = await _insert_job(conn, invoice_id, YOUNG)

            counts = await prune(conn, DAYS)

            assert counts == {"messages": 1, "outbox": 1, "events": 1, "jobs": 1}

            old_payload = await _message_payload(conn, old_message)
            assert old_payload is None
            assert await _message_payload(conn, young_message) == {"text": "young"}
            assert not await _exists(conn, "outbox", old_sent)
            assert await _exists(conn, "outbox", young_sent)
            assert await _exists(conn, "outbox", pending_old)
            assert not await _exists(conn, "events", old_event)
            assert await _exists(conn, "events", young_event)
            assert not await _exists(conn, "jobs", old_job)
            assert await _exists(conn, "jobs", young_job)
            assert await _invoice_count(conn) == invoices_before
        finally:
            await conn.rollback()


async def test_prune_keeps_a_pending_outbox_row_past_its_window(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.12 a pending outbox row older than 90 days stays, and is not counted."""
    async with db_pool.connection() as conn:
        try:
            await _clear(conn)
            pending = await _insert_outbox(conn, OLD, "pending")

            counts = await prune(conn, DAYS)

            assert await _exists(conn, "outbox", pending)
            assert counts["outbox"] == 0
        finally:
            await conn.rollback()


async def test_prune_leaves_invoices_untouched(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.12 invoices are kept: their count is the same after pruning."""
    async with db_pool.connection() as conn:
        try:
            await _clear(conn)
            await _insert_invoice(conn)
            before = await _invoice_count(conn)

            await prune(conn, DAYS)

            assert await _invoice_count(conn) == before
        finally:
            await conn.rollback()


async def _message_payload(conn: AsyncConnection[Any], external_id: str) -> Any:
    """Return a message's ``payload_json`` (``None`` once nulled)."""
    cur = await conn.execute(
        "SELECT payload_json FROM messages WHERE channel = 'whatsapp' AND external_id = %s",
        (external_id,),
    )
    row = await cur.fetchone()
    assert row is not None, "the messages row must be kept, only its payload is nulled"
    return row[0]


async def _invoice_count(conn: AsyncConnection[Any]) -> int:
    """Return how many invoices the table holds."""
    cur = await conn.execute("SELECT count(*) FROM invoices")
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])
