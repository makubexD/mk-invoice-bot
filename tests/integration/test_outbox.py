"""Integration tests for the outbox: transactional writes and delivery (R-1.8, R-11.5)."""

import logging
from datetime import timedelta
from typing import Any

import pytest
from psycopg import AsyncConnection
from psycopg.errors import ForeignKeyViolation
from psycopg_pool import AsyncConnectionPool

from invoicebot.queue.outbox import (
    OutboxRow,
    OutboxSender,
    PermanentSendError,
    SendLater,
    add,
    last_rule,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

TRANSPORT_CHANNELS = ("whatsapp", "telegram", "github", "event")
#: No identity reaches this, so an outbox row cannot reference it.
MISSING_INVOICE_ID = 2**31 - 1


async def _clear_outbox(conn: AsyncConnection[Any]) -> None:
    """Remove every outbox row so tests cannot depend on each other's leftovers."""
    await conn.execute("DELETE FROM outbox")


async def _insert_invoice(conn: AsyncConnection[Any]) -> int:
    """Insert a minimal invoice and return its id."""
    cur = await conn.execute(
        "INSERT INTO invoices (profile_id, origin_channel, state) VALUES (%s, %s, %s) RETURNING id",
        ("owner", "whatsapp", "parsed"),
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _fetch_row(conn: AsyncConnection[Any], outbox_id: int) -> tuple[Any, ...] | None:
    """Return ``(state, attempts, last_error, sent_at)`` for one outbox row."""
    cur = await conn.execute(
        "SELECT state, attempts, last_error, sent_at FROM outbox WHERE id = %s",
        (outbox_id,),
    )
    return await cur.fetchone()


async def _fetch_channel_message_id(conn: AsyncConnection[Any], outbox_id: int) -> str | None:
    """Return the channel's message id stored for one outbox row."""
    cur = await conn.execute("SELECT channel_message_id FROM outbox WHERE id = %s", (outbox_id,))
    row = await cur.fetchone()
    assert row is not None
    return row[0]


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


async def test_sent_row_stores_the_channel_message_id(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D102: a transport that returns a channel message id leaves it on the sent row."""

    async def transport(row: OutboxRow) -> str | None:
        return "wamid.X"

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        outbox_id = await add(conn, "whatsapp", "+51900000011", {"text": "hola"})

    sender = OutboxSender(db_pool, {"whatsapp": transport})
    assert await sender.send_pending() == 1

    async with db_pool.connection() as conn:
        row = await _fetch_row(conn, outbox_id)
        channel_message_id = await _fetch_channel_message_id(conn, outbox_id)
    assert row is not None
    assert row[0] == "sent"
    assert channel_message_id == "wamid.X"


async def test_sent_row_without_a_channel_message_id_keeps_it_null(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D102: a transport that returns None leaves channel_message_id NULL."""

    async def transport(row: OutboxRow) -> str | None:
        return None

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        outbox_id = await add(conn, "telegram", "42", {"text": "hola"})

    sender = OutboxSender(db_pool, {"telegram": transport})
    assert await sender.send_pending() == 1

    async with db_pool.connection() as conn:
        row = await _fetch_row(conn, outbox_id)
        channel_message_id = await _fetch_channel_message_id(conn, outbox_id)
    assert row is not None
    assert row[0] == "sent"
    assert channel_message_id is None


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
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R-11.5: the row is marked dead at max_attempts and is never picked again."""
    caplog.set_level(logging.ERROR)
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
    assert "(permanent)" not in caplog.text

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
        with pytest.raises(ValueError, match="unknown outbox channel"):
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


async def test_permanent_failure_marks_the_row_dead_on_the_first_attempt(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.5: a PermanentSendError kills the row at once, whatever max_attempts."""
    calls = 0

    async def transport(row: OutboxRow) -> None:
        nonlocal calls
        calls += 1
        raise PermanentSendError("no number")

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        outbox_id = await add(conn, "whatsapp", "+51900000006", {"text": "hola"})

    sender = OutboxSender(db_pool, {"whatsapp": transport}, max_attempts=3)
    assert await sender.send_pending() == 0

    async with db_pool.connection() as conn:
        row = await _fetch_row(conn, outbox_id)
    assert row is not None
    assert row[0] == "dead"
    assert row[1] == 1
    assert row[2] == "PermanentSendError: no number"

    assert await sender.send_pending() == 0
    assert calls == 1


async def test_a_permanent_dead_row_logs_as_permanent(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R-9.16: a permanent failure logs its dead-row line marked as permanent."""
    caplog.set_level(logging.ERROR)

    async def transport(row: OutboxRow) -> None:
        raise PermanentSendError("no number")

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        await add(conn, "whatsapp", "+51900000006", {"text": "hola"})

    sender = OutboxSender(db_pool, {"whatsapp": transport}, max_attempts=3)
    assert await sender.send_pending() == 0

    assert "is dead (permanent) after 1 attempts" in caplog.text


async def test_transient_failure_still_reschedules_the_row(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.5: a RuntimeError leaves the row pending, attempts 1 and rescheduled."""

    async def transport(row: OutboxRow) -> None:
        raise RuntimeError("boom")

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        outbox_id = await add(conn, "whatsapp", "+51900000007", {"text": "hola"})

    sender = OutboxSender(db_pool, {"whatsapp": transport})
    assert await sender.send_pending() == 0

    async with db_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT state, attempts, last_error, run_after > now() FROM outbox WHERE id = %s",
            (outbox_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "pending"
    assert row[1] == 1
    assert row[2] == "RuntimeError: boom"
    assert row[3] is True


async def test_permanent_failure_after_a_transient_one_marks_the_row_dead(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.5: a PermanentSendError on the second attempt kills the row with attempts 2."""
    calls = 0

    async def transport(row: OutboxRow) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        raise PermanentSendError("no number")

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        outbox_id = await add(conn, "whatsapp", "+51900000008", {"text": "hola"})

    sender = OutboxSender(db_pool, {"whatsapp": transport})
    assert await sender.send_pending() == 0
    async with db_pool.connection() as conn:
        await conn.execute("UPDATE outbox SET run_after = now() WHERE id = %s", (outbox_id,))

    assert await sender.send_pending() == 0

    async with db_pool.connection() as conn:
        row = await _fetch_row(conn, outbox_id)
    assert row is not None
    assert row[0] == "dead"
    assert row[1] == 2
    assert row[2] == "PermanentSendError: no number"
    assert calls == 2


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


async def test_add_stores_the_invoice_it_talks_about(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.7: add stores the invoice id so a parked invoice's question can be re-sent."""
    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        invoice_id = await _insert_invoice(conn)
        outbox_id = await add(conn, "whatsapp", "+51900000009", {"text": "hola"}, invoice_id=invoice_id)

    async with db_pool.connection() as conn:
        cur = await conn.execute("SELECT invoice_id FROM outbox WHERE id = %s", (outbox_id,))
        row = await cur.fetchone()
    assert row == (invoice_id,)


async def test_add_without_an_invoice_leaves_the_column_null(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.7: a message that names no invoice stores a NULL invoice_id."""
    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        outbox_id = await add(conn, "telegram", "42", {"text": "hola"})

    async with db_pool.connection() as conn:
        cur = await conn.execute("SELECT invoice_id FROM outbox WHERE id = %s", (outbox_id,))
        row = await cur.fetchone()
    assert row == (None,)


async def test_add_with_an_unknown_invoice_is_rejected(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.7: an outbox row cannot name an invoice that does not exist."""
    async with db_pool.connection() as conn:
        await _clear_outbox(conn)

    with pytest.raises(ForeignKeyViolation):
        async with db_pool.connection() as conn:
            await add(conn, "whatsapp", "+51900000010", {"text": "hola"}, invoice_id=MISSING_INVOICE_ID)

    async with db_pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM outbox")
        row = await cur.fetchone()
    assert row == (0,)


async def test_last_rule_returns_the_newest_rule_stored(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S02: last_rule returns the newest rule id stored for the invoice."""
    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        invoice_id = await _insert_invoice(conn)
        await add(conn, "whatsapp", "+51900000011", {"rule_id": "BR-D11"}, invoice_id=invoice_id)
        await add(conn, "whatsapp", "+51900000011", {"rule_id": "BR-D10"}, invoice_id=invoice_id)

    async with db_pool.connection() as conn:
        rule = await last_rule(conn, invoice_id, "BR-S02")

    assert rule == "BR-D10"


async def test_last_rule_skips_the_ignored_rule(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S02: last_rule skips BR-S02 so the invoice's last real question wins."""
    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        invoice_id = await _insert_invoice(conn)
        await add(conn, "whatsapp", "+51900000012", {"rule_id": "BR-D11"}, invoice_id=invoice_id)
        await add(conn, "whatsapp", "+51900000012", {"rule_id": "BR-S02"}, invoice_id=invoice_id)

    async with db_pool.connection() as conn:
        rule = await last_rule(conn, invoice_id, "BR-S02")

    assert rule == "BR-D11"


async def test_last_rule_without_a_stored_rule_returns_none(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S02: an invoice with no stored rule id has no last rule."""
    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        invoice_id = await _insert_invoice(conn)

    async with db_pool.connection() as conn:
        rule = await last_rule(conn, invoice_id, "BR-S02")

    assert rule is None


async def test_send_later_reschedules_the_row_without_an_attempt(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.10: a SendLater leaves the row pending with the reason and a one-hour delay."""

    async def transport(row: OutboxRow) -> None:
        raise SendLater(timedelta(hours=1), "budget")

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        outbox_id = await add(conn, "github", "org/repo", {"issue": 1})

    sender = OutboxSender(db_pool, {"github": transport})
    assert await sender.send_pending() == 0

    async with db_pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT state, attempts, last_error,
                   run_after >= now() + interval '59 minutes',
                   run_after <= now() + interval '61 minutes'
            FROM outbox
            WHERE id = %s
            """,
            (outbox_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    state, attempts, last_error, after_59, before_61 = row
    assert state == "pending"
    assert attempts == 0
    assert last_error == "budget"
    assert after_59 is True
    assert before_61 is True


async def test_send_later_row_is_not_picked_again_before_run_after(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.10: a row rescheduled by SendLater is not sent again before run_after."""
    calls = 0

    async def transport(row: OutboxRow) -> None:
        nonlocal calls
        calls += 1
        raise SendLater(timedelta(hours=1), "budget")

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        await add(conn, "github", "org/repo", {"issue": 1})

    sender = OutboxSender(db_pool, {"github": transport})
    assert await sender.send_pending() == 0
    assert await sender.send_pending() == 0
    assert calls == 1


async def test_send_later_never_lets_the_row_go_dead(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.10: twenty SendLater reschedules keep the row pending with attempts 0."""
    calls = 0

    async def transport(row: OutboxRow) -> None:
        nonlocal calls
        calls += 1
        raise SendLater(timedelta(hours=1), "budget")

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        outbox_id = await add(conn, "github", "org/repo", {"issue": 1})

    sender = OutboxSender(db_pool, {"github": transport}, max_attempts=3)
    for _ in range(20):
        assert await sender.send_pending() == 0
        async with db_pool.connection() as conn:
            await conn.execute(
                """
                UPDATE outbox SET run_after = now()
                WHERE id = %s AND state = 'pending'
                """,
                (outbox_id,),
            )

    assert calls == 20
    async with db_pool.connection() as conn:
        row = await _fetch_row(conn, outbox_id)
    assert row is not None
    assert row[0] == "pending"
    assert row[1] == 0
