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


#: The largest invoice id Postgres can hold (``bigint``); a longer name is not an invoice's folder.
_MAX_INVOICE_ID = 2**63 - 1


async def _remove_stale_tmp(pool: Pool, data_dir: Path) -> int:
    """Remove the tmp folders whose invoice is terminal or unknown (R-9.7), returning how many.

    Only the terminal invoices' rows are locked, in id order, while their folders
    are removed, so a concurrent retry cannot move one of them into a download
    that is about to lose its folder, and invoices still running never wait. A
    folder that cannot be removed is skipped and counted in one warning, so one
    bad entry never stops the rest.
    """
    root = data_dir / TMP_DIRECTORY
    if not root.is_dir():
        return 0
    folders = {
        int(entry.name): entry
        for entry in root.iterdir()
        if _is_invoice_id(entry.name) and entry.is_dir() and not entry.is_symlink()
    }
    if not folders:
        return 0
    removed = failed = 0
    async with pool.connection() as conn, conn.transaction():
        states = await _invoice_states(conn, list(folders))
        unknown = [invoice_id for invoice_id in folders if invoice_id not in states]
        terminal = [invoice_id for invoice_id, state in states.items() if state in TERMINAL]
        for invoice_id in unknown + await _lock_terminal(conn, terminal):
            try:
                await asyncio.to_thread(shutil.rmtree, folders[invoice_id])
            except OSError:
                failed += 1
            else:
                removed += 1
    if failed:
        logger.warning("tmp folders not removed", extra={"tmp_dirs": failed})
    return removed


def _is_invoice_id(name: str) -> bool:
    """True when ``name`` is ASCII digits that fit an invoice id."""
    return name.isascii() and name.isdecimal() and int(name) <= _MAX_INVOICE_ID


async def _invoice_states(conn: AsyncConnection[Any], invoice_ids: list[int]) -> dict[int, str]:
    """Return the state of each invoice among ``invoice_ids`` that still exists."""
    cur = await conn.execute("SELECT id, state FROM invoices WHERE id = ANY(%s)", (invoice_ids,))
    return {int(row[0]): str(row[1]) for row in await cur.fetchall()}


async def _lock_terminal(conn: AsyncConnection[Any], invoice_ids: list[int]) -> list[int]:
    """Lock the invoices among ``invoice_ids`` that are still terminal and return their ids (R-9.7).

    The rows are locked ``FOR SHARE`` in id order until the caller's transaction
    ends, so a state change on one of them waits for that transaction; an invoice
    that left its terminal state since it was read is not returned.
    """
    if not invoice_ids:
        return []
    cur = await conn.execute(
        "SELECT id FROM invoices WHERE id = ANY(%s) AND state = ANY(%s) ORDER BY id FOR SHARE",
        (invoice_ids, sorted(TERMINAL)),
    )
    return [int(row[0]) for row in await cur.fetchall()]
