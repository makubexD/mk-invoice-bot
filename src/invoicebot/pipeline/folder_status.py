"""The ``folder_status`` job: converge an invoice's folder to its status name (R-7.11).

:func:`make_folder_status_handler` reads the invoice's flow data, batch state and
payment, then moves the folder to the name :func:`invoicebot.sinks.folders.status_name`
computes and records the new path in ``flow_data_json.folder_path``. A move that Graph
applied but the bot never recorded is found at the target and recorded instead of being
moved again; a folder at neither path raises :class:`FolderLost` and is never created.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from invoicebot.config.models import Config, OneDriveFolderSink
from invoicebot.pipeline.deliver import SENT_BATCH_STATE
from invoicebot.pipeline.steps import FOLDER_STATUS_STEP
from invoicebot.queue.jobs import Job
from invoicebot.queue.worker import Handler
from invoicebot.sinks.base import Drive
from invoicebot.sinks.folders import folder_parent, status_name
from invoicebot.sinks.onedrive import FolderExists, FolderMissing
from invoicebot.store.db import Pool
from invoicebot.store.flow_b import get_batch_state, load_flow_data, merge_flow_data
from invoicebot.store.invoices import load_flow

logger = logging.getLogger(__name__)


class FolderLost(RuntimeError):
    """The invoice's folder is neither at its recorded path nor at its target (R-7.11)."""


@dataclass(frozen=True)
class _Inputs:
    """The invoice state the job reads before any external call (R-7.11)."""

    flow_name: str | None
    flow_data: Mapping[str, Any]
    batch_state: str | None
    paid_on: date | None
    payment_total: Decimal | None


@dataclass(frozen=True)
class _Plan:
    """The folder's current path and the target it must converge to (R-7.11)."""

    root: str
    current: str
    target_parent: str
    target_name: str
    target: str
    year: int
    paid: bool


def make_folder_status_handler(pool: Pool, cfg: Config, drive: Drive) -> Handler:
    """Build the handler that converges one invoice's folder to its status name (R-7.11).

    An invoice without a folder yet, or whose flow has no OneDrive folder sink, is left
    alone. The folder moves from wherever it is now to the target name, so a payment
    recorded before the ``_Sent`` rename ran still ends in one correct name.
    """

    async def handle(job: Job) -> None:
        inputs = await _load_inputs(pool, job.invoice_id)
        sink = _folder_sink(cfg, inputs.flow_name)
        if inputs.flow_data.get("folder") is None or sink is None:
            logger.info(
                "step skipped",
                extra={
                    "step": FOLDER_STATUS_STEP,
                    "invoice_id": job.invoice_id,
                    "reason": "no_folder_sink" if sink is None else "no_folder",
                },
            )
            return
        plan = _plan(inputs, sink)
        if plan.current == plan.target:
            return
        await _ensure_year_folder(drive, plan)
        try:
            await drive.move(plan.current, plan.target_parent, plan.target_name)
        except (FolderMissing, FolderExists):
            await _recover_lost(pool, drive, job.invoice_id, plan)
            return
        await _record(pool, job.invoice_id, plan.target)

    return handle


async def _load_inputs(pool: Pool, invoice_id: int) -> _Inputs:
    """Read the invoice's flow data, batch state, payment and flow in one transaction."""
    async with pool.connection() as conn:
        flow_name = await load_flow(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        cur = await conn.execute(
            """
            SELECT p.paid_on, p.amount
            FROM invoices AS i
            LEFT JOIN payments AS p ON p.id = i.payment_id
            WHERE i.id = %s
            """,
            (invoice_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return _Inputs(flow_name, flow_data, batch_state, None, None)
    return _Inputs(flow_name, flow_data, batch_state, row[0], row[1])


def _folder_sink(cfg: Config, flow_name: str | None) -> OneDriveFolderSink | None:
    """Return the flow's OneDrive folder sink, or None without one (R-7.1)."""
    if flow_name is None:
        return None
    flow = cfg.flows.get(flow_name)
    if flow is None:
        return None
    for sink in flow.sinks:
        if isinstance(sink, OneDriveFolderSink):
            return sink
    return None


def _plan(inputs: _Inputs, sink: OneDriveFolderSink) -> _Plan:
    """Compute the folder's current path and its target name from the invoice state (R-7.11)."""
    base = str(inputs.flow_data["folder"])
    year = int(inputs.flow_data["year"])
    sent = inputs.batch_state == SENT_BATCH_STATE
    paid = inputs.paid_on is not None
    target_name = status_name(base, sent, inputs.paid_on, inputs.payment_total)
    target_parent = folder_parent(sink.root, year, paid)
    saved = inputs.flow_data.get("folder_path")
    current = saved if isinstance(saved, str) and saved else f"{sink.root}/{base}"
    return _Plan(
        root=sink.root,
        current=current,
        target_parent=target_parent,
        target_name=target_name,
        target=f"{target_parent}/{target_name}",
        year=year,
        paid=paid,
    )


async def _ensure_year_folder(drive: Drive, plan: _Plan) -> None:
    """Create the payment's year folder once, before the move (R-7.11)."""
    if not plan.paid:
        return
    year_name = str(plan.year)
    if year_name not in await drive.list_names(plan.root):
        await drive.create_folder(plan.root, year_name)


async def _recover_lost(pool: Pool, drive: Drive, invoice_id: int, plan: _Plan) -> None:
    """Record a move Graph already applied, or report the folder as lost (R-7.11)."""
    if plan.target_name in await drive.list_names(plan.target_parent):
        await _record(pool, invoice_id, plan.target)
        return
    logger.error("folder_lost", extra={"invoice_id": invoice_id})
    raise FolderLost(invoice_id)


async def _record(pool: Pool, invoice_id: int, target: str) -> None:
    """Record the folder's path in its own transaction (R-7.11)."""
    async with pool.connection() as conn, conn.transaction():
        await merge_flow_data(conn, invoice_id, {"folder_path": target})
