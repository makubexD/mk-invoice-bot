"""Job handler that validates a routed invoice with SUNAT and saves its summary inputs (R-4.1, D124, D206).

:func:`make_validate_handler` builds the handler of the ``validate`` job step. In
``VALIDATING`` it reads the routed invoice and its padrón row, asks the SUNAT validez
API when the adapter's secrets are present and, in one transaction, stores the answer,
applies BR-V01..BR-V06's verdict and either moves the invoice to ``FETCHING_DOCS`` with
its ``fetch`` and ``fetch_progress`` jobs and the summary's inputs saved (the SUNAT lines
included; the fetch step posts the summary, D206), or to ``CONFIRM_INVALID`` with
BR-V02/BR-V06's question. Without secrets (validation off, D124) it saves the inputs with
no SUNAT line and moves on. The token fetch and the validez call
run with no connection held, except the short, separate ``kv`` reads, writes and deletes
the token makes on connections of its own.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import httpx
from psycopg import AsyncConnection

from invoicebot.channels.base import Button
from invoicebot.config.models import Config
from invoicebot.extract.fields import Reading
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.questions import invalid_question
from invoicebot.pipeline.steps import FETCH_PROGRESS_STEP, FETCH_STEP, SUNAT_RECHECK_STEP
from invoicebot.pipeline.summary_post import discard_summary_inputs, save_summary_inputs
from invoicebot.queue.jobs import Job, enqueue
from invoicebot.queue.retry import backoff_minutes
from invoicebot.queue.worker import Handler
from invoicebot.rules.messages_es import MESSAGES
from invoicebot.secrets import Secrets
from invoicebot.states import CONFIRM_INVALID, FETCHING_DOCS, VALIDATING, StateMoved
from invoicebot.store.db import Pool
from invoicebot.store.invoices import (
    CpStatus,
    InvoiceHead,
    get_state_rule,
    load_flow,
    load_invoice_head,
    load_reading,
    set_state,
    set_sunat_status,
    start_fetch_round,
)
from invoicebot.store.padron import PadronRow, lookup
from invoicebot.sunat.token import SunatUnavailable, forget, token
from invoicebot.sunat.validez import SunatUnauthorized
from invoicebot.sunat.validez import validate as sunat_validate
from invoicebot.sunat.verdict import RULE_ISSUER_STATE, RULE_MISSING, Verdict, decide

logger = logging.getLogger(__name__)

#: The job step this handler serves; routing enqueues it in ``VALIDATING`` (D124).
VALIDATE_STEP = "validate"
#: The recheck delay for a comprobante SUNAT does not know yet (BR-V01, D125).
MISSING_RECHECK_HOURS = 24
#: Validez calls per answer: the first, and one retry after a 401 drops the token (R-4.3).
VALIDEZ_ATTEMPTS = 2


@dataclass(frozen=True)
class _Inputs:
    """The validate handler's inputs, read once before the SUNAT call runs (D125)."""

    invoice: InvoiceHead
    invoice_id: int
    reading: Reading
    rule_id: str
    padron: PadronRow | None


