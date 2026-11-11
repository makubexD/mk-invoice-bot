"""Async Postgres connection pool and database health ping."""

import asyncio
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

Pool = AsyncConnectionPool[AsyncConnection[Any]]

PING_TIMEOUT_S = 2.0


def create_pool(dsn: str) -> Pool:
    """Create a closed async pool with one to five connections."""
    return AsyncConnectionPool(dsn, open=False, min_size=1, max_size=5)


async def ping(pool: Pool) -> bool:
    """Return True when SELECT 1 succeeds within two seconds; never raise."""
    try:
        async with asyncio.timeout(PING_TIMEOUT_S):
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")
    except Exception:  # any failure, including a timeout, means the DB is down
        return False
    return True
