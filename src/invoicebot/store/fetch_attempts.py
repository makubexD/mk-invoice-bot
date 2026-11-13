"""Fetch-attempt persistence for the invoice fetch pipeline (R-5.7).

Every function takes the caller's connection and never commits: the caller
owns the transaction.
"""

from typing import Any

from psycopg import AsyncConnection

#: The FetchReason.OK value; the store does not import invoicebot.fetchers.
OK_REASON = "ok"


async def record_attempt(
    conn: AsyncConnection[Any],
    invoice_id: int,
    fetcher: str,
    portal_key: str | None,
    reason: str,
    ms: int,
) -> None:
    """Insert one fetch attempt, marking it successful only for "ok" (R-5.7)."""
    await conn.execute(
        """
        INSERT INTO fetch_attempts (invoice_id, fetcher, portal_key, ok, reason, ms)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (invoice_id, fetcher, portal_key, reason == OK_REASON, reason, ms),
    )


async def list_attempts(conn: AsyncConnection[Any], invoice_id: int) -> list[tuple[str, str]]:
    """Return the invoice's ``(fetcher, reason)`` attempts in insertion order (R-5.7)."""
    cur = await conn.execute(
        """
        SELECT fetcher, reason
        FROM fetch_attempts
        WHERE invoice_id = %s
        ORDER BY id
        """,
        (invoice_id,),
    )
    rows = await cur.fetchall()
    return [(str(row[0]), str(row[1])) for row in rows]
