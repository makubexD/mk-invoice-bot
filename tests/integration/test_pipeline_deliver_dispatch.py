"""Integration tests for the deliver dispatcher (R-6.1, D132).

The handlers are recording fakes; Postgres is real. The dispatcher reads the invoice's flow
and calls the deliverer registered for it, and skips an invoice whose flow has none.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.app import _deliverers_by_flow
from invoicebot.config.loader import load_config
from invoicebot.pipeline.deliver_dispatch import make_deliver_dispatch
from invoicebot.pipeline.steps import DELIVER_STEP
from invoicebot.queue.jobs import Job
from invoicebot.queue.worker import Handler
from invoicebot.store.invoices import create_invoice, set_flow

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
FLOW_A = "personal_google"
FLOW_B = "work_microsoft"


def _recorder(calls: list[int]) -> Handler:
    """Return a deliverer that records the invoice ids it was asked to deliver (R-6.1)."""

    async def handle(job: Job) -> None:
        calls.append(job.invoice_id)

    return handle


def _job(invoice_id: int) -> Job:
    """Build the deliver job the dispatcher receives."""
    return Job(
        id=1,
        invoice_id=invoice_id,
        step=DELIVER_STEP,
        attempts=0,
        crashes=0,
        lease_until=datetime.now(UTC),
    )


async def _invoice(pool: AsyncConnectionPool[AsyncConnection[Any]], profile_id: str, flow: str | None) -> int:
    """Create an invoice, routed to ``flow`` unless it is None."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        if flow is not None:
            await set_flow(conn, invoice_id, flow)
    return invoice_id


def test_deliverers_by_flow_picks_each_flows_first_sink_kind() -> None:
    """R-6.1, D132: the example config maps flow A to Google and flow B to OneDrive."""
    cfg = load_config(EXAMPLE)
    google: Handler = _recorder([])
    onedrive: Handler = _recorder([])

    deliverers = _deliverers_by_flow(cfg, {"google_drive": google, "onedrive_folder": onedrive})

    assert deliverers == {FLOW_A: google, FLOW_B: onedrive}


@pytest.mark.asyncio(loop_scope="session")
async def test_flow_a_and_flow_b_invoices_reach_their_own_deliverers(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-6.1: a flow A invoice reaches the flow A deliverer and a flow B invoice the flow B one."""
    profile_id = f"dispatch-{uuid4().hex}"
    flow_a_calls: list[int] = []
    flow_b_calls: list[int] = []
    flow_a_invoice = await _invoice(db_pool, profile_id, FLOW_A)
    flow_b_invoice = await _invoice(db_pool, profile_id, FLOW_B)
    dispatch = make_deliver_dispatch(
        db_pool, {FLOW_A: _recorder(flow_a_calls), FLOW_B: _recorder(flow_b_calls)}
    )

    await dispatch(_job(flow_a_invoice))
    await dispatch(_job(flow_b_invoice))

    assert flow_a_calls == [flow_a_invoice]
    assert flow_b_calls == [flow_b_invoice]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_flow_without_a_deliverer_is_skipped(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-6.1: a flow absent from the mapping is skipped and neither deliverer runs."""
    profile_id = f"dispatch-{uuid4().hex}"
    flow_a_calls: list[int] = []
    flow_b_calls: list[int] = []
    routed_elsewhere = await _invoice(db_pool, profile_id, "archived_flow")
    dispatch = make_deliver_dispatch(
        db_pool, {FLOW_A: _recorder(flow_a_calls), FLOW_B: _recorder(flow_b_calls)}
    )

    await dispatch(_job(routed_elsewhere))

    assert flow_a_calls == []
    assert flow_b_calls == []


@pytest.mark.asyncio(loop_scope="session")
async def test_an_invoice_without_a_flow_is_skipped(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-6.1: an invoice that was never routed has no deliverer and neither one runs."""
    profile_id = f"dispatch-{uuid4().hex}"
    flow_a_calls: list[int] = []
    unrouted = await _invoice(db_pool, profile_id, None)
    dispatch = make_deliver_dispatch(db_pool, {FLOW_A: _recorder(flow_a_calls)})

    await dispatch(_job(unrouted))

    assert flow_a_calls == []
