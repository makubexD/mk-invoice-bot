"""Job handler that ends a captcha ask nobody answered in time and moves on (D209, BR-F05)."""

from __future__ import annotations

from pathlib import Path

from invoicebot.fetchers.base import FetchReason
from invoicebot.pipeline.captcha import drop_image
from invoicebot.pipeline.manual_message import PORTAL_FETCHER
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.steps import FETCH_STEP
from invoicebot.queue.jobs import Job, enqueue
from invoicebot.queue.worker import Handler
from invoicebot.rules.messages_es import MESSAGES
from invoicebot.states import FETCHING_DOCS, WAITING_CAPTCHA
from invoicebot.store import captchas
from invoicebot.store.db import Pool
from invoicebot.store.fetch_attempts import record_attempt
from invoicebot.store.invoices import load_invoice_head, lock_invoice, set_state

RULE_ID = "BR-F05"


def make_captcha_timeout_handler(pool: Pool, data_dir: Path) -> Handler:
    """Build the handler that drops an expired, unanswered captcha and resumes the fetch chain (D209).

    Under the invoice lock, only an invoice still ``WAITING_CAPTCHA`` with an expired row
    nobody answered is touched; the sender's answer or any other move wins otherwise.
    """

    async def handle(job: Job) -> None:
        image_path: str | None = None
        async with pool.connection() as conn, conn.transaction():
            await lock_invoice(conn, job.invoice_id)
            invoice = await load_invoice_head(conn, job.invoice_id)
            if invoice is None or invoice.state != WAITING_CAPTCHA:
                return
            pending = await captchas.get(conn, job.invoice_id)
            if pending is None or pending.answer is not None or not pending.expired:
                return
            image_path = pending.image_path
            await record_attempt(
                conn, job.invoice_id, PORTAL_FETCHER, pending.portal_key, FetchReason.CAPTCHA_TIMEOUT, 0
            )
            await captchas.delete(conn, job.invoice_id)
            await queue_message(
                conn,
                Addressee(invoice.origin_channel, invoice.profile_id),
                MESSAGES[RULE_ID],
                invoice_id=job.invoice_id,
                rule_id=RULE_ID,
            )
            await set_state(conn, job.invoice_id, FETCHING_DOCS, rule_id=RULE_ID, expected=WAITING_CAPTCHA)
            await enqueue(conn, job.invoice_id, FETCH_STEP)
        if image_path is not None:
            await drop_image(data_dir, image_path)

    return handle
