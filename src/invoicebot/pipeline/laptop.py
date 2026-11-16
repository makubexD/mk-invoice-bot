"""The receipt poller for ``laptop_outlook`` deliveries (R-7.8, R-7.14, D108).

The laptop agent writes one receipt per job into ``_outbox/receipts``. Each
receipt is applied in its own transaction and its file is deleted once that
transaction commits; a file that is too large, does not parse or fails its HMAC
stays on the drive and is logged. A sent receipt closes a prepared batch as
``sent``, files the invoice ``DONE`` with BR-D10 and queues the ``folder_status``
job; on a batch already ``sent`` it only stamps the job and queues BR-D10. An
error receipt records the error and asks the sender how the mail went out
(BR-D11), leaving a prepared batch ``prepared``. A receipt for an unknown or
already-handled job changes nothing and is deleted too, and one receipt that
raises never stops the sweep.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection

from invoicebot.config.models import Mail
from invoicebot.pipeline.actions import send_not_confirmed
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
from invoicebot.store.mail_jobs import (
    OpenMailJob,
    claim_overdue_alert,
    claim_stale_alert,
    get_mail_job,
    list_open_jobs,
    set_receipt_at,
    set_receipt_error,
)

logger = logging.getLogger(__name__)

#: The cap on a receipt's body; the same 4 KiB the parser accepts (R-7.14).
RECEIPT_MAX_BYTES = MAX_RECEIPT_BYTES
#: BR-D10: the mail was sent and the invoice is filed (spec 06).
RULE_DONE = "BR-D10"
#: BR-D12: the laptop agent has not picked the job up (spec 06).
RULE_STALE = "BR-D12"


async def poll_receipts(pool: Pool, drive: Drive, root: str, key: bytes) -> None:
    """Apply every signed receipt under ``{root}/_outbox/receipts`` (R-7.14).

    Each receipt file is downloaded with a :data:`RECEIPT_MAX_BYTES` cap and
    handled in its own transaction. A file that is too large or does not parse
    or whose HMAC does not verify under ``key`` is logged and left in place; a
    file whose application raises is logged by name and also left in place, so
    one bad receipt never blocks the others and the sweep goes on.
    """
    folder = f"{root}/_outbox/receipts"
    for name in await drive.list_names(folder):
        try:
            await _apply_file(pool, drive, f"{folder}/{name}", name, key)
        except Exception:  # one bad receipt must not stop the sweep (R-7.14)
            logger.exception("receipt %s failed", name)


async def check_overdue(pool: Pool, drive: Drive, root: str, mail: Mail) -> None:
    """Alert once on stale and overdue laptop jobs (R-7.8, R-7.14, BR-D11, BR-D12).

    A job past ``agent_stale_after`` whose file the agent never picked up gets
    BR-D12, and one past ``confirm_timeout`` gets BR-D11. Each alert is claimed
    in its own transaction, so a later sweep sends nothing and one job can get
    both. Nothing is re-sent.
    """
    async with pool.connection() as conn:
        jobs = await list_open_jobs(conn, mail.agent_stale_after, mail.confirm_timeout)
    pending = set(await drive.list_names(f"{root}/_outbox"))
    for job in jobs:
        if job.is_stale and f"{job.job_id}.json" in pending:
            await _notify_stale(pool, job)
        if job.is_overdue:
            await _notify_overdue(pool, job)


async def _notify_stale(pool: Pool, job: OpenMailJob) -> None:
    """Send BR-D12 once for a job the agent has not picked up (R-7.14)."""
    async with pool.connection() as conn, conn.transaction():
        if not await claim_stale_alert(conn, job.job_id):
            return
        reading = await load_reading(conn, job.invoice_id)
        if reading is None:
            raise RuntimeError(f"invoice {job.invoice_id} cannot be asked about its mail")
        await queue_message(
            conn,
            await _sender(conn, job.invoice_id),
            MESSAGES[RULE_STALE].format(
                serie=reading.value("serie"),
                numero=reading.value("numero"),
                hours=job.age_hours,
            ),
            invoice_id=job.invoice_id,
            rule_id=RULE_STALE,
            buttons=[],
        )


async def _notify_overdue(pool: Pool, job: OpenMailJob) -> None:
    """Send BR-D11 once for a job still without a receipt (R-7.8)."""
    async with pool.connection() as conn, conn.transaction():
        if await claim_overdue_alert(conn, job.job_id):
            await send_not_confirmed(conn, job.invoice_id)


async def _apply_file(pool: Pool, drive: Drive, path: str, name: str, key: bytes) -> None:
    """Apply one receipt and delete it once the commit stands (R-7.14).

    The photo files a completed delivery frees are deleted first. Nothing is
    deleted when applying raises, so the next sweep retries the receipt.
    """
    receipt = await _read(drive, path, name, key)
    if receipt is None:
        return
    async with pool.connection() as conn, conn.transaction():
        photos = await _apply(conn, receipt)
    for photo in photos:
        await asyncio.to_thread(photo.unlink, missing_ok=True)
    await drive.delete(path)


async def _read(drive: Drive, path: str, name: str, key: bytes) -> Receipt | None:
    """Download, verify and parse one receipt, or log why it was skipped (R-7.14)."""
    try:
        return parse_receipt(await drive.download(path, RECEIPT_MAX_BYTES), key)
    except (FileTooLarge, ValueError) as err:
        logger.warning("receipt %s skipped: %s", name, err)
        return None


async def _apply(conn: AsyncConnection[Any], receipt: Receipt) -> list[Path]:
    """Apply a receipt for a known job still without one (R-7.8, R-7.14).

    A prepared batch follows today's behaviour. A batch already ``sent`` — an
    ``inbox_rule`` batch the laptop resent, or a late receipt after [Ya llegó] —
    only stamps the job and, for a sent receipt, queues BR-D10. Any other batch
    state stamps the job alone. Returns the photo paths a completed delivery
    frees, for deletion after the transaction commits.
    """
    job = await get_mail_job(conn, receipt.job_id)
    if job is None or job.has_receipt:
        return []
    state = await get_batch_state(conn, job.invoice_id)
    if state == PREPARED_BATCH_STATE:
        return await _apply_prepared(conn, job.invoice_id, receipt)
    if state == SENT_BATCH_STATE:
        await _apply_sent(conn, job.invoice_id, receipt)
    else:
        await _stamp(conn, receipt)
    return []


async def _apply_prepared(conn: AsyncConnection[Any], invoice_id: int, receipt: Receipt) -> list[Path]:
    """Apply a receipt to a prepared batch: close it or ask the sender (R-7.8, R-7.14)."""
    if receipt.sent_at is None:
        await _not_confirmed(conn, invoice_id, receipt.job_id, receipt.error or "")
        return []
    return await _sent(conn, invoice_id, receipt.job_id)


async def _apply_sent(conn: AsyncConnection[Any], invoice_id: int, receipt: Receipt) -> None:
    """Stamp a receipt on an already-sent batch without touching its state (R-7.8, R-7.14)."""
    if receipt.sent_at is None:
        await _not_confirmed(conn, invoice_id, receipt.job_id, receipt.error or "")
        return
    await _queue_done(conn, invoice_id)
    await set_receipt_at(conn, receipt.job_id)


async def _stamp(conn: AsyncConnection[Any], receipt: Receipt) -> None:
    """Record a receipt on a job whose batch is neither prepared nor sent (R-7.14)."""
    if receipt.sent_at is None:
        await set_receipt_error(conn, receipt.job_id, receipt.error or "")
    else:
        await set_receipt_at(conn, receipt.job_id)


async def _sent(conn: AsyncConnection[Any], invoice_id: int, job_id: str) -> list[Path]:
    """Close the batch as sent and the invoice DONE with BR-D10 (R-7.8, R-7.14)."""
    flow_id = await load_flow(conn, invoice_id)
    if flow_id is None:
        raise RuntimeError(f"invoice {invoice_id} has no flow")
    batch_id = await open_batch(conn, invoice_id, flow_id, PREPARED_BATCH_STATE)
    await set_batch_state(conn, batch_id, SENT_BATCH_STATE)
    await enqueue(conn, invoice_id, FOLDER_STATUS_STEP)
    await set_state(conn, invoice_id, DONE_STATE, rule_id=RULE_DONE)
    await _queue_done(conn, invoice_id)
    await set_receipt_at(conn, job_id)
    return await take_photo_paths(conn, invoice_id)


async def _queue_done(conn: AsyncConnection[Any], invoice_id: int) -> None:
    """Queue BR-D10 to the invoice's sender with the folder the delivery filed (R-7.8, R-7.14)."""
    folder = (await load_flow_data(conn, invoice_id))["folder"]
    await queue_message(
        conn,
        await _sender(conn, invoice_id),
        MESSAGES[RULE_DONE].format(folder=folder),
        invoice_id=invoice_id,
        rule_id=RULE_DONE,
        buttons=[],
    )


async def _not_confirmed(conn: AsyncConnection[Any], invoice_id: int, job_id: str, error: str) -> None:
    """Record an error receipt and ask the sender how the mail went out (BR-D11)."""
    await set_receipt_error(conn, job_id, error)
    await send_not_confirmed(conn, invoice_id)


async def _sender(conn: AsyncConnection[Any], invoice_id: int) -> Addressee:
    """Return the invoice's sender, the recipient of every receipt-driven message."""
    head = await load_invoice_head(conn, invoice_id)
    if head is None:
        raise RuntimeError(f"invoice {invoice_id} does not exist")
    return Addressee(head.origin_channel, head.profile_id)
