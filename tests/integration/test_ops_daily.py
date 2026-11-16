"""Integration tests for the daily task loop (R-9.11, D114)."""

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import time
from typing import Any

import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.ops.daily import DailyTask, run_due

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: The owner timezone, where a task's local date is decided (D114).
TIMEZONE = "America/Lima"
#: Every key this file writes, so the cleanup deletes only its own claims.
KEY_PREFIX = "daily-test-"
ALL_DUE = time(0, 0)
ALMOST_MIDNIGHT = time(23, 59, 59, 999999)


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def clean_daily_keys(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> AsyncIterator[None]:
    """Delete this file's ops_state claims before each test."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM ops_state WHERE key LIKE %s", (f"{KEY_PREFIX}%",))
    yield


def _recorder(calls: list[str], key: str) -> Callable[[], Awaitable[None]]:
    """Return a task body that records having run under ``key``."""

    async def run() -> None:
        calls.append(key)

    return run


async def test_task_before_its_time_does_not_run(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.11: a task whose local time has not passed does not run."""
    calls: list[str] = []
    tasks = [DailyTask(key=f"{KEY_PREFIX}later", at=ALMOST_MIDNIGHT, run=_recorder(calls, "later"))]

    started = await run_due(db_pool, TIMEZONE, tasks)

    assert started == []
    assert calls == []


async def test_task_at_midnight_runs_once_and_not_again(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.11: a task at midnight runs once, and a second sweep the same day skips it."""
    key = f"{KEY_PREFIX}midnight"
    calls: list[str] = []
    tasks = [DailyTask(key=key, at=ALL_DUE, run=_recorder(calls, key))]

    first = await run_due(db_pool, TIMEZONE, tasks)
    second = await run_due(db_pool, TIMEZONE, tasks)

    assert first == [key]
    assert second == []
    assert calls == [key]


async def test_task_claimed_yesterday_runs_today(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.11: a claim stamped yesterday in the local zone runs at the next sweep."""
    key = f"{KEY_PREFIX}yesterday"
    async with db_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO ops_state (key, last_at) VALUES (%s, now() - interval '1 day')",
            (key,),
        )
    calls: list[str] = []

    started = await run_due(db_pool, TIMEZONE, [DailyTask(key=key, at=ALL_DUE, run=_recorder(calls, key))])

    assert started == [key]
    assert calls == [key]


async def test_failing_task_does_not_stop_the_next_and_keeps_its_claim(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.11: a task that raises is logged, the next still runs, and its claim stays."""
    failing_key = f"{KEY_PREFIX}failing"
    next_key = f"{KEY_PREFIX}next"
    calls: list[str] = []

    async def failing() -> None:
        raise RuntimeError("task failed")

    tasks = [
        DailyTask(key=failing_key, at=ALL_DUE, run=failing),
        DailyTask(key=next_key, at=ALL_DUE, run=_recorder(calls, next_key)),
    ]

    first = await run_due(db_pool, TIMEZONE, tasks)
    second = await run_due(db_pool, TIMEZONE, tasks)

    assert first == [failing_key, next_key]
    assert calls == [next_key]
    assert second == []


async def test_claim_before_today_in_lima_runs(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.11: a claim one second before today in America/Lima runs at the next sweep."""
    key = f"{KEY_PREFIX}before-today"
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO ops_state (key, last_at)
            VALUES (
                %s,
                (date_trunc('day', now() AT TIME ZONE 'America/Lima')
                 AT TIME ZONE 'America/Lima') - interval '1 second'
            )
            """,
            (key,),
        )
    calls: list[str] = []

    started = await run_due(db_pool, TIMEZONE, [DailyTask(key=key, at=ALL_DUE, run=_recorder(calls, key))])

    assert started == [key]
    assert calls == [key]


async def test_claim_at_start_of_today_in_lima_does_not_run(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.11: a claim at today's start in America/Lima does not run again today."""
    key = f"{KEY_PREFIX}today"
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO ops_state (key, last_at)
            VALUES (
                %s,
                (date_trunc('day', now() AT TIME ZONE 'America/Lima')
                 AT TIME ZONE 'America/Lima') + interval '2 seconds'
            )
            """,
            (key,),
        )
    calls: list[str] = []

    started = await run_due(db_pool, TIMEZONE, [DailyTask(key=key, at=ALL_DUE, run=_recorder(calls, key))])

    assert started == []
    assert calls == []
