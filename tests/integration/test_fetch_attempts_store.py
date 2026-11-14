"""Integration tests for fetch-attempt persistence (R-5.7)."""

from typing import Any

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import ChannelName
from invoicebot.store.fetch_attempts import list_attempts, record_attempt
from invoicebot.store.invoices import create_invoice

pytestmark = pytest.mark.asyncio(loop_scope="session")

CHANNEL: ChannelName = "whatsapp"
PORTAL_KEY = "sunat_factura"


async def test_record_attempt_lists_attempts_in_insertion_order(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-5.7: two attempts come back in insertion order with their reasons."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await record_attempt(conn, invoice_id, "portal_registry", PORTAL_KEY, "not_found_yet", 120)
        await record_attempt(conn, invoice_id, "qr_link", None, "ok", 80)
        attempts = await list_attempts(conn, invoice_id)

    assert attempts == [("portal_registry", "not_found_yet"), ("qr_link", "ok")]


async def test_list_attempts_does_not_list_another_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-5.7: an attempt recorded for another invoice is not listed."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        other_id = await create_invoice(conn, "yo", CHANNEL)
        await record_attempt(conn, invoice_id, "portal_registry", PORTAL_KEY, "ok", 10)
        await record_attempt(conn, other_id, "portal_registry", PORTAL_KEY, "timeout", 20)
        attempts = await list_attempts(conn, invoice_id)

    assert attempts == [("portal_registry", "ok")]


async def test_record_attempt_rolls_back_with_the_transaction(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-5.7: an attempt recorded in a rolled-back transaction leaves no row."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        with pytest.raises(RuntimeError, match="roll back"):
            async with conn.transaction():
                await record_attempt(conn, invoice_id, "portal_registry", PORTAL_KEY, "ok", 10)
                raise RuntimeError("roll back the attempt")
        attempts = await list_attempts(conn, invoice_id)

    assert attempts == []


async def test_record_attempt_sets_ok_only_for_the_ok_reason(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-5.7: the ok column is true for "ok" and false for any other reason."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await record_attempt(conn, invoice_id, "portal_registry", PORTAL_KEY, "ok", 10)
        await record_attempt(conn, invoice_id, "qr_link", None, "not_found_yet", 20)
        cur = await conn.execute(
            "SELECT ok, reason FROM fetch_attempts WHERE invoice_id = %s ORDER BY id",
            (invoice_id,),
        )
        rows = await cur.fetchall()

    assert rows == [(True, "ok"), (False, "not_found_yet")]
