"""Integration tests for gap persistence (R-11.2, R-11.8, R-11.11, BR-F02, BR-E06, BR-R06)."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import ChannelName
from invoicebot.gaps.kinds import GAP_KINDS
from invoicebot.store.gaps import alert_due, mark_alerted, unreported_overdue, upsert_gap
from invoicebot.store.invoices import create_invoice

pytestmark = pytest.mark.asyncio(loop_scope="session")

CHANNEL: ChannelName = "whatsapp"
#: From tests/synthetic_rucs.txt; no real RUC may appear here.
ISSUER_RUC = "20000000001"
BUYER_RUC = "20000000002"
SUBJECT = f"RUC {ISSUER_RUC}"
PORTAL_MISSING = GAP_KINDS["PORTAL_MISSING"]
PARSER_MISS = GAP_KINDS["PARSER_MISS"]
UNROUTED_BUYER = GAP_KINDS["UNROUTED_BUYER"]

PORTAL_FP = f"portal_missing:{ISSUER_RUC}"
PARSER_FP = f"parser_miss:{ISSUER_RUC}"
UNROUTED_FP = f"unrouted:{BUYER_RUC}"
OLD_FP = "portal_missing:20000000003"
RECENT_FP = "portal_missing:20000000004"
DUE_FP = "portal_missing:20000000005"
IGNORED_FP = "portal_missing:20000000006"


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def clean_gaps(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> AsyncIterator[None]:
    """Start each test from an empty gaps and invoice_gaps."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM invoice_gaps")
        await conn.execute("DELETE FROM gaps")
    yield


async def test_ten_upserts_make_one_gap_with_ten_links(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.2: ten upserts of one fingerprint keep one row with count 10 and ten links."""
    async with db_pool.connection() as conn:
        invoice_ids = [await create_invoice(conn, "yo", CHANNEL) for _ in range(10)]
        for invoice_id in invoice_ids:
            last = await upsert_gap(conn, PORTAL_MISSING, PORTAL_FP, SUBJECT, invoice_id)

        cur = await conn.execute("SELECT count(*), max(count) FROM gaps WHERE fingerprint = %s", (PORTAL_FP,))
        gaps_row = await cur.fetchone()
        cur = await conn.execute("SELECT count(*) FROM invoice_gaps WHERE fingerprint = %s", (PORTAL_FP,))
        links_row = await cur.fetchone()

    assert gaps_row == (1, 10)
    assert links_row == (10,)
    assert last.count == 10
    assert last.raised_now is False


async def test_portal_missing_is_raised_on_the_first_upsert_only(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-F02: a threshold-1 gap reports raised_now on the first upsert only."""
    async with db_pool.connection() as conn:
        first = await upsert_gap(conn, PORTAL_MISSING, PORTAL_FP, SUBJECT, None)
        second = await upsert_gap(conn, PORTAL_MISSING, PORTAL_FP, SUBJECT, None)

    assert first.raised_now is True
    assert first.raised_at is not None
    assert second.raised_now is False
    assert second.count == 2


async def test_parser_miss_is_raised_on_the_second_upsert(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-E06: a threshold-2 gap is raised on the second upsert, not the first."""
    async with db_pool.connection() as conn:
        first = await upsert_gap(conn, PARSER_MISS, PARSER_FP, SUBJECT, None)
        second = await upsert_gap(conn, PARSER_MISS, PARSER_FP, SUBJECT, None)

    assert first.raised_now is False
    assert first.raised_at is None
    assert second.raised_now is True
    assert second.count == 2


async def test_unrouted_buyer_raises_on_the_third_and_restarts_after_31_days(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-R06: the third upsert in 30 days raises; one after 31 days restarts at count 1."""
    async with db_pool.connection() as conn:
        first = await upsert_gap(conn, UNROUTED_BUYER, UNROUTED_FP, SUBJECT, None)
        second = await upsert_gap(conn, UNROUTED_BUYER, UNROUTED_FP, SUBJECT, None)
        third = await upsert_gap(conn, UNROUTED_BUYER, UNROUTED_FP, SUBJECT, None)
        await conn.execute(
            "UPDATE gaps SET last_seen = now() - interval '31 days' WHERE fingerprint = %s",
            (UNROUTED_FP,),
        )
        restarted = await upsert_gap(conn, UNROUTED_BUYER, UNROUTED_FP, SUBJECT, None)

    assert first.raised_now is False
    assert second.raised_now is False
    assert third.raised_now is True
    assert third.count == 3
    assert restarted.count == 1
    assert restarted.raised_now is False


async def test_unreported_overdue_returns_only_gaps_raised_over_24_hours_ago(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.11: unreported_overdue returns a gap raised 25 h ago, not one raised 1 h ago."""
    async with db_pool.connection() as conn:
        await upsert_gap(conn, PORTAL_MISSING, OLD_FP, SUBJECT, None)
        await upsert_gap(conn, PORTAL_MISSING, RECENT_FP, SUBJECT, None)
        await conn.execute(
            "UPDATE gaps SET raised_at = now() - interval '25 hours' WHERE fingerprint = %s",
            (OLD_FP,),
        )
        await conn.execute(
            "UPDATE gaps SET raised_at = now() - interval '1 hour' WHERE fingerprint = %s",
            (RECENT_FP,),
        )
        overdue = await unreported_overdue(conn)

    assert [gap.fingerprint for gap in overdue] == [OLD_FP]


async def test_alert_due_skips_ignored_and_recently_alerted_gaps(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.8: alert_due skips an ignored gap and one alerted yesterday with realert_days 7."""
    async with db_pool.connection() as conn:
        await upsert_gap(conn, PORTAL_MISSING, DUE_FP, SUBJECT, None)
        await upsert_gap(conn, PORTAL_MISSING, RECENT_FP, SUBJECT, None)
        await upsert_gap(conn, PORTAL_MISSING, IGNORED_FP, SUBJECT, None)
        await mark_alerted(conn, [RECENT_FP])
        await conn.execute(
            "UPDATE gaps SET last_alert_at = now() - interval '1 day' WHERE fingerprint = %s",
            (RECENT_FP,),
        )
        await conn.execute("UPDATE gaps SET state = 'ignored' WHERE fingerprint = %s", (IGNORED_FP,))
        due = await alert_due(conn, 7, [PORTAL_MISSING.kind])

    assert [gap.fingerprint for gap in due] == [DUE_FP]
