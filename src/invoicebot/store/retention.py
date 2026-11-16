"""Per-table retention: null raw message payloads and delete expired rows (S15.1, R-9.12).

``invoices`` are never touched. The caller owns the transaction and logs the
returned counts once; this module never commits and never logs.
"""

from typing import Any

from psycopg import AsyncConnection

from invoicebot.config.models import RetentionDays

_NULL_MESSAGE_PAYLOADS = """
    UPDATE messages
    SET payload_json = NULL
    WHERE received_at < now() - make_interval(days => %s)
      AND payload_json IS NOT NULL
"""
#: Pending rows are still waiting to be sent, so they outlive their window.
_DELETE_OUTBOX = """
    DELETE FROM outbox
    WHERE created_at < now() - make_interval(days => %s)
      AND state <> 'pending'
"""
_DELETE_EVENTS = "DELETE FROM events WHERE created_at < now() - make_interval(days => %s)"
_DELETE_JOBS = "DELETE FROM jobs WHERE created_at < now() - make_interval(days => %s)"


async def _execute(conn: AsyncConnection[Any], statement: str, days: int) -> int:
    """Run one retention statement for a window of ``days`` and return its row count."""
    cur = await conn.execute(statement, (days,))
    return cur.rowcount


async def prune(conn: AsyncConnection[Any], days: RetentionDays) -> dict[str, int]:
    """Apply R-9.12's retention windows on the caller's connection.

    Returns the number of rows changed or deleted per table, keyed ``messages``,
    ``outbox``, ``events`` and ``jobs``. ``messages`` rows are kept with their
    payload nulled; pending outbox rows are kept whatever their age. Does not
    commit.
    """
    return {
        "messages": await _execute(conn, _NULL_MESSAGE_PAYLOADS, days.messages_payload),
        "outbox": await _execute(conn, _DELETE_OUTBOX, days.outbox),
        "events": await _execute(conn, _DELETE_EVENTS, days.events),
        "jobs": await _execute(conn, _DELETE_JOBS, days.jobs),
    }
