"""Job handler that sends an invoice's kept documents after [📎 Ver documentos] (D206, BR-D24, BR-D25).

The files are listed off the event loop with no connection held. One document row is queued
per file, in name order, each captioned with its file name (BR-D24). When none is left,
BR-D25 says so and attaches the folder link flow B saved, when there is one. Flow A saves
no folder link, so its BR-D25 has none.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from invoicebot.pipeline.chat_docs import kept_documents
from invoicebot.pipeline.deliver import FOLDER_URL_KEY
from invoicebot.pipeline.outgoing import Addressee, outbound, queue_document, queue_outbound, safe_links
from invoicebot.queue.jobs import Job
from invoicebot.queue.worker import Handler
from invoicebot.rules.messages_es import DOCS_GONE, LINK_TITLE_FOLDER
from invoicebot.store.db import Pool
from invoicebot.store.flow_b import load_flow_data
from invoicebot.store.invoices import load_invoice_head

RULE_SENT = "BR-D24"
RULE_GONE = "BR-D25"


def make_send_documents_handler(pool: Pool, data_dir: Path) -> Handler:
    """Build the handler that queues the kept documents, or BR-D25 when they are gone (D206)."""

    async def handle(job: Job) -> None:
        documents = await asyncio.to_thread(kept_documents, data_dir, job.invoice_id)
        async with pool.connection() as conn, conn.transaction():
            invoice = await load_invoice_head(conn, job.invoice_id)
            if invoice is None:
                return
            to = Addressee(invoice.origin_channel, invoice.profile_id)
            for document in documents:
                await queue_document(
                    conn,
                    to,
                    document,
                    caption=document.filename,
                    invoice_id=job.invoice_id,
                    rule_id=RULE_SENT,
                )
            if documents:
                return
            url = (await load_flow_data(conn, job.invoice_id)).get(FOLDER_URL_KEY)
            links = safe_links([(LINK_TITLE_FOLDER, url)]) if isinstance(url, str) and url else []
            message = outbound(to, DOCS_GONE.format(id=job.invoice_id), rule_id=RULE_GONE, links=links)
            await queue_outbound(conn, message, invoice_id=job.invoice_id)

    return handle
