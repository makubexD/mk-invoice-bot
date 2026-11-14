"""Job handler that routes a read invoice and sends its summary (S4.2, R-3.1, R-3.4, R-7.13).

:func:`make_route_handler` builds the handler of the ``route`` job step. A routed invoice
stores its flow, gets one summary, queues its ``fetch`` job and waits in ``FETCHING_DOCS``;
one above its flow's reimbursement cap waits in ``CONFIRMING`` for [Continuar] / [Cancelar]
with BR-D06's question instead (S11.13); a refused one is cancelled with the rule's message.
The state change, the fetch job and the outbox row commit together.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from datetime import date
from typing import Any, cast

from psycopg import AsyncConnection

from invoicebot.channels.base import Button
from invoicebot.config.models import Config, Extraction, Flow, StoreDirectory
from invoicebot.extract.fields import Reading
from invoicebot.extract.validate import check_date, doubtful_fields
from invoicebot.pipeline.actions import RULE_CAP
from invoicebot.pipeline.extract import CANCELLED_STATE, ROUTE_STEP
from invoicebot.pipeline.fetch import FETCH_STEP
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.questions import cap_question
from invoicebot.queue.jobs import Job, enqueue
from invoicebot.queue.worker import Handler
from invoicebot.routing.router import (
    REFUSALS,
    RULE_ASK,
    RULE_NOT_ALLOWED,
    RULE_UNMATCHED,
    RULE_WRONG_TIPO,
    CompiledRule,
    route,
)
from invoicebot.rules.messages_es import (
    MESSAGES,
    SUMMARY,
    SUMMARY_DATE_FORMAT,
    SUMMARY_INFERRED,
    SUMMARY_REVIEW,
    TIPO_DESC,
    buyer_desc,
    fields_desc,
    tipo_phrase,
    tipos_desc,
)
from invoicebot.store.db import Pool
from invoicebot.store.invoices import InvoiceHead, load_invoice_head, load_reading, set_flow, set_state

logger = logging.getLogger(__name__)

#: The only state this handler acts on; any other leaves the invoice as it is (R-9.4).
ROUTING_STATE = "ROUTING"
#: Where a routed invoice waits; its ``fetch`` job is queued with this state change.
ROUTED_STATE = "FETCHING_DOCS"
#: Where an invoice above the reimbursement cap waits for [Continuar] / [Cancelar] (R-7.13).
CONFIRMING_STATE = "CONFIRMING"
DATE_FIELD = "fecha_emision"


def make_route_handler(
    pool: Pool, cfg: Config, directory: StoreDirectory, rules: tuple[CompiledRule, ...]
) -> Handler:
    """Build the handler that routes an invoice in ``ROUTING`` (S4.2).

    A matched flow is stored with its rule id, the summary goes to the sender and the
    invoice moves to ``FETCHING_DOCS`` with its ``fetch`` job queued. BR-R05, BR-R07 and
    BR-R08 cancel it with their message. A missing reading or an unknown profile raises,
    so the queue retries.
    """
    # BR-R09's brand question, added in part 2, reads the issuer's store from here.
    _ = directory

    async def handle(job: Job) -> None:
        started = time.perf_counter()
        async with pool.connection() as conn:
            invoice = await load_invoice_head(conn, job.invoice_id)
            if invoice is None or invoice.state != ROUTING_STATE:
                return
            reading = await load_reading(conn, job.invoice_id)
            today = await _today(conn, cfg.owner.timezone)
        if reading is None:
            raise RuntimeError(f"invoice {job.invoice_id} has no reading")
        profile = cfg.profiles.get(invoice.profile_id)
        if profile is None:
            raise RuntimeError(f"invoice {job.invoice_id} has an unknown profile")

        result = route(reading, profile.flows, rules, cfg.flows, cfg.routing.unmatched)
        # shortcut: `unmatched: ask` is answered as `reject` until S4.3 lands in widen-b.
        rule_id = RULE_UNMATCHED if result.rule == RULE_ASK else result.rule
        flow = None if result.flow is None else cfg.flows[result.flow]

        if rule_id in REFUSALS or flow is None:
            text = _refusal(rule_id, reading, flow)
            async with pool.connection() as conn, conn.transaction():
                await set_state(
                    conn, job.invoice_id, CANCELLED_STATE, rule_id=rule_id, expected=ROUTING_STATE
                )
                await _reply(conn, invoice, job.invoice_id, rule_id, text)
        else:
            # shortcut: the summary has no [Editar] button until S4.5.
            text = _summary(reading, flow, rule_id, today, cfg.extraction)
            question = cap_question(reading, flow, job.invoice_id)
            async with pool.connection() as conn, conn.transaction():
                await set_flow(conn, job.invoice_id, cast(str, result.flow))
                await _routed(conn, invoice, job.invoice_id, rule_id, text, question)
        logger.info(
            "step done",
            extra={
                "step": ROUTE_STEP,
                "invoice_id": job.invoice_id,
                "rule_id": rule_id,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            },
        )

    return handle


async def _today(conn: AsyncConnection[Any], timezone: str) -> date:
    """Return today's date in the owner's timezone, from the database clock."""
    cur = await conn.execute("SELECT (now() AT TIME ZONE %s)::date", (timezone,))
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("the database returned no date")
    return cast(date, row[0])


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


async def _routed(
    conn: AsyncConnection[Any],
    invoice: InvoiceHead,
    invoice_id: int,
    rule_id: str,
    text: str,
    cap_question: tuple[str, list[Button]] | None,
) -> None:
    """Queue the fetch job, or BR-D06's question when the total is above the cap (R-7.13).

    Both writes require the invoice to still be in ``ROUTING``, so one moved on by
    ``/cancelar`` or an edit keeps its own state (D95).
    """
    if cap_question is None:
        await set_state(conn, invoice_id, ROUTED_STATE, rule_id=rule_id, expected=ROUTING_STATE)
        await enqueue(conn, invoice_id, FETCH_STEP)
        await _reply(conn, invoice, invoice_id, rule_id, text)
        return
    question, buttons = cap_question
    await set_state(conn, invoice_id, CONFIRMING_STATE, rule_id=RULE_CAP, expected=ROUTING_STATE)
    await _reply(conn, invoice, invoice_id, RULE_CAP, f"{text}\n{question}", buttons)


def _refusal(rule_id: str, reading: Reading, flow: Flow | None) -> str:
    """Return the message of a refusing rule, its placeholders filled (spec 06 §1)."""
    if rule_id == RULE_NOT_ALLOWED and flow is not None:
        return MESSAGES[rule_id].format(flow=flow)
    if rule_id == RULE_WRONG_TIPO and flow is not None:
        return MESSAGES[rule_id].format(
            tipo_desc=tipo_phrase(reading.value("tipo") or ""),
            flow=flow,
            accepted_desc=tipos_desc(flow.accepted_tipos),
        )
    buyer = buyer_desc(reading.value("tipo_doc_adq") or "", reading.value("num_doc_adq") or "")
    return MESSAGES[RULE_UNMATCHED].format(buyer_desc=buyer)


def _summary(reading: Reading, flow: Flow, rule_id: str, today: date, extraction: Extraction) -> str:
    """Return the routed-invoice summary (spec 06 §1 "Summary message", D79, D80)."""
    fecha = reading.fecha()
    tipo = reading.value("tipo") or ""
    lines = [
        SUMMARY.format(
            tipo_desc=TIPO_DESC.get(tipo, tipo),
            serie=reading.value("serie") or "",
            numero=reading.value("numero") or "",
            fecha=(reading.value(DATE_FIELD) or "") if fecha is None else fecha.strftime(SUMMARY_DATE_FORMAT),
            ruc_emisor=reading.value("ruc_emisor") or "",
            total=reading.value("total") or "",
            flow_line=MESSAGES[rule_id].format(flow=flow),
        )
    ]
    inferred = [name for name, field in reading.fields.items() if field.flag == "inferred"]
    if inferred:
        lines.append(SUMMARY_INFERRED.format(fields_desc=fields_desc(inferred)))
    doubtful = list(doubtful_fields(reading, extraction.review_threshold, extraction.igv_rates))
    date_is_doubtful = fecha is None or check_date(fecha, today, extraction.max_invoice_age_days) is not None
    if date_is_doubtful and DATE_FIELD not in doubtful:
        doubtful.append(DATE_FIELD)
    if doubtful:
        lines.append(SUMMARY_REVIEW.format(fields_desc=fields_desc(doubtful)))
    return "\n".join(lines)
