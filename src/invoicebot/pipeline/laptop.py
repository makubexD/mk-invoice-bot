"""The receipt poller for ``laptop_outlook`` deliveries (R-7.8, R-7.14, D108).

The laptop agent writes one receipt per job into ``_outbox/receipts``. A sent
receipt closes the prepared batch as ``sent``, files the invoice ``DONE`` with
BR-D10 and queues the ``folder_status`` job; an error receipt records the error
and asks the sender how the mail went out (BR-D11), leaving the batch
``prepared``. Each receipt is applied in its own transaction, and an unknown or
already-handled job changes nothing.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection

from invoicebot.pipeline.deliver import DONE_STATE, PREPARED_BATCH_STATE, SENT_BATCH_STATE
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.steps import FOLDER_STATUS_STEP
from invoicebot.queue.jobs import enqueue
from invoicebot.rules.messages_es import MESSAGES
from invoicebot.sinks.base import Drive
from invoicebot.sinks.laptop_job import MAX_RECEIPT_BYTES, Receipt, parse_receipt
from invoicebot.sinks.onedrive import FileTooLarge
from invoicebot.store.db import Pool
from invoicebot.store.flow_b import (
    get_batch_state,
    load_flow_data,
    open_batch,
    set_batch_state,
    take_photo_paths,
)
from invoicebot.store.invoices import load_flow, load_invoice_head, load_reading, set_state
from invoicebot.store.mail_jobs import get_mail_job, set_receipt_at, set_receipt_error

logger = logging.getLogger(__name__)

#: The cap on a receipt's body; the same 4 KiB the parser accepts (R-7.14).
RECEIPT_MAX_BYTES = MAX_RECEIPT_BYTES
#: BR-D10: the mail was sent and the invoice is filed (spec 06).
RULE_DONE = "BR-D10"
#: BR-D11: the mail's delivery is not confirmed, so the sender is asked (spec 06).
RULE_NOT_CONFIRMED = "BR-D11"


async def poll_receipts(pool: Pool, drive: Drive, root: str) -> None:
    """Apply every receipt under ``{root}/_outbox/receipts`` (R-7.14).

    Each receipt file is downloaded with a :data:`RECEIPT_MAX_BYTES` cap and
    handled in its own transaction. A file that is too large or does not parse
    is logged and skipped, so one bad receipt never blocks the others.
    """
    folder = f"{root}/_outbox/receipts"
    for name in await drive.list_names(folder):
        await _apply_file(pool, drive, f"{folder}/{name}", name)


async def _apply_file(pool: Pool, drive: Drive, path: str, name: str) -> None:
    """Read one receipt and apply it; the photo files it frees are deleted after the commit."""
    receipt = await _read(drive, path, name)
    if receipt is None:
        return
    async with pool.connection() as conn, conn.transaction():
        photos = await _apply(conn, receipt)
    for photo in photos:
        await asyncio.to_thread(photo.unlink, missing_ok=True)


async def _read(drive: Drive, path: str, name: str) -> Receipt | None:
    """Download and parse one receipt, or log why it was skipped (R-7.14)."""
    try:
        return parse_receipt(await drive.download(path, RECEIPT_MAX_BYTES))
    except (FileTooLarge, ValueError) as err:
        logger.warning("receipt %s skipped: %s", name, err)
        return None


async def _apply(conn: AsyncConnection[Any], receipt: Receipt) -> list[Path]:
    """Apply a receipt whose job is known, waiting, and whose batch is prepared (R-7.8).

    Returns the photo paths a completed delivery frees, for deletion after the
    transaction commits; an unknown job, an already-handled one, or any other
    batch state returns none and changes nothing.
    """
    job = await get_mail_job(conn, receipt.job_id)
    if job is None or job.has_receipt:
        return []
    if await get_batch_state(conn, job.invoice_id) != PREPARED_BATCH_STATE:
        return []
    if receipt.sent_at is None:
        await _not_confirmed(conn, job.invoice_id, receipt.job_id, receipt.error or "")
        return []
    return await _sent(conn, job.invoice_id, receipt.job_id)


async def _sent(conn: AsyncConnection[Any], invoice_id: int, job_id: str) -> list[Path]:
    """Close the batch as sent and the invoice DONE with BR-D10 (R-7.8, R-7.14)."""
    flow_id = await load_flow(conn, invoice_id)
    if flow_id is None:
        raise RuntimeError(f"invoice {invoice_id} has no flow")
    batch_id = await open_batch(conn, invoice_id, flow_id, PREPARED_BATCH_STATE)
    await set_batch_state(conn, batch_id, SENT_BATCH_STATE)
    await enqueue(conn, invoice_id, FOLDER_STATUS_STEP)
    await set_state(conn, invoice_id, DONE_STATE, rule_id=RULE_DONE)
    folder = (await load_flow_data(conn, invoice_id))["folder"]
    await queue_message(
        conn,
        await _sender(conn, invoice_id),
        MESSAGES[RULE_DONE].format(folder=folder),
        invoice_id=invoice_id,
        rule_id=RULE_DONE,
        buttons=[],
    )
    await set_receipt_at(conn, job_id)
    return await take_photo_paths(conn, invoice_id)


async def _not_confirmed(conn: AsyncConnection[Any], invoice_id: int, job_id: str, error: str) -> None:
    """Record an error receipt and ask the sender how the mail went out (BR-D11)."""
    await set_receipt_error(conn, job_id, error)
    reading = await load_reading(conn, invoice_id)
    if reading is None:
        raise RuntimeError(f"invoice {invoice_id} has no reading")
    text = MESSAGES[RULE_NOT_CONFIRMED].format(
        serie=reading.value("serie"),
        numero=reading.value("numero"),
    )
    await queue_message(
        conn,
        await _sender(conn, invoice_id),
        text,
        invoice_id=invoice_id,
        rule_id=RULE_NOT_CONFIRMED,
        buttons=[],
    )


async def _sender(conn: AsyncConnection[Any], invoice_id: int) -> Addressee:
    """Return the invoice's sender, the recipient of every receipt-driven message."""
    head = await load_invoice_head(conn, invoice_id)
    if head is None:
        raise RuntimeError(f"invoice {invoice_id} does not exist")
    return Addressee(head.origin_channel, head.profile_id)
