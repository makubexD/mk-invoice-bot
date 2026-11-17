"""Integration tests for the key/value cache (R-4.3)."""

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.store.kv import get, put

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: The key the read-back and replacement tests use.
KEY = "test_kv_key"


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def empty_kv(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> AsyncIterator[None]:
    """Start each test from an empty ``kv`` table."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM kv")
    yield


async def test_put_value_reads_back(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.3: a value put with a ttl is read back before it expires."""
    async with db_pool.connection() as conn:
        await put(conn, KEY, "cached", timedelta(hours=1))
        found = await get(conn, KEY)

    assert found == "cached"


async def test_expired_value_reads_none(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.3: a value whose expiry has passed reads None."""
    async with db_pool.connection() as conn:
        await put(conn, KEY, "stale", timedelta(seconds=-1))
        found = await get(conn, KEY)

    assert found is None


async def test_second_put_replaces_first(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.3: a second put for the same key replaces the first value."""
    async with db_pool.connection() as conn:
        await put(conn, KEY, "first", timedelta(hours=1))
        await put(conn, KEY, "second", timedelta(hours=1))
        found = await get(conn, KEY)

    assert found == "second"
