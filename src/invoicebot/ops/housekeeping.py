"""Daily housekeeping: stale jobs and the folders of finished invoices (R-9.7, S12.4, D195, D206).

A download lives in ``<data_dir>/tmp/<invoice_id>/`` until its invoice is done
with it. Once the invoice is terminal, or no longer exists, the folder can go.
Review files live in ``<data_dir>/mail/<invoice_id>/`` and follow the same rule,
except while a review document row still waits in the outbox (D195); the
``mail/<id>.eml`` dry-run drafts are files and are never touched.
The kept chat documents in ``<data_dir>/docs/<invoice_id>/`` go too, but a terminal
invoice's folder stays until its last state change (``updated_at``) is older than
the chat-documents retention, so the user can still ask for them (D206).
Jobs left behind by an invoice that can no longer move are deleted too. The
daily loop schedules this; the module never runs on its own.
"""

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection

from invoicebot.pipeline.chat_docs import DOCS_DIRECTORY
from invoicebot.pipeline.chat_docs_post import RULE_SENT
from invoicebot.pipeline.handlers import TMP_DIRECTORY
from invoicebot.pipeline.review import RULE_REVIEW
from invoicebot.queue.outbox import has_pending_document
from invoicebot.sinks.mailer import MAIL_DIRECTORY
from invoicebot.states import TERMINAL
from invoicebot.store.db import Pool
from invoicebot.store.retention import delete_stale_jobs

logger = logging.getLogger(__name__)


async def housekeep(pool: Pool, data_dir: Path, docs_retention_days: int) -> None:
    """Delete stale jobs and the tmp, mail and docs folders of finished invoices (R-9.7, D195, D206).

    The jobs are deleted in one transaction. A folder is removed only when its
    name is an invoice id in a terminal state or one no invoice has; any other
    name is left alone, and a mail folder stays while a review document row is
    pending. A docs folder stays while a BR-D24 document row is pending, and one of a
    terminal invoice also waits until the invoice's last
    state change is ``docs_retention_days`` old by the database clock (D206). Logs one
    INFO line with the counts.
    """
    async with pool.connection() as conn, conn.transaction():
        jobs = await delete_stale_jobs(conn)
    tmp_dirs = await _remove_stale(pool, data_dir / TMP_DIRECTORY, "tmp")
    mail_dirs = await _remove_stale(pool, data_dir / MAIL_DIRECTORY, "mail", RULE_REVIEW)
    docs_dirs = await _remove_stale(
        pool, data_dir / DOCS_DIRECTORY, "docs", RULE_SENT, min_age_days=docs_retention_days
    )
    logger.info(
        "housekeeping done",
        extra={"jobs": jobs, "tmp_dirs": tmp_dirs, "mail_dirs": mail_dirs, "docs_dirs": docs_dirs},
    )


#: The largest invoice id Postgres can hold (``bigint``); a longer name is not an invoice's folder.
_MAX_INVOICE_ID = 2**63 - 1


async def _remove_stale(
    pool: Pool, root: Path, kind: str, pending_rule: str | None = None, min_age_days: int | None = None
) -> int:
    """Remove the folders under ``root`` of terminal or unknown invoices (D195, D206), returning the count.

    ``kind`` names the root in the warning. A folder whose invoice still has a
    pending document of ``pending_rule`` is kept, checked in the same transaction.
    With ``min_age_days``, a terminal invoice's folder is kept until its last state
    change is that many days old; a folder with no invoice always goes.

    Only the terminal invoices' rows are locked, in id order, while their folders
    are removed, so a concurrent retry cannot move one of them into a download
    that is about to lose its folder, and invoices still running never wait. A
    folder that cannot be removed is skipped and counted in one warning, so one
    bad entry never stops the rest.
    """
    folders = await asyncio.to_thread(_invoice_folders, root)
    if not folders:
        return 0
    removed = failed = 0
    async with pool.connection() as conn, conn.transaction():
        states = await _invoice_states(conn, list(folders))
        unknown = [invoice_id for invoice_id in folders if invoice_id not in states]
        terminal = [invoice_id for invoice_id, state in states.items() if state in TERMINAL]
        for invoice_id in unknown + await _lock_terminal(conn, terminal, min_age_days):
            if pending_rule is not None and await has_pending_document(conn, invoice_id, pending_rule):
                continue
            try:
                await asyncio.to_thread(shutil.rmtree, folders[invoice_id])
            except OSError:
                failed += 1
            else:
                removed += 1
    if failed:
        logger.warning("folders not removed", extra={"root": kind, "dirs": failed})
    return removed


def _invoice_folders(root: Path) -> dict[int, Path]:
    """Map invoice id to its real folder among the entries of ``root``; empty when ``root`` is missing."""
    if not root.is_dir():
        return {}
    return {
        int(entry.name): entry
        for entry in root.iterdir()
        if _is_invoice_id(entry.name) and entry.is_dir() and not entry.is_symlink()
    }


def _is_invoice_id(name: str) -> bool:
    """True when ``name`` is ASCII digits that fit an invoice id."""
    return name.isascii() and name.isdecimal() and int(name) <= _MAX_INVOICE_ID


async def _invoice_states(conn: AsyncConnection[Any], invoice_ids: list[int]) -> dict[int, str]:
    """Return the state of each invoice among ``invoice_ids`` that still exists."""
    cur = await conn.execute("SELECT id, state FROM invoices WHERE id = ANY(%s)", (invoice_ids,))
    return {int(row[0]): str(row[1]) for row in await cur.fetchall()}


async def _lock_terminal(
    conn: AsyncConnection[Any], invoice_ids: list[int], min_age_days: int | None
) -> list[int]:
    """Lock the invoices among ``invoice_ids`` that are still terminal and return their ids (R-9.7, D206).

    The rows are locked ``FOR SHARE`` in id order until the caller's transaction
    ends, so a state change on one of them waits for that transaction; an invoice
    that left its terminal state since it was read is not returned. With
    ``min_age_days``, one whose ``updated_at`` is newer than that many days by the
    database clock is not returned either.
    """
    if not invoice_ids:
        return []
    cur = await conn.execute(
        """
        SELECT id FROM invoices
        WHERE id = ANY(%s) AND state = ANY(%s)
          AND (%s::int IS NULL OR updated_at < now() - make_interval(days => %s::int))
        ORDER BY id FOR SHARE
        """,
        (invoice_ids, sorted(TERMINAL), min_age_days, min_age_days),
    )
    return [int(row[0]) for row in await cur.fetchall()]
