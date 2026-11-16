"""The daily task loop: run each due task once per local date (R-9.11, D114).

A task is due when the owner's local time has passed its ``at`` and its
``ops_state`` claim is not from today. The claim commits before the task runs,
so a failing task reports its own failure and does not retry every sweep.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import time

from invoicebot.store.db import Pool
from invoicebot.store.ops_state import claim_daily

logger = logging.getLogger(__name__)

#: How often the loop looks for a task that has become due (D114).
DAILY_SWEEP_S = 300


@dataclass(frozen=True)
class DailyTask:
    """One task that runs once per local date after ``at`` in the owner's timezone."""

    key: str
    at: time
    run: Callable[[], Awaitable[None]]


async def run_due(pool: Pool, timezone: str, tasks: Sequence[DailyTask]) -> list[str]:
    """Claim and run every task that is due, returning the keys whose run was started.

    Each claim is its own short transaction, committed before its task runs. A
    task that raises is logged and the next task still runs; the claim stays.
    """
    started: list[str] = []
    for task in tasks:
        async with pool.connection() as conn, conn.transaction():
            claimed = await claim_daily(conn, task.key, timezone, task.at)
        if not claimed:
            continue
        started.append(task.key)
        try:
            await task.run()
        except Exception:
            logger.exception("daily task failed", extra={"task": task.key})
    return started


async def daily_forever(pool: Pool, timezone: str, tasks: Sequence[DailyTask]) -> None:
    """Run the due tasks now and every ``DAILY_SWEEP_S`` seconds (R-9.11, D114)."""
    while True:
        await run_due(pool, timezone, tasks)
        await asyncio.sleep(DAILY_SWEEP_S)
