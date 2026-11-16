"""The self-alerts: outbox backlog (BR-O01) and disk usage (BR-O02) (S15.2).

Gap ``github`` rows waiting on a ``SendLater`` stay ``pending`` in the outbox, so
they count toward BR-O01 with no special case: a backlog that grows while GitHub
is down is exactly the signal BR-O01 exists to raise. Both alerts share the
``ops_state`` rate limit with the other self-alerts (BR-O01 to BR-O04, D114).
"""

import asyncio
import shutil
from collections.abc import Callable, Mapping
from datetime import timedelta
from pathlib import Path
from typing import cast

from invoicebot.config.models import Profile, SelfAlerts
from invoicebot.pipeline.outgoing import queue_rate_limited_alert
from invoicebot.rules.messages_es import MESSAGES
from invoicebot.store.db import Pool

#: How often the loop checks the outbox and the disk (S15.2).
SELFCHECK_SWEEP_S = 300

#: The outbox state of a row that has not been sent yet (migrations/versions/0003_outbox_retry.py).
PENDING = "pending"

#: The rule ids, which are also the ``ops_state`` rate-limit keys (BR-O01, BR-O02).
RULE_OUTBOX_BACKLOG = "BR-O01"
RULE_DISK = "BR-O02"


def disk_used_pct(path: Path) -> float:
    """Return the used percentage (0-100) of the volume that holds ``path`` (BR-O02)."""
    usage = shutil.disk_usage(path)
    return usage.used / usage.total * 100


def _age_text(age: timedelta) -> str:
    """Render a pending row's age in whole minutes under an hour, else in whole hours."""
    minutes = int(age.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h"


async def check(
    pool: Pool,
    profiles: Mapping[str, Profile],
    alerts: SelfAlerts,
    data_dir: Path,
    disk_usage: Callable[[Path], float],
) -> None:
    """Queue BR-O01 and BR-O02's rate-limited admin alerts when a threshold is crossed.

    The pending count and the oldest pending age come from one query on the
    database clock. Gap ``github`` rows waiting on a ``SendLater`` are still
    ``pending``, so they count toward BR-O01 with no special case. Both alerts
    are queued in one transaction per check run. ``disk_usage`` returns the used
    percentage of the volume and is called off the event loop.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*), now() - min(created_at) FROM outbox WHERE state = %s",
            (PENDING,),
        )
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError("SELECT count(*), now() - min(created_at) FROM outbox yielded no row")
    count = int(row[0])
    oldest = cast(timedelta | None, row[1])
    pct = await asyncio.to_thread(disk_usage, data_dir)

    backlog = alerts.outbox_backlog
    too_many = count > backlog.max_rows
    too_old = oldest is not None and oldest > timedelta(minutes=backlog.max_age_min)
    every = timedelta(hours=alerts.realert_hours)
    async with pool.connection() as conn, conn.transaction():
        if too_many or too_old:
            text = MESSAGES[RULE_OUTBOX_BACKLOG].format(
                n=count, age=_age_text(oldest) if oldest is not None else "0 min"
            )
            await queue_rate_limited_alert(
                conn, profiles, RULE_OUTBOX_BACKLOG, every, RULE_OUTBOX_BACKLOG, text
            )
        if pct > alerts.disk_pct:
            text = MESSAGES[RULE_DISK].format(pct=round(pct))
            await queue_rate_limited_alert(conn, profiles, RULE_DISK, every, RULE_DISK, text)


async def selfcheck_forever(
    pool: Pool,
    profiles: Mapping[str, Profile],
    alerts: SelfAlerts,
    data_dir: Path,
    disk_usage: Callable[[Path], float],
) -> None:
    """Run the self-check now and every ``SELFCHECK_SWEEP_S`` seconds (S15.2)."""
    while True:
        await check(pool, profiles, alerts, data_dir, disk_usage)
        await asyncio.sleep(SELFCHECK_SWEEP_S)
