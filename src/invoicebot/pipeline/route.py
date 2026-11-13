"""Job handler that routes a read invoice and sends its summary (S4.2, R-3.1, R-3.4).

:func:`make_route_handler` builds the handler of the ``route`` job step. A routed invoice
stores its flow, gets one summary and waits in ``FETCHING_DOCS``; a refused one is
cancelled with the rule's message. The state change and the outbox row commit together.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, cast

from psycopg import AsyncConnection

from invoicebot.channels.base import ChannelName, OutboundMessage
from invoicebot.config.models import Config, Extraction, Flow
from invoicebot.extract.fields import Reading
from invoicebot.extract.qr_payload import INFERRED_CONFIDENCE
from invoicebot.extract.validate import check_date, doubtful_fields
from invoicebot.pipeline.extract import ROUTE_STEP
from invoicebot.queue import outbox
from invoicebot.queue.jobs import Job
from invoicebot.queue.worker import Handler
from invoicebot.routing.router import CompiledRule, route
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
from invoicebot.store.invoices import load_reading, set_state

logger = logging.getLogger(__name__)

#: The only state this handler acts on; any other leaves the invoice as it is (R-9.4).
ROUTING_STATE = "ROUTING"
#: Where a routed invoice waits; the ``fetch`` phase adds its next job.
ROUTED_STATE = "FETCHING_DOCS"
CANCELLED_STATE = "CANCELLED"
RULE_ASK = "BR-R03"
RULE_NOT_ALLOWED = "BR-R05"
RULE_UNMATCHED = "BR-R07"
RULE_WRONG_TIPO = "BR-R08"
#: Rules that cancel the invoice instead of routing it.
REFUSALS = frozenset({RULE_NOT_ALLOWED, RULE_UNMATCHED, RULE_WRONG_TIPO})
DATE_FIELD = "fecha_emision"


@dataclass(frozen=True)
class _Invoice:
    """What the handler needs from the invoice row."""

    state: str
    profile_id: str
    channel: ChannelName


def make_route_handler(pool: Pool, cfg: Config, rules: tuple[CompiledRule, ...]) -> Handler:
    """Build the handler that routes an invoice in ``ROUTING`` (S4.2).

    A matched flow is stored with its rule id, the summary goes to the sender and the
    invoice moves to ``FETCHING_DOCS`` with no job. BR-R05, BR-R07 and BR-R08 cancel it
    with their message. A missing reading or an unknown profile raises, so the queue
    retries.
    """

    async def handle(job: Job) -> None:
        started = time.perf_counter()
        async with pool.connection() as conn:
            invoice = await _load_invoice(conn, job.invoice_id)
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
                await set_state(conn, job.invoice_id, CANCELLED_STATE, rule_id=rule_id)
                await _reply(conn, invoice, rule_id, text)
        else:
            # shortcut: the summary has no [Editar] / [Cancelar] buttons until S4.5 and S4.9.
            text = _summary(reading, flow, rule_id, today, cfg.extraction)
            async with pool.connection() as conn, conn.transaction():
                await _set_flow(conn, job.invoice_id, cast(str, result.flow))
                await set_state(conn, job.invoice_id, ROUTED_STATE, rule_id=rule_id)
                await _reply(conn, invoice, rule_id, text)
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


async def _load_invoice(conn: AsyncConnection[Any], invoice_id: int) -> _Invoice | None:
    """Return the invoice's state, profile and origin channel, or None when it is gone."""
    cur = await conn.execute(
        "SELECT state, profile_id, origin_channel FROM invoices WHERE id = %s",
        (invoice_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return _Invoice(state=str(row[0]), profile_id=str(row[1]), channel=cast(ChannelName, row[2]))


async def _today(conn: AsyncConnection[Any], timezone: str) -> date:
    """Return today's date in the owner's timezone, from the database clock."""
    cur = await conn.execute("SELECT (now() AT TIME ZONE %s)::date", (timezone,))
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("the database returned no date")
    return cast(date, row[0])


async def _set_flow(conn: AsyncConnection[Any], invoice_id: int, flow_id: str) -> None:
    """Store the flow the invoice was routed to (R-3.1)."""
    await conn.execute("UPDATE invoices SET flow_id = %s WHERE id = %s", (flow_id, invoice_id))


async def _reply(conn: AsyncConnection[Any], invoice: _Invoice, rule_id: str, text: str) -> None:
    """Queue ``text`` for the invoice's sender in the caller's transaction."""
    message = OutboundMessage(
        channel=invoice.channel,
        recipient=invoice.profile_id,
        profile_id=invoice.profile_id,
        rule_id=rule_id,
        text=text,
    )
    await outbox.add(conn, invoice.channel, invoice.profile_id, message.model_dump(mode="json"))


def _refusal(rule_id: str, reading: Reading, flow: Flow | None) -> str:
    """Return the message of a refusing rule, its placeholders filled (spec 06 §1)."""
    if rule_id == RULE_NOT_ALLOWED and flow is not None:
        return MESSAGES[rule_id].format(flow=flow)
    if rule_id == RULE_WRONG_TIPO and flow is not None:
        return MESSAGES[rule_id].format(
            tipo_desc=tipo_phrase(_value(reading, "tipo")),
            flow=flow,
            accepted_desc=tipos_desc(flow.accepted_tipos),
        )
    buyer = buyer_desc(_value(reading, "tipo_doc_adq"), _value(reading, "num_doc_adq"))
    return MESSAGES[RULE_UNMATCHED].format(buyer_desc=buyer)


def _summary(reading: Reading, flow: Flow, rule_id: str, today: date, extraction: Extraction) -> str:
    """Return the routed-invoice summary (spec 06 §1 "Summary message", D79, D80)."""
    fecha = _fecha(reading)
    lines = [
        SUMMARY.format(
            tipo_desc=TIPO_DESC.get(_value(reading, "tipo"), _value(reading, "tipo")),
            serie=_value(reading, "serie"),
            numero=_value(reading, "numero"),
            fecha=_value(reading, DATE_FIELD) if fecha is None else fecha.strftime(SUMMARY_DATE_FORMAT),
            ruc_emisor=_value(reading, "ruc_emisor"),
            total=_value(reading, "total"),
            flow_line=MESSAGES[rule_id].format(flow=flow),
        )
    ]
    inferred = [name for name, field in reading.fields.items() if field.confidence == INFERRED_CONFIDENCE]
    if inferred:
        lines.append(SUMMARY_INFERRED.format(fields_desc=fields_desc(inferred)))
    doubtful = list(doubtful_fields(reading, extraction.review_threshold, extraction.igv_rates))
    date_is_doubtful = fecha is None or check_date(fecha, today, extraction.max_invoice_age_days) is not None
    if date_is_doubtful and DATE_FIELD not in doubtful:
        doubtful.append(DATE_FIELD)
    if doubtful:
        lines.append(SUMMARY_REVIEW.format(fields_desc=fields_desc(doubtful)))
    return "\n".join(lines)


def _value(reading: Reading, name: str) -> str:
    """Return the reading's value for ``name``, or an empty string when it is absent."""
    field = reading.fields.get(name)
    return "" if field is None else field.value


def _fecha(reading: Reading) -> date | None:
    """Return the issuing date, or None when it is missing or not ISO."""
    try:
        return date.fromisoformat(_value(reading, DATE_FIELD))
    except ValueError:
        return None
