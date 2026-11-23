"""Send a stuck invoice back to the step that failed (S4.9, R-11.9)."""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection

from invoicebot.pipeline.deliver import DELIVER_STEP, FAILED_BATCH_STATE
from invoicebot.pipeline.fetch import FETCH_STEP
from invoicebot.queue.jobs import enqueue
from invoicebot.states import BATCHED, DELIVERING, FAILED, FETCHING_DOCS, MANUAL_REQUIRED
from invoicebot.store.flow_b import get_batch_state
from invoicebot.store.invoices import load_flow, set_state, start_fetch_round


async def retry_documents(conn: AsyncConnection[Any], invoice_id: int, state: str) -> bool:
    """Send a failed or manual invoice back to its fetch step in a new fetch round, if it has a flow.

    S4.9, D96, D166.
    """
    if state == FAILED and await load_flow(conn, invoice_id) is None:
        return False
    await set_state(conn, invoice_id, FETCHING_DOCS, expected=state)
    await start_fetch_round(conn, invoice_id)
    await enqueue(conn, invoice_id, FETCH_STEP)
    return True


async def retry_delivery(conn: AsyncConnection[Any], invoice_id: int) -> bool:
    """Send an invoice whose batch failed back to deliver, which resends it (S4.9, D96)."""
    if await get_batch_state(conn, invoice_id) != FAILED_BATCH_STATE:
        return False
    await set_state(conn, invoice_id, DELIVERING, expected=BATCHED)
    await enqueue(conn, invoice_id, DELIVER_STEP)
    return True


async def resume_delivery(conn: AsyncConnection[Any], invoice_id: int) -> None:
    """Send a manual invoice back to deliver, which resends its batch (R-11.9)."""
    await set_state(conn, invoice_id, DELIVERING, expected=MANUAL_REQUIRED)
    await enqueue(conn, invoice_id, DELIVER_STEP)
