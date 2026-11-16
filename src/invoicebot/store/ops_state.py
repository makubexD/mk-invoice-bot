"""Operations rate-limit state: one claim per key per window (BR-O01, BR-D05).

``claim`` is an atomic check-and-stamp on the database clock, so two callers
racing for the same alert produce one send. Every function takes the caller's
connection and never commits: the caller owns the transaction.
"""

from datetime import datetime, timedelta
from typing import Any, cast

from psycopg import AsyncConnection


async def claim(conn: AsyncConnection[Any], key: str, every: timedelta) -> bool:
    """Stamp ``key`` with ``now()`` and report whether its window had elapsed (BR-O01).

    Returns True when the key is new or its last claim is older than ``every``.
    The check and the stamp are one statement, so concurrent callers get True
    for exactly one of them.
    """
    cur = await conn.execute(
        """
        INSERT INTO ops_state AS s (key, last_at)
        VALUES (%s, now())
        ON CONFLICT (key) DO UPDATE
        SET last_at = now()
        WHERE s.last_at < now() - %s
        RETURNING key
        """,
        (key, every),
    )
    return await cur.fetchone() is not None


async def last(conn: AsyncConnection[Any], key: str) -> datetime | None:
    """Return when ``key`` was last claimed, or None when it never was (BR-O01)."""
    cur = await conn.execute("SELECT last_at FROM ops_state WHERE key = %s", (key,))
    row = await cur.fetchone()
    return None if row is None else cast(datetime, row[0])
