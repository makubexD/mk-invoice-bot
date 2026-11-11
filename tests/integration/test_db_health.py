"""Integration tests for database health through /health (R-9.14)."""

from functools import partial
from typing import Any

import httpx
import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.app import create_app
from invoicebot.store.db import create_pool, ping

pytestmark = pytest.mark.asyncio(loop_scope="session")

CLOSED_PORT_DSN = "postgresql://invoicebot:invoicebot@127.0.0.1:1/invoicebot_test"


async def _get_health(app: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/health")


async def test_health_ok_when_database_is_up(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.14: /health returns 200 when SELECT 1 succeeds against Postgres."""
    app = create_app(partial(ping, db_pool))
    response = await _get_health(app)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}


async def test_health_degraded_when_pool_cannot_connect() -> None:
    """R-9.14: /health returns 503 when the pool points at a closed port."""
    pool = create_pool(CLOSED_PORT_DSN)
    await pool.open(wait=False)
    try:
        app = create_app(partial(ping, pool))
        response = await _get_health(app)
    finally:
        await pool.close()
    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "db": "down"}
