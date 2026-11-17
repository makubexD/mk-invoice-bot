"""Job handler that asks SUNAT again and alerts the owner when the answer changed (BR-V01, D125).

:func:`make_sunat_recheck_handler` builds the handler of the ``sunat_recheck`` job step.
The validate step and each earlier recheck enqueue it after a BR-V01 (missing) or BR-V04
(unavailable) verdict. It asks SUNAT outside any transaction, stores the answer with an
incremented ``rechecks`` count, schedules another recheck while the count is below
:data:`MAX_RECHECKS`, and — when a later answer turns into BR-V02 or BR-V06 — tells the
admins without moving the invoice: the sender already accepted or answered it (D125).
A cancelled invoice, or one with no reading, makes the job end quietly.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import httpx
from psycopg import AsyncConnection

from invoicebot.config.models import Config
from invoicebot.extract.fields import Reading
from invoicebot.pipeline.outgoing import queue_admin_alert
from invoicebot.pipeline.steps import SUNAT_RECHECK_STEP
from invoicebot.pipeline.validate import ask_sunat, recheck_run_after
from invoicebot.queue.jobs import Job, enqueue
from invoicebot.queue.worker import Handler
from invoicebot.rules.messages_es import MESSAGES
from invoicebot.secrets import Secrets
from invoicebot.states import CANCELLED
from invoicebot.store.db import Pool
from invoicebot.store.invoices import (
    load_invoice_head,
    load_reading,
    set_sunat_status,
    sunat_rechecks,
)
from invoicebot.sunat.validez import CpStatus
from invoicebot.sunat.verdict import RULE_ANNULLED, RULE_NOT_AUTHORIZED, Verdict, decide

logger = logging.getLogger(__name__)

#: How many times SUNAT is asked again before the recheck stops (BR-V01, D125).
MAX_RECHECKS = 3

#: The admin rule that reports a question verdict a recheck can flip to (BR-V02, BR-V06).
_ADMIN_RULES: Mapping[str, str] = MappingProxyType(
    {RULE_ANNULLED: "BR-V02_admin", RULE_NOT_AUTHORIZED: "BR-V06_admin"}
)


@dataclass(frozen=True)
class _Recheck:
    """The invoice a recheck asks about and the count it carries when it starts (D125)."""

    invoice_id: int
    reading: Reading
    rechecks: int


def make_sunat_recheck_handler(pool: Pool, http: httpx.AsyncClient, secrets: Secrets, cfg: Config) -> Handler:
    """Build the handler that asks SUNAT again in ``sunat_recheck`` (BR-V01, D125).

    It reads the invoice and its count, asks SUNAT with no transaction held, then in one
    transaction stores the answer with the count incremented and, depending on the
    verdict, schedules the next recheck or alerts the admins. It never changes the
    invoice's state, so a state change another handler committed meanwhile is harmless.
    A cancelled invoice, or one with no reading, ends the job quietly.
    """

    async def handle(job: Job) -> None:
        started = time.perf_counter()
        recheck = await _read(pool, job.invoice_id)
        if recheck is None:
            return
        status = await ask_sunat(pool, http, secrets, cfg, recheck.reading)
        verdict = decide(status, None)
        async with pool.connection() as conn, conn.transaction():
            await _record(conn, recheck, cfg, status, verdict)
        logger.info(
            "step done",
            extra={
                "step": SUNAT_RECHECK_STEP,
                "invoice_id": job.invoice_id,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            },
        )

    return handle


async def _read(pool: Pool, invoice_id: int) -> _Recheck | None:
    """Load the invoice's reading and recheck count, or None when there is nothing to do.

    A missing invoice, a cancelled one or one without a reading ends the job quietly.
    """
    async with pool.connection() as conn:
        invoice = await load_invoice_head(conn, invoice_id)
        if invoice is None or invoice.state == CANCELLED:
            return None
        reading = await load_reading(conn, invoice_id)
        if reading is None:
            return None
        return _Recheck(
            invoice_id=invoice_id,
            reading=reading,
            rechecks=await sunat_rechecks(conn, invoice_id),
        )


async def _record(
    conn: AsyncConnection[Any],
    recheck: _Recheck,
    cfg: Config,
    status: CpStatus | None,
    verdict: Verdict,
) -> None:
    """Store the answer and act on the verdict in the caller's transaction (D125)."""
    count = recheck.rechecks + 1
    await set_sunat_status(conn, recheck.invoice_id, status, rechecks=count)
    if verdict.ask:
        await _alert(conn, cfg, recheck, verdict.rule_id)
    elif verdict.recheck and count < MAX_RECHECKS:
        run_after = await recheck_run_after(conn, verdict)
        await enqueue(conn, recheck.invoice_id, SUNAT_RECHECK_STEP, run_after)


async def _alert(conn: AsyncConnection[Any], cfg: Config, recheck: _Recheck, rule_id: str | None) -> None:
    """Tell the admins that a recheck found BR-V02 or BR-V06; the state stays put (D125)."""
    if rule_id is None:
        raise RuntimeError("a recheck ask verdict has no rule")
    admin_rule = _ADMIN_RULES[rule_id]
    text = MESSAGES[admin_rule].format(
        id=recheck.invoice_id,
        serie=recheck.reading.value("serie") or "",
        numero=recheck.reading.value("numero") or "",
    )
    await queue_admin_alert(conn, cfg.profiles, admin_rule, text)
