"""Daily housekeeping: stale jobs and the temp folders of finished invoices (R-9.7, S12.4).

A download lives in ``<data_dir>/tmp/<invoice_id>/`` until its invoice is done
with it. Once the invoice is terminal, or no longer exists, the folder can go.
Jobs left behind by an invoice that can no longer move are deleted too. The
daily loop schedules this; the module never runs on its own.
"""

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection

from invoicebot.pipeline.handlers import TMP_DIRECTORY
from invoicebot.states import TERMINAL
from invoicebot.store.db import Pool
from invoicebot.store.retention import delete_stale_jobs

logger = logging.getLogger(__name__)


async def housekeep(pool: Pool, data_dir: Path) -> None:
    """Delete stale jobs and the tmp folders of terminal or unknown invoices (R-9.7, S12.4).

    The jobs are deleted in one transaction. A tmp entry is removed only when
    its name is an invoice id in a terminal state or one no invoice has; any
    other name is left alone. Logs one INFO line with both counts.
    """
    async with pool.connection() as conn, conn.transaction():
        jobs = await delete_stale_jobs(conn)
    tmp_dirs = await _remove_stale_tmp(pool, data_dir)
    logger.info("housekeeping done", extra={"jobs": jobs, "tmp_dirs": tmp_dirs})


async def _remove_stale_tmp(pool: Pool, data_dir: Path) -> int:
    """Remove the tmp folders whose invoice is terminal or unknown (R-9.7), returning how many.

    The invoice rows stay locked while their folders are removed, so a concurrent
    state change cannot move an invoice into a download that is about to lose its
    folder.
    """
    root = data_dir / TMP_DIRECTORY
    if not root.is_dir():
        return 0
    folders = {
        int(entry.name): entry
        for entry in root.iterdir()
        if entry.name.isdigit() and entry.is_dir() and not entry.is_symlink()
    }
    if not folders:
        return 0
    async with pool.connection() as conn, conn.transaction():
        states = await _invoice_states(conn, list(folders))
        stale = [
            entry
            for invoice_id, entry in folders.items()
            if states.get(invoice_id) is None or states[invoice_id] in TERMINAL
        ]
        for entry in stale:
            await asyncio.to_thread(shutil.rmtree, entry)
    return len(stale)


async def _invoice_states(conn: AsyncConnection[Any], invoice_ids: list[int]) -> dict[int, str]:
    """Return the state of each invoice among ``invoice_ids`` that still exists.

    The rows are locked ``FOR SHARE`` until the caller's transaction ends (R-9.7),
    so a state change on one of them waits for that transaction.
    """
    cur = await conn.execute("SELECT id, state FROM invoices WHERE id = ANY(%s) FOR SHARE", (invoice_ids,))
    return {int(row[0]): str(row[1]) for row in await cur.fetchall()}
