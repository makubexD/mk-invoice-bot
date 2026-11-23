"""Integration tests for fetch-attempt persistence (R-5.7)."""

from collections.abc import Sequence
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import ChannelName
from invoicebot.store.fetch_attempts import list_attempts, record_attempt, technical_streak
from invoicebot.store.invoices import create_invoice, start_fetch_round

pytestmark = pytest.mark.asyncio(loop_scope="session")

CHANNEL: ChannelName = "whatsapp"
PORTAL_KEY = "sunat_factura"


def _fresh_portal_key() -> str:
    """Return a portal key no other test in the session database uses."""
    return f"sunat_factura_{uuid4().hex}"


async def _record_portal_attempts(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    portal_key: str,
    invoice_reasons: Sequence[Sequence[str]],
) -> None:
    """Create one invoice per reason list and record each portal attempt in its own transaction."""
    for reasons in invoice_reasons:
        async with db_pool.connection() as conn:
            invoice_id = await create_invoice(conn, "yo", CHANNEL)
        for reason in reasons:
            async with db_pool.connection() as conn:
                await record_attempt(conn, invoice_id, "portal_registry", portal_key, reason, 10)


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


async def test_list_attempts_returns_every_attempt_without_a_round(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D166: an invoice with no fetch round lists every attempt it ever had, in order."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await record_attempt(conn, invoice_id, "portal_registry", PORTAL_KEY, "not_found_yet", 120)
        await record_attempt(conn, invoice_id, "qr_link", None, "ok", 80)
    async with db_pool.connection() as conn:
        attempts = await list_attempts(conn, invoice_id)

    assert attempts == [("portal_registry", "not_found_yet"), ("qr_link", "ok")]


async def test_list_attempts_returns_only_the_current_round(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D166: a round started after two attempts hides them from list_attempts but keeps them stored."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await record_attempt(conn, invoice_id, "portal_registry", PORTAL_KEY, "not_found_yet", 120)
        await record_attempt(conn, invoice_id, "qr_link", None, "ok", 80)
    async with db_pool.connection() as conn:
        await start_fetch_round(conn, invoice_id)
    async with db_pool.connection() as conn:
        await record_attempt(conn, invoice_id, "portal_registry", PORTAL_KEY, "ok", 90)
    async with db_pool.connection() as conn:
        attempts = await list_attempts(conn, invoice_id)
        cur = await conn.execute(
            "SELECT count(*) FROM fetch_attempts WHERE invoice_id = %s",
            (invoice_id,),
        )
        row = await cur.fetchone()

    assert attempts == [("portal_registry", "ok")]
    assert row is not None
    assert row[0] == 3


async def test_list_attempts_is_empty_after_a_round_with_no_attempts(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D166: a round started after the only attempt lists no attempt for the invoice."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await record_attempt(conn, invoice_id, "portal_registry", PORTAL_KEY, "not_found_yet", 120)
    async with db_pool.connection() as conn:
        await start_fetch_round(conn, invoice_id)
    async with db_pool.connection() as conn:
        attempts = await list_attempts(conn, invoice_id)

    assert attempts == []


async def test_technical_streak_counts_attempts_from_before_the_round(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D166: technical_streak is per portal and ignores rounds, so three earlier technicals still count."""
    invoice_ids = []
    for _ in range(3):
        async with db_pool.connection() as conn:
            invoice_id = await create_invoice(conn, "yo", CHANNEL)
            await record_attempt(conn, invoice_id, "portal_registry", PORTAL_KEY, "technical", 10)
            invoice_ids.append(invoice_id)
    for invoice_id in invoice_ids:
        async with db_pool.connection() as conn:
            await start_fetch_round(conn, invoice_id)
    async with db_pool.connection() as conn:
        streak = await technical_streak(conn, PORTAL_KEY, 3)

    assert streak is True


async def test_technical_streak_counts_invoices_not_attempts(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-F03, D170: two invoices with two technical attempts each are not a streak of three."""
    portal_key = _fresh_portal_key()
    await _record_portal_attempts(
        db_pool,
        portal_key,
        [("technical", "technical"), ("technical", "technical")],
    )
    async with db_pool.connection() as conn:
        streak = await technical_streak(conn, portal_key, 3)

    assert streak is False


async def test_technical_streak_counts_one_technical_attempt_per_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-F03, D170: three invoices with one technical attempt each are a streak of three."""
    portal_key = _fresh_portal_key()
    await _record_portal_attempts(db_pool, portal_key, [("technical",)] * 3)
    async with db_pool.connection() as conn:
        streak = await technical_streak(conn, portal_key, 3)

    assert streak is True


async def test_technical_streak_uses_each_invoices_newest_attempt(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-F03, D170: an invoice whose newest portal attempt is not_found_yet breaks the streak."""
    portal_key = _fresh_portal_key()
    await _record_portal_attempts(
        db_pool,
        portal_key,
        [("technical",), ("technical", "not_found_yet"), ("technical",)],
    )
    async with db_pool.connection() as conn:
        streak = await technical_streak(conn, portal_key, 3)

    assert streak is False


async def test_technical_streak_counts_timeouts_as_technical(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-F03, D170: three invoices whose newest attempt is timeout are a streak."""
    portal_key = _fresh_portal_key()
    await _record_portal_attempts(db_pool, portal_key, [("timeout",)] * 3)
    async with db_pool.connection() as conn:
        streak = await technical_streak(conn, portal_key, 3)

    assert streak is True
