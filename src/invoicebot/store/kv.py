"""The key/value cache: a value with an optional expiry (R-4.3).

The ``kv`` table is UNLOGGED and only holds rebuildable values, such as the
SUNAT token. Every function takes the caller's connection and never commits:
the caller owns the transaction. Expiry uses the database clock, never Python's.
"""

from datetime import timedelta
from typing import Any, cast

from psycopg import AsyncConnection

#: The read the primary key serves; an expired value reads as absent (R-4.3).
GET_SQL = """
    SELECT value
    FROM kv
    WHERE key = %s AND (expires_at IS NULL OR expires_at > now())
"""

#: The upsert, with the expiry computed on the database clock (R-4.3).
PUT_SQL = """
    INSERT INTO kv (key, value, expires_at)
    VALUES (%s, %s, now() + %s)
    ON CONFLICT (key) DO UPDATE
    SET value = EXCLUDED.value, expires_at = EXCLUDED.expires_at
"""

#: The delete that drops a cached value, whether or not it was there (R-4.3).
DELETE_SQL = """
    DELETE FROM kv
    WHERE key = %s
"""


async def get(conn: AsyncConnection[Any], key: str) -> str | None:
    """Return the live value for ``key``, or None when absent or expired (R-4.3).

    Does not commit.
    """
    cur = await conn.execute(GET_SQL, (key,))
    row = await cur.fetchone()
    return None if row is None else cast(str, row[0])


async def put(conn: AsyncConnection[Any], key: str, value: str, ttl: timedelta | None) -> None:
    """Store ``value`` under ``key``, replacing any existing entry (R-4.3).

    A ``ttl`` of None stores no expiry; the expiry is ``now() + ttl`` on the
    database clock. Does not commit.
    """
    await conn.execute(PUT_SQL, (key, value, ttl))


async def delete(conn: AsyncConnection[Any], key: str) -> None:
    """Remove ``key`` from the cache, whether or not it was there (R-4.3).

    Does not commit.
    """
    await conn.execute(DELETE_SQL, (key,))