def make_validate_handler(
    pool: Pool, http: httpx.AsyncClient, secrets: Secrets | None, cfg: Config
) -> Handler:
    """Build the handler that validates an invoice in ``VALIDATING`` (R-4.1, D124, D125).

    ``secrets is None`` means validation is off: the handler only saves the summary's
    inputs and moves the invoice on (the fetch step posts the summary, D206). With
    secrets it stores the SUNAT answer and acts on :func:`invoicebot.sunat.verdict.decide`:
    a question moves the invoice to ``CONFIRM_INVALID``, a recheck schedules a
    ``sunat_recheck`` job, and the saved inputs gain the verdict's line and BR-V03's. A
    missing reading, flow or rule is a programming error, so the queue retries. An
    invoice a cancel or edit moved out of ``VALIDATING`` while SUNAT was being asked ends
    the job quietly: the final transaction rolls back, so the answer is not stored (D95).
    """

    async def handle(job: Job) -> None:
        started = time.perf_counter()
        async with pool.connection() as conn:
            inputs = await _read(conn, job.invoice_id, cfg)
        if inputs is None:
            return
        if secrets is None:
            try:
                async with pool.connection() as conn, conn.transaction():
                    await _routed_off(conn, inputs, cfg)
            except StateMoved:
                # D95: a cancel or edit after the read above wins; nothing was written.
                return
        else:
            status = await ask_sunat(pool, http, secrets, cfg, inputs.reading)
            verdict = decide(status, inputs.padron)
            try:
                async with pool.connection() as conn, conn.transaction():
                    await set_sunat_status(conn, job.invoice_id, status, rechecks=0)
                    await _apply(conn, inputs, cfg, verdict)
            except StateMoved:
                # D95: a cancel or edit during the SUNAT call wins; the rollback also
                # drops set_sunat_status, so the answer is not stored.
                return
        logger.info(
            "step done",
            extra={
                "step": VALIDATE_STEP,
                "invoice_id": job.invoice_id,
                "rule_id": inputs.rule_id,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            },
        )

    return handle


async def _read(conn: AsyncConnection[Any], invoice_id: int, cfg: Config) -> _Inputs | None:
    """Load the routed invoice and its padrón row (D125).

    Returns None when the invoice does not exist or is not in ``VALIDATING``, so the
    job ends quietly. A missing reading, flow or rule raises for the queue to retry.
    """
    invoice = await load_invoice_head(conn, invoice_id)
    if invoice is None or invoice.state != VALIDATING:
        return None
    reading = await load_reading(conn, invoice_id)
    flow_id = await load_flow(conn, invoice_id)
    state_rule = await get_state_rule(conn, invoice_id)
    rule_id = None if state_rule is None else state_rule[1]
    if reading is None:
        raise RuntimeError(f"invoice {invoice_id} has no reading")
    if flow_id is None or rule_id is None:
        raise RuntimeError(f"invoice {invoice_id} has no routed flow or rule")
    if flow_id not in cfg.flows:
        raise RuntimeError(f"invoice {invoice_id} has an unknown flow")
    return _Inputs(
        invoice=invoice,
        invoice_id=invoice_id,
        reading=reading,
        rule_id=rule_id,
        padron=await lookup(conn, reading.value("ruc_emisor") or ""),
    )


async def ask_sunat(
    pool: Pool,
    http: httpx.AsyncClient,
    secrets: Secrets,
    cfg: Config,
    reading: Reading,
) -> CpStatus | None:
    """Return SUNAT's answer, or None when it could not be asked (R-4.1, R-4.3, D125).

    The token's cache read and write each use their own short connection; the fetch and
    the validez call run with no connection held. A validez 401 drops the cached token
    and retries once with a freshly fetched one (R-4.3); a second 401, like a token
    failure or any other ``SunatUnavailable``, reads as no answer, which ``decide``
    turns into BR-V04.
    """
    for _ in range(VALIDEZ_ATTEMPTS):
        try:
            access_token = await token(pool, http, secrets, cfg.sunat.validez_api)
        except SunatUnavailable:
            return None
        try:
            return await sunat_validate(http, access_token, cfg.sunat.validez_api, cfg.owner.ruc, reading)
        except SunatUnauthorized:
            await forget(pool, access_token)
        except SunatUnavailable:
            return None
    return None


async def _routed_off(conn: AsyncConnection[Any], inputs: _Inputs, cfg: Config) -> None:
    """Save the summary's inputs and move the invoice on, with no SUNAT line (D124, D206)."""
    await save_summary_inputs(conn, inputs.invoice_id, rule_id=inputs.rule_id, extra_lines=())
    await _start_fetching(conn, inputs, cfg)


