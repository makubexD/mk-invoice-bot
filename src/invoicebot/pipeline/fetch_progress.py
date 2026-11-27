"""Job handler that tells the sender the files are still being looked for (D206, BR-F17).

Validate schedules this step a little after the invoice is validated. If the fetch step has
not posted the summary by then, the sender gets one short progress note; a ``noted`` mark in
the saved summary inputs keeps it to one note even if the job runs twice.
"""

from __future__ import annotations

from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.summary_post import SUMMARY_KEY
from invoicebot.queue.jobs import Job
from invoicebot.queue.worker import Handler
from invoicebot.rules.messages_es import FETCH_PROGRESS
from invoicebot.states import FETCHING_DOCS
from invoicebot.store.db import Pool
from invoicebot.store.flow_b import load_flow_data, merge_flow_data
from invoicebot.store.invoices import load_invoice_head, lock_invoice

RULE_ID = "BR-F17"


def make_fetch_progress_handler(pool: Pool) -> Handler:
    """Build the handler that queues the BR-F17 note while the summary is still unposted (D206)."""

    async def handle(job: Job) -> None:
        async with pool.connection() as conn, conn.transaction():
            await lock_invoice(conn, job.invoice_id)
            invoice = await load_invoice_head(conn, job.invoice_id)
            if invoice is None or invoice.state != FETCHING_DOCS:
                return
            saved = (await load_flow_data(conn, job.invoice_id)).get(SUMMARY_KEY)
            if not isinstance(saved, dict) or saved.get("posted") or saved.get("noted"):
                return
            await queue_message(
                conn,
                Addressee(invoice.origin_channel, invoice.profile_id),
                invoice_id=job.invoice_id,
                rule_id=RULE_ID,
                text=FETCH_PROGRESS.format(id=job.invoice_id),
            )
            await merge_flow_data(conn, job.invoice_id, {SUMMARY_KEY: {**saved, "noted": True}})

    return handle
