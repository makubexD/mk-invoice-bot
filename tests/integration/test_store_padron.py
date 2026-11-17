"""Integration tests for the padrón primary-key lookup (R-2.5, D123)."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.store.padron import LOOKUP_SQL, PadronRow, lookup

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: The synthetic issuer RUC tests/synthetic_rucs.txt uses.
RUC = "20000000001"
#: A RUC the test padrón never carries.
UNKNOWN_RUC = "20999999999"
#: Ten digits: not a RUC.
SHORT_RUC = "2000000001"
#: First RUC of the seeded block; 11 digits and clear of ``RUC``.
SEED_BASE = 30000000000
#: Enough rows for the planner to prefer the primary-key index for one lookup (R-2.5).
SEEDED_ROWS = 5000
#: The lookup under EXPLAIN, armed with the same SQL the module runs.
EXPLAIN_SQL = "EXPLAIN " + LOOKUP_SQL


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def empty_padron(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> AsyncIterator[None]:
    """Start each test from an empty padrón table."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM padron")
    yield


async def test_lookup_seeded_ruc_returns_row(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-2.5: a RUC in the padrón returns its razón social, estado and condición."""
    async with db_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO padron (ruc, razon_social, estado, condicion) VALUES (%s, %s, %s, %s)",
            (int(RUC), "EMPRESA DE PRUEBA", "ACTIVO", "HABIDO"),
        )
        found = await lookup(conn, RUC)

    assert found == PadronRow(
        ruc=RUC,
        razon_social="EMPRESA DE PRUEBA",
        estado="ACTIVO",
        condicion="HABIDO",
    )


async def test_lookup_unknown_ruc_is_none(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-2.5: a RUC the padrón does not carry gives None."""
    async with db_pool.connection() as conn:
        found = await lookup(conn, UNKNOWN_RUC)

    assert found is None


async def test_lookup_short_ruc_is_none(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-2.5: a value that is not 11 digits is not a RUC and gives None."""
    async with db_pool.connection() as conn:
        found = await lookup(conn, SHORT_RUC)

    assert found is None


async def test_lookup_plan_uses_padron_pkey_index(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-2.5: with a populated padrón, EXPLAIN shows the lookup on ``padron_pkey``."""
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO padron (ruc, razon_social, estado, condicion)
            SELECT ruc, 'EMPRESA ' || ruc::text, 'ACTIVO', 'HABIDO'
            FROM generate_series(%s::bigint, %s::bigint) AS ruc
            """,
            (SEED_BASE, SEED_BASE + SEEDED_ROWS - 1),
        )
        await conn.execute("ANALYZE padron")
        cur = await conn.execute(EXPLAIN_SQL, (int(RUC),))
        plan = [str(row[0]) for row in await cur.fetchall()]

    assert any(
        "padron_pkey" in line and ("Index Scan" in line or "Index Only Scan" in line) for line in plan
    ), f"expected an index scan on padron_pkey, got {plan}"