async def _apply(conn: AsyncConnection[Any], inputs: _Inputs, cfg: Config, verdict: Verdict) -> None:
    """Apply the verdict in the caller's transaction (D125, D206)."""
    if verdict.ask:
        await _ask(conn, inputs, verdict)
        return
    await save_summary_inputs(
        conn, inputs.invoice_id, rule_id=inputs.rule_id, extra_lines=_extra_lines(verdict)
    )
    await _start_fetching(conn, inputs, cfg)
    run_after = await recheck_run_after(conn, verdict, 1)
    if run_after is not None:
        await enqueue(conn, inputs.invoice_id, SUNAT_RECHECK_STEP, run_after)


async def _start_fetching(conn: AsyncConnection[Any], inputs: _Inputs, cfg: Config) -> None:
    """Move to ``FETCHING_DOCS``, start a fetch round and queue the fetch and its progress note (D206)."""
    await set_state(conn, inputs.invoice_id, FETCHING_DOCS, rule_id=inputs.rule_id, expected=VALIDATING)
    await start_fetch_round(conn, inputs.invoice_id)
    await enqueue(conn, inputs.invoice_id, FETCH_STEP)
    progress_at = await _now_plus(conn, seconds=cfg.limits.fetch_progress_after_s)
    await enqueue(conn, inputs.invoice_id, FETCH_PROGRESS_STEP, progress_at)


async def _ask(conn: AsyncConnection[Any], inputs: _Inputs, verdict: Verdict) -> None:
    """Ask the sender about a BR-V02/BR-V06 comprobante and wait in ``CONFIRM_INVALID`` (D125)."""
    rule_id = verdict.rule_id
    if rule_id is None:
        raise RuntimeError("an ask verdict has no rule")
    text, buttons = invalid_question(inputs.invoice_id, rule_id)
    await set_state(conn, inputs.invoice_id, CONFIRM_INVALID, rule_id=rule_id, expected=VALIDATING)
    await discard_summary_inputs(conn, inputs.invoice_id)
    await _reply(conn, inputs.invoice, inputs.invoice_id, rule_id, text, buttons)


def _extra_lines(verdict: Verdict) -> tuple[str, ...]:
    """Return the summary's SUNAT lines: the verdict's rule and BR-V03 when present (D125)."""
    lines: list[str] = []
    if verdict.rule_id is not None:
        lines.append(MESSAGES[verdict.rule_id])
    if verdict.issuer_line is not None:
        estado, condicion = verdict.issuer_line
        lines.append(MESSAGES[RULE_ISSUER_STATE].format(estado=estado, condicion=condicion))
    return tuple(lines)


async def recheck_run_after(conn: AsyncConnection[Any], verdict: Verdict, attempt: int) -> datetime | None:
    """Return when a recheck job runs, or None when the verdict schedules none (D125).

    ``attempt`` is the 1-based number of the recheck being scheduled. BR-V01 rechecks
    tomorrow; BR-V04 rechecks on the queue's backoff for that attempt, so each recheck
    waits longer than the one before. The time is the database clock's, never Python's.
    """
    if not verdict.recheck:
        return None
    if verdict.rule_id == RULE_MISSING:
        return await _now_plus(conn, hours=MISSING_RECHECK_HOURS)
    return await _now_plus(conn, minutes=backoff_minutes(attempt))


async def _now_plus(
    conn: AsyncConnection[Any], *, hours: int = 0, minutes: int = 0, seconds: int = 0
) -> datetime:
    """Return ``now() + hours/minutes/seconds`` from the database clock."""
    cur = await conn.execute(
        "SELECT now() + make_interval(hours => %s, mins => %s, secs => %s)", (hours, minutes, seconds)
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("the database returned no time")
    return cast(datetime, row[0])


async def _reply(
    conn: AsyncConnection[Any],
    invoice: InvoiceHead,
    invoice_id: int,
    rule_id: str,
    text: str,
    buttons: Sequence[Button] = (),
) -> None:
    """Queue ``text`` for the invoice's sender in the caller's transaction."""
    await queue_message(
        conn,
        Addressee(invoice.origin_channel, invoice.profile_id),
        invoice_id=invoice_id,
        rule_id=rule_id,
        text=text,
        buttons=buttons,
    )
