"""Job handler that sends a deliver job to the deliverer of the invoice's flow (D132).

A flow A invoice files through Google and a flow B invoice through OneDrive, so the
``deliver`` step cannot be one handler. :func:`make_deliver_dispatch` reads the flow in one
short connection, releases it before calling the flow's handler, and skips an invoice whose
flow has no deliverer instead of failing its job.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from invoicebot.pipeline.steps import DELIVER_STEP
from invoicebot.queue.jobs import Job
from invoicebot.queue.worker import Handler
from invoicebot.store.db import Pool
from invoicebot.store.invoices import load_flow

logger = logging.getLogger(__name__)


def make_deliver_dispatch(pool: Pool, by_flow: Mapping[str, Handler]) -> Handler:
    """Build the ``deliver`` step's handler from one deliverer per flow name (D132).

    The flow is read in a short connection that is released before the flow's handler
    runs, so the handler owns its own transaction. An invoice with no flow, or a flow
    with no deliverer, logs ``step skipped`` and returns.
    """

    async def handle(job: Job) -> None:
        async with pool.connection() as conn:
            flow = await load_flow(conn, job.invoice_id)
        handler = by_flow.get(flow) if flow is not None else None
        if handler is None:
            logger.info(
                "step skipped",
                extra={"step": DELIVER_STEP, "invoice_id": job.invoice_id, "reason": "no_deliverer"},
            )
            return
        await handler(job)

    return handle
