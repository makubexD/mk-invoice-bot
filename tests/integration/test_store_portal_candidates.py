"""Integration tests for the per-invoice portal candidate and the approved hosts (D191)."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import ChannelName
from invoicebot.store.invoices import create_invoice
from invoicebot.store.portal_candidates import get, save
from invoicebot.store.taught_links import approved_hosts

pytestmark = pytest.mark.asyncio(loop_scope="session")

CHANNEL: ChannelName = "whatsapp"
#: From tests/synthetic_rucs.txt; no real RUC may appear here.
APPROVED_RUC = 20000000010
PROPOSED_RUC = 20000000028
REJECTED_RUC = 20000000036
URL = "https://portal.example.pe/consulta"


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def clean_taught_links(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> AsyncIterator[None]:
    """Start each test from an empty taught_links table."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM taught_links")
    yield


async def test_save_then_get_returns_the_url(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D191: a saved candidate reads back for its invoice."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await save(conn, invoice_id, URL)
        found = await get(conn, invoice_id)

    assert found == URL


async def test_second_save_replaces_the_first(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D191: a second save for the same invoice replaces the first URL."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await save(conn, invoice_id, URL)
        await save(conn, invoice_id, "https://otro.example.pe/consulta")
        found = await get(conn, invoice_id)

    assert found == "https://otro.example.pe/consulta"


async def test_get_without_a_candidate_is_none(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D191: an invoice BR-F14 never offered a URL for reads None."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        found = await get(conn, invoice_id)

    assert found is None


async def test_deleting_the_invoice_deletes_its_candidate(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D191: the foreign key removes the candidate with its invoice."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await save(conn, invoice_id, URL)
        await conn.execute("DELETE FROM invoices WHERE id = %s", (invoice_id,))
        found = await get(conn, invoice_id)

    assert found is None


async def test_approved_hosts_keeps_only_approved_links(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D191: approved_hosts returns the approved link's lowercase host and skips the other states."""
    async with db_pool.connection() as conn:
        for ruc_emisor, url, state in (
            (APPROVED_RUC, "https://Portal.Example.PE/consulta", "approved"),
            (PROPOSED_RUC, "https://propuesto.example.pe/consulta", "proposed"),
            (REJECTED_RUC, "https://rechazado.example.pe/consulta", "rejected"),
        ):
            await conn.execute(
                "INSERT INTO taught_links (ruc_emisor, url, profile_id, state) VALUES (%s, %s, %s, %s)",
                (ruc_emisor, url, "owner", state),
            )
        hosts = await approved_hosts(conn)

    assert hosts == frozenset({"portal.example.pe"})
