"""Integration tests for the portal probe store (D193)."""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import ChannelName
from invoicebot.fetchers.browser.probe_protocol import ProbeOutcome, ProbeReport
from invoicebot.store.invoices import create_invoice
from invoicebot.store.portal_probes import count_try, finish, get, get_for_invoice, start

pytestmark = pytest.mark.asyncio(loop_scope="session")

REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNEL: ChannelName = "whatsapp"
#: From tests/synthetic_rucs.txt; no real RUC may appear here.
RUC = 20000000010
REPORT = ProbeReport(outcome=ProbeOutcome.CAPTCHA, captcha="image").model_dump(mode="json")


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def clean_probes(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> AsyncIterator[None]:
    """Start each test from an empty portal_probes table."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM portal_probes")
    yield


async def test_start_then_get_returns_a_queued_row(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D193: a started probe reads back queued with no tries and no result."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await start(conn, RUC, 1, invoice_id)
        row = await get(conn, RUC)
        by_invoice = await get_for_invoice(conn, invoice_id)

    assert row is not None
    assert (row.ruc_emisor, row.link_version, row.invoice_id) == (RUC, 1, invoice_id)
    assert (row.state, row.tries, row.outcome, row.report) == ("queued", 0, None, None)
    assert by_invoice == row


async def test_get_without_a_row_is_none(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """D193: an issuer never probed reads None."""
    async with db_pool.connection() as conn:
        assert await get(conn, RUC) is None
        assert await get_for_invoice(conn, 999999) is None


async def test_count_try_twice_returns_two(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """D193: count_try returns the running total, and 0 for a version with no row."""
    async with db_pool.connection() as conn:
        await start(conn, RUC, 1, None)
        first = await count_try(conn, RUC, 1)
        second = await count_try(conn, RUC, 1)
        other_version = await count_try(conn, RUC, 2)

    assert (first, second, other_version) == (1, 2, 0)


async def test_finish_marks_done_with_the_report(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """D193: finish stores done, the outcome and the report; a second finish is False."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await start(conn, RUC, 1, invoice_id)
        first = await finish(conn, RUC, 1, ProbeOutcome.CAPTCHA.value, REPORT)
        second = await finish(conn, RUC, 1, ProbeOutcome.OK.value, REPORT)
        row = await get(conn, RUC)
        queued = await get_for_invoice(conn, invoice_id)

    assert (first, second) == (True, False)
    assert row is not None
    assert (row.state, row.outcome) == ("done", "captcha")
    assert row.report == REPORT
    assert queued is None


async def test_finish_with_an_older_version_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D193: a result for an older version is refused and the row stays queued."""
    async with db_pool.connection() as conn:
        await start(conn, RUC, 2, None)
        accepted = await finish(conn, RUC, 1, ProbeOutcome.OK.value, REPORT)
        row = await get(conn, RUC)

    assert accepted is False
    assert row is not None
    assert (row.link_version, row.state, row.outcome, row.report) == (2, "queued", None, None)


async def test_start_with_a_newer_version_resets(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """D193: a newer version replaces a finished row: queued, tries 0, no outcome."""
    async with db_pool.connection() as conn:
        await start(conn, RUC, 1, None)
        await count_try(conn, RUC, 1)
        await finish(conn, RUC, 1, ProbeOutcome.OK.value, REPORT)
        await start(conn, RUC, 2, None)
        row = await get(conn, RUC)

    assert row is not None
    assert (row.link_version, row.state, row.tries, row.outcome, row.report) == (2, "queued", 0, None, None)


async def test_deleting_the_invoice_keeps_the_row(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """D193: deleting the invoice leaves the probe row with invoice_id NULL."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await start(conn, RUC, 1, invoice_id)
        await conn.execute("DELETE FROM invoices WHERE id = %s", (invoice_id,))
        row = await get(conn, RUC)

    assert row is not None
    assert row.invoice_id is None


def _run_alembic(url: str, revision: str, *, downgrade: bool) -> None:
    """Run Alembic against url; call it through asyncio.to_thread."""
    config = Config(str(REPO_ROOT / "alembic.ini"))
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        (command.downgrade if downgrade else command.upgrade)(config, revision)
    finally:
        if previous is None:
            del os.environ["DATABASE_URL"]
        else:
            os.environ["DATABASE_URL"] = previous


async def _has_table(pool: AsyncConnectionPool[AsyncConnection[Any]]) -> bool:
    """Return True when portal_probes exists."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT to_regclass('public.portal_probes') IS NOT NULL")
        row = await cur.fetchone()
    return bool(row and row[0])


async def test_downgrade_drops_and_upgrade_recreates_the_table(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D193: downgrading 0017 drops portal_probes and upgrading head brings it back."""
    try:
        await asyncio.to_thread(_run_alembic, db_pool.conninfo, "0016", downgrade=True)
        dropped = not await _has_table(db_pool)
    finally:
        await asyncio.to_thread(_run_alembic, db_pool.conninfo, "head", downgrade=False)

    assert dropped
    assert await _has_table(db_pool)
