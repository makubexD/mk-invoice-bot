"""Integration tests for the outbox: transactional writes and delivery (R-1.8, R-11.5)."""

from typing import Any

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.queue.outbox import OutboxRow, OutboxSender, add

pytestmark = pytest.mark.asyncio(loop_scope="session")

TRANSPORT_CHANNELS = ("whatsapp", "telegram", "github", "event")


async def _clear_outbox(conn: AsyncConnection[Any]) -> None:
    """Remove every outbox row so tests cannot depend on each other's leftovers."""
    await conn.execute("DELETE FROM outbox")


async def _fetch_row(conn: AsyncConnection[Any], outbox_id: int) -> tuple[Any, ...] | None:
    """Return ``(state, attempts, last_error, sent_at)`` for one outbox row."""
    cur = await conn.execute(
        "SELECT state, attempts, last_error, sent_at FROM outbox WHERE id = %s",
        (outbox_id,),
    )
    return await cur.fetchone()


async def test_row_from_a_rolled_back_transaction_is_never_sent(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.8: an outbox row written inside a rolled-back transaction is not delivered."""
    delivered: list[OutboxRow] = []

    async def transport(row: OutboxRow) -> None:
        delivered.append(row)

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        with pytest.raises(RuntimeError):
            async with conn.transaction():
                await add(conn, "whatsapp", "+51900000001", {"text": "hola"})
                raise RuntimeError("rollback")

    sender = OutboxSender(db_pool, {"whatsapp": transport})
    assert await sender.send_pending() == 0
    assert delivered == []

    async with db_pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM outbox")
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 0


async def test_committed_row_is_sent_once(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.8: a committed outbox row is delivered once, then nothing else is due."""
    delivered: list[OutboxRow] = []

    async def transport(row: OutboxRow) -> None:
        delivered.append(row)

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        outbox_id = await add(conn, "telegram", "42", {"text": "hola"})

    sender = OutboxSender(db_pool, {"telegram": transport})
    assert await sender.send_pending() == 1
    assert [row.id for row in delivered] == [outbox_id]
    assert await sender.send_pending() == 0
    assert len(delivered) == 1

    async with db_pool.connection() as conn:
        row = await _fetch_row(conn, outbox_id)
    assert row is not None
    assert row[0] == "sent"
    assert row[3] is not None


async def test_failure_doubles_the_retry_delay(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.5: a failure leaves the row pending and backs off ~2 then ~4 minutes."""
    attempts_seen = 0

    async def transport(row: OutboxRow) -> None:
        nonlocal attempts_seen
        attempts_seen += 1
        raise RuntimeError("boom")

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        outbox_id = await add(conn, "whatsapp", "+51900000002", {"text": "hola"})

    sender = OutboxSender(db_pool, {"whatsapp": transport})
    assert await sender.send_pending() == 0

    async with db_pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT state, attempts, last_error,
                   run_after >= now() + interval '1 minute',
                   run_after <= now() + interval '3 minutes'
            FROM outbox
            WHERE id = %s
            """,
            (outbox_id,),
        )
        first = await cur.fetchone()
        # Make the row due again so the next failure is scheduled immediately.
        await conn.execute("UPDATE outbox SET run_after = now() WHERE id = %s", (outbox_id,))

    assert first is not None
    state, attempts, last_error, after_one, before_three = first
    assert state == "pending"
    assert attempts == 1
    assert last_error == "RuntimeError: boom"
    assert after_one is True
    assert before_three is True

    assert await sender.send_pending() == 0
    assert attempts_seen == 2

    async with db_pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT state, attempts,
                   run_after >= now() + interval '3 minutes',
                   run_after <= now() + interval '5 minutes'
            FROM outbox
            WHERE id = %s
            """,
            (outbox_id,),
        )
        second = await cur.fetchone()
    assert second is not None
    state, attempts, after_three, before_five = second
    assert state == "pending"
    assert attempts == 2
    assert after_three is True
    assert before_five is True


async def test_row_becomes_dead_after_max_attempts(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.5: the row is marked dead at max_attempts and is never picked again."""
    calls = 0

    async def transport(row: OutboxRow) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        outbox_id = await add(conn, "github", "org/repo", {"issue": 1})

    sender = OutboxSender(db_pool, {"github": transport}, max_attempts=3)
    for _ in range(3):
        assert await sender.send_pending() == 0
        async with db_pool.connection() as conn:
            await conn.execute(
                """
                UPDATE outbox SET run_after = now()
                WHERE id = %s AND state = 'pending'
                """,
                (outbox_id,),
            )

    assert calls == 3
    async with db_pool.connection() as conn:
        row = await _fetch_row(conn, outbox_id)
    assert row is not None
    assert row[0] == "dead"
    assert row[1] == 3

    assert await sender.send_pending() == 0
    assert calls == 3


async def test_each_channel_routes_to_its_own_transport(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.5: whatsapp, telegram, github and event each dispatch to their own transport."""
    delivered: dict[str, list[OutboxRow]] = {channel: [] for channel in TRANSPORT_CHANNELS}

    def make_transport(channel: str) -> Any:
        async def transport(row: OutboxRow) -> None:
            delivered[channel].append(row)

        return transport

    recipients = {
        "whatsapp": "+51900000003",
        "telegram": "42",
        "github": "org/repo",
        "event": "https://example.test/hook",
    }
    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        ids = {
            channel: await add(conn, channel, recipient, {"channel": channel})
            for channel, recipient in recipients.items()
        }

    sender = OutboxSender(db_pool, {channel: make_transport(channel) for channel in TRANSPORT_CHANNELS})
    assert await sender.send_pending() == len(TRANSPORT_CHANNELS)

    for channel, rows in delivered.items():
        assert [row.id for row in rows] == [ids[channel]]
        assert rows[0].channel == channel
        assert rows[0].to == recipients[channel]


async def test_unknown_channel_is_rejected_at_add(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.8: add rejects a channel outside the four known ones before touching the database."""
    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        with pytest.raises(ValueError):
            await add(conn, "email", "owner@example.test", {"text": "hola"})
        cur = await conn.execute("SELECT count(*) FROM outbox")
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 0


async def test_missing_transport_is_recorded_as_a_failure(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.5: a channel without a registered transport fails and reschedules the row."""
    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        outbox_id = await add(conn, "whatsapp", "+51900000004", {"text": "hola"})

    sender = OutboxSender(db_pool, {})
    assert await sender.send_pending() == 0

    async with db_pool.connection() as conn:
        row = await _fetch_row(conn, outbox_id)
    assert row is not None
    assert row[0] == "pending"
    assert row[1] == 1
    assert row[2] == "no transport for channel whatsapp"


async def test_failing_row_does_not_block_a_later_row(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.5: a failed row is rescheduled and the next due row is still delivered."""
    delivered: list[int] = []

    async def failing(row: OutboxRow) -> None:
        raise RuntimeError("boom")

    async def working(row: OutboxRow) -> None:
        delivered.append(row.id)

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        failing_id = await add(conn, "whatsapp", "+51900000005", {"seq": 1})
        working_id = await add(conn, "telegram", "42", {"seq": 2})

    sender = OutboxSender(db_pool, {"whatsapp": failing, "telegram": working})
    assert await sender.send_pending() == 1
    assert delivered == [working_id]

    async with db_pool.connection() as conn:
        cur = await conn.execute("SELECT id, state, attempts FROM outbox ORDER BY id")
        rows = await cur.fetchall()
    assert rows == [(failing_id, "pending", 1), (working_id, "sent", 0)]
