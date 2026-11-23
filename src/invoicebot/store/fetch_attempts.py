"""Fetch-attempt persistence for the invoice fetch pipeline (R-5.7).

Every function takes the caller's connection and never commits: the caller
owns the transaction.
"""

from typing import Any

from psycopg import AsyncConnection

#: The FetchReason.OK value; the store does not import invoicebot.fetchers.
OK_REASON = "ok"
#: The FetchReason values BR-F03 counts as a technical failure (spec 06 §3); keep in step with
#: ``fetchers.base.TECHNICAL_REASONS``, which the store may not import.
_TECHNICAL_REASONS = frozenset({"technical", "timeout"})


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


async def technical_streak(conn: AsyncConnection[Any], portal_key: str, n: int) -> bool:
    """Return True when the portal's newest ``n`` attempts all failed technically (BR-F03).

    ``n`` rows must exist and every one must be ``technical`` or ``timeout``; a
    portal with fewer attempts has no streak. Does not commit.
    """
    if n < 1:
        return False
    cur = await conn.execute(
        """
        SELECT reason
        FROM fetch_attempts
        WHERE portal_key = %s
        ORDER BY at DESC, id DESC
        LIMIT %s
        """,
        (portal_key, n),
    )
    rows = await cur.fetchall()
    return len(rows) == n and all(row[0] in _TECHNICAL_REASONS for row in rows)


async def list_attempts(conn: AsyncConnection[Any], invoice_id: int) -> list[tuple[str, str]]:
    """Return the invoice's current-round ``(fetcher, reason)`` attempts in insertion order (R-5.7, D166).

    A fetch round starts at ``invoices.fetch_round_at``; every attempt at or after it
    belongs to the round, and an invoice with no round keeps every attempt.
    """
    cur = await conn.execute(
        """
        SELECT a.fetcher, a.reason
        FROM fetch_attempts AS a
        JOIN invoices AS i ON i.id = a.invoice_id
        WHERE a.invoice_id = %s
          AND (i.fetch_round_at IS NULL OR a.at >= i.fetch_round_at)
        ORDER BY a.id
        """,
        (invoice_id,),
    )
    rows = await cur.fetchall()
    return [(str(row[0]), str(row[1])) for row in rows]
