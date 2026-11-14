"""Job handler that routes a read invoice through its questions and sends its summary.

:func:`make_route_handler` builds the handler of the ``route`` job step. In ``ROUTING`` it
first claims the invoice's natural key (BR-E04, D93), then runs D92's ordered, resumable
checks over the stored answers: flow (rule match, else BR-R03),
buyer confidence (BR-R04), reimbursement cap (BR-D06) and brand (BR-R09). The first
unanswered check moves the invoice to ``CONFIRMING`` with its rule and its question; when
every check passes the invoice stores its flow, gets one summary, queues its ``fetch`` job
and waits in ``FETCHING_DOCS`` (S11.13). A refused invoice is cancelled with the rule's
message. The state change, the next job and the outbox row commit together.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection

from invoicebot.channels.base import Button
from invoicebot.config.models import Config, Extraction, Flow, Profile, StoreDirectory
from invoicebot.extract.fields import Reading
from invoicebot.extract.validate import check_date, doubtful_fields
from invoicebot.pipeline.extract import CANCELLED_STATE, ROUTE_STEP
from invoicebot.pipeline.fetch import FETCH_STEP
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.questions import (
    RULE_BRAND,
    RULE_BUYER,
    RULE_CAP,
    brand_question,
    buyer_question,
    candidate_flows,
    cap_question,
    flow_question,
    summary_buttons,
)
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
from invoicebot.sinks.folders import comercio_for
from invoicebot.store.db import Pool
from invoicebot.store.flow_b import load_flow_data, merge_flow_data
from invoicebot.store.invoices import (
    Duplicate,
    InvoiceHead,
    claim_natural_key,
    load_invoice_head,
    load_reading,
    set_flow,
    set_state,
)

logger = logging.getLogger(__name__)

#: The rule that cancels an invoice whose natural key another invoice holds (BR-E04, D93).
RULE_DUPLICATE = "BR-E04"
#: The reading fields that make up an invoice's natural key, in key order (D93).
NATURAL_KEY_FIELDS = ("ruc_emisor", "tipo", "serie", "numero")
#: The only state this handler acts on; any other leaves the invoice as it is (R-9.4).
ROUTING_STATE = "ROUTING"
#: Where a routed invoice waits; its ``fetch`` job is queued with this state change.
ROUTED_STATE = "FETCHING_DOCS"
#: Where an invoice waits for an answer to one of D92's questions.
CONFIRMING_STATE = "CONFIRMING"
DATE_FIELD = "fecha_emision"

#: The summary's flow line for a flow the sender picked in the BR-R03 menu (spec 06 §1).
#: BR-R01 and BR-R02 already carry this same ``➡️ {flow.label}`` line as their rule message,
#: so the summary reuses that catalog text instead of copying it into this module.
PICKED_FLOW_LINE = MESSAGES["BR-R02"]


@dataclass(frozen=True)
class _Resolved:
    """The flow check's answer: the flow to continue with and the rule that decided it (D92)."""

    flow_id: str
    flow: Flow
    rule_id: str


@dataclass(frozen=True)
class _Step:
    """The one write the handler performs once the ordered checks decide (D92)."""

    kind: Literal["cancel", "ask", "ask_flow", "route"]
    rule_id: str
    text: str
    buttons: Sequence[Button] = ()
    flow_id: str | None = None
    #: What a question's buttons offer, in button order, stored for its answer (BR-R03, BR-R09).
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Decision:
    """The route handler's inputs, read once before the checks run (D92)."""

    invoice_id: int
    reading: Reading
    profile: Profile
    cfg: Config
    rules: tuple[CompiledRule, ...]
    answers: Mapping[str, Any]
    directory: StoreDirectory
    today: date


def make_route_handler(
    pool: Pool, cfg: Config, directory: StoreDirectory, rules: tuple[CompiledRule, ...]
) -> Handler:
    """Build the handler that routes an invoice in ``ROUTING`` (S4.2, S4.3, S4.4, D92).

    The ordered checks are :func:`_resolve_flow`, :func:`_buyer_check`, :func:`_cap_check`
    and :func:`_brand_check`; a button answer stores its value and enqueues the route job
    again (step 12), so a re-run skips the answered checks. BR-R05, BR-R07 and BR-R08 cancel
    the invoice with their message. A missing reading or an unknown profile raises, so the
    queue retries.
    """

    async def handle(job: Job) -> None:
        started = time.perf_counter()
        async with pool.connection() as conn:
            invoice = await load_invoice_head(conn, job.invoice_id)
            if invoice is None or invoice.state != ROUTING_STATE:
                return
            reading = await load_reading(conn, job.invoice_id)
            flow_data = await load_flow_data(conn, job.invoice_id)
            today = await _today(conn, cfg.owner.timezone)
        if reading is None:
            raise RuntimeError(f"invoice {job.invoice_id} has no reading")
        profile = cfg.profiles.get(invoice.profile_id)
        if profile is None:
            raise RuntimeError(f"invoice {job.invoice_id} has an unknown profile")
        raw_answers = flow_data.get("answers")
        answers: Mapping[str, Any] = raw_answers if isinstance(raw_answers, dict) else {}
        async with pool.connection() as conn, conn.transaction():
            duplicate = await _claim(conn, job.invoice_id, invoice.profile_id, reading)
            if duplicate is not None:
                await _cancel_duplicate(conn, invoice, job.invoice_id, duplicate, cfg.owner.timezone)
                rule_id = RULE_DUPLICATE
            else:
                step = _decide(
                    _Decision(
                        invoice_id=job.invoice_id,
                        reading=reading,
                        profile=profile,
                        cfg=cfg,
                        rules=rules,
                        answers=answers,
                        directory=directory,
                        today=today,
                    )
                )
                await _write(conn, invoice, job.invoice_id, step)
                rule_id = step.rule_id
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


def _decide(decision: _Decision) -> _Step:
    """Run D92's ordered checks and return the write they decide (S4.3, S4.4, BR-R09)."""
    resolved = _resolve_flow(
        decision.reading,
        decision.profile,
        decision.cfg,
        decision.rules,
        decision.answers,
        decision.invoice_id,
    )
    if isinstance(resolved, _Step):
        return resolved
    buyer = _buyer_check(
        decision.reading,
        resolved,
        decision.profile,
        decision.cfg,
        decision.answers,
        decision.invoice_id,
    )
    if buyer is not None:
        return buyer
    summary = _summary(
        decision.reading,
        _flow_line(resolved.flow, resolved.rule_id),
        decision.today,
        decision.cfg.extraction,
    )
    cap = _cap_check(decision.reading, resolved, decision.answers, decision.invoice_id, summary)
    if cap is not None:
        return cap
    brand = _brand_check(
        decision.reading,
        decision.answers,
        decision.directory,
        decision.invoice_id,
        resolved.flow_id,
    )
    if brand is not None:
        return brand
    return _Step("route", resolved.rule_id, summary, flow_id=resolved.flow_id)


def _resolve_flow(
    reading: Reading,
    profile: Profile,
    cfg: Config,
    rules: tuple[CompiledRule, ...],
    answers: Mapping[str, Any],
    invoice_id: int,
) -> _Resolved | _Step:
    """Run D92's first check: the stored answer or the router (BR-R03, BR-R07, refusals).

    A stored ``answers.flow`` that is no longer a candidate is dropped, so the router runs
    again and the question is re-asked.
    """
    answer = answers.get("flow")
    if isinstance(answer, str):
        for flow_id, flow in candidate_flows(profile.flows, cfg.flows, reading.value("tipo") or ""):
            if flow_id == answer:
                return _Resolved(flow_id=flow_id, flow=flow, rule_id=RULE_ASK)
    result = route(reading, profile.flows, rules, cfg.flows, cfg.routing.unmatched)
    if result.rule in REFUSALS:
        refused_flow = None if result.flow is None else cfg.flows[result.flow]
        return _Step("cancel", result.rule, _refusal(result.rule, reading, refused_flow))
    if result.rule == RULE_ASK:
        candidates = candidate_flows(profile.flows, cfg.flows, reading.value("tipo") or "")
        if not candidates:
            return _Step("cancel", RULE_UNMATCHED, _refusal(RULE_UNMATCHED, reading, None))
        text, buttons = flow_question(invoice_id, reading, candidates)
        return _Step("ask", RULE_ASK, text, buttons, choices=tuple(flow_id for flow_id, _ in candidates))
    return _Resolved(
        flow_id=cast(str, result.flow), flow=cfg.flows[cast(str, result.flow)], rule_id=result.rule
    )


def _buyer_check(
    reading: Reading,
    resolved: _Resolved,
    profile: Profile,
    cfg: Config,
    answers: Mapping[str, Any],
    invoice_id: int,
) -> _Step | None:
    """Run D92's second check: BR-R04 for a doubtful buyer, BR-R03 when it was refused (S4.4).

    A flow the sender picked in the BR-R03 menu needs no buyer check: they chose it knowing
    the buyer, and asking again after [Cambiar] would loop.
    """
    if resolved.rule_id == RULE_ASK:
        return None
    buyer_ok = answers.get("buyer_ok")
    if buyer_ok is False:
        candidates = candidate_flows(profile.flows, cfg.flows, reading.value("tipo") or "")
        text, buttons = flow_question(invoice_id, reading, candidates)
        return _Step("ask", RULE_ASK, text, buttons, choices=tuple(flow_id for flow_id, _ in candidates))
    if buyer_ok is None and _doubtful_buyer(reading, cfg.extraction.review_threshold):
        text, buttons = buyer_question(invoice_id, reading, resolved.flow)
        return _Step("ask_flow", RULE_BUYER, text, buttons, resolved.flow_id)
    return None


def _cap_check(
    reading: Reading,
    resolved: _Resolved,
    answers: Mapping[str, Any],
    invoice_id: int,
    summary: str,
) -> _Step | None:
    """Run D92's third check: BR-D06 unless the sender already continued (R-7.13)."""
    if answers.get("cap_ok"):
        return None
    question = cap_question(reading, resolved.flow, invoice_id)
    if question is None:
        return None
    text, buttons = question
    return _Step("ask_flow", RULE_CAP, f"{summary}\n{text}", buttons, resolved.flow_id)


def _brand_check(
    reading: Reading,
    answers: Mapping[str, Any],
    directory: StoreDirectory,
    invoice_id: int,
    flow_id: str,
) -> _Step | None:
    """Run D92's fourth check: BR-R09 for a multi-brand store with no serie match (BR-R09).

    The issuer's store is the one delivery looks up. A stored ``answers.comercio`` that is
    not one of its brands is dropped, so the question is asked again.
    """
    store = directory.stores.get(reading.value("ruc_emisor") or "")
    comercio = answers.get("comercio")
    if isinstance(comercio, str) and store is not None and any(b.comercio == comercio for b in store.brands):
        return None
    serie = reading.value("serie") or ""
    razon_social = reading.value("razon_social") or ""
    if store is not None and len(store.brands) > 1 and comercio_for(store, serie, razon_social) is None:
        text, buttons = brand_question(invoice_id, store)
        return _Step(
            "ask_flow",
            RULE_BRAND,
            text,
            buttons,
            flow_id,
            choices=tuple(brand.comercio for brand in store.brands),
        )
    return None


async def _claim(
    conn: AsyncConnection[Any], invoice_id: int, profile_id: str, reading: Reading
) -> Duplicate | None:
    """Claim the reading's natural key before the checks decide (BR-E04, D93, R-10.1).

    The key is scoped to the invoice's profile (D93), so the same invoice may be read by
    two profiles. An incomplete key is not claimed: without all four parts it cannot
    identify an invoice, so the checks run as before.
    """
    key = _natural_key(profile_id, reading)
    if key is None:
        return None
    return await claim_natural_key(conn, invoice_id, key)


def _natural_key(profile_id: str, reading: Reading) -> str | None:
    """Return ``profile_id|ruc_emisor|tipo|serie|numero``, or None if a part is missing (D93, R-10.1)."""
    parts = tuple(reading.value(name) for name in NATURAL_KEY_FIELDS)
    if any(not part for part in parts):
        return None
    return "|".join((profile_id, *cast(tuple[str, ...], parts)))


async def _cancel_duplicate(
    conn: AsyncConnection[Any],
    invoice: InvoiceHead,
    invoice_id: int,
    duplicate: Duplicate,
    timezone: str,
) -> None:
    """Cancel invoice_id with BR-E04, pointing at the invoice that holds its key (BR-E04)."""
    text = MESSAGES[RULE_DUPLICATE].format(
        created_at=duplicate.created_at.astimezone(ZoneInfo(timezone)),
        id=duplicate.invoice_id,
    )
    await set_state(conn, invoice_id, CANCELLED_STATE, rule_id=RULE_DUPLICATE, expected=ROUTING_STATE)
    await _reply(conn, invoice, invoice_id, RULE_DUPLICATE, text)


async def _write(
    conn: AsyncConnection[Any],
    invoice: InvoiceHead,
    invoice_id: int,
    step: _Step,
) -> None:
    """Apply the decided step in the caller's transaction (D92)."""
    if step.kind in ("ask_flow", "route"):
        await set_flow(conn, invoice_id, cast(str, step.flow_id))
    if step.kind == "cancel":
        await set_state(conn, invoice_id, CANCELLED_STATE, rule_id=step.rule_id, expected=ROUTING_STATE)
        await _reply(conn, invoice, invoice_id, step.rule_id, step.text)
    elif step.kind == "route":
        await _routed(conn, invoice, invoice_id, step.rule_id, step.text)
    else:
        if step.choices:
            await merge_flow_data(conn, invoice_id, {"choices": list(step.choices)})
        await set_state(conn, invoice_id, CONFIRMING_STATE, rule_id=step.rule_id, expected=ROUTING_STATE)
        await _reply(conn, invoice, invoice_id, step.rule_id, step.text, step.buttons)


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
) -> None:
    """Queue the fetch job and move the invoice to ``FETCHING_DOCS`` with its summary (R-3.4)."""
    await set_state(conn, invoice_id, ROUTED_STATE, rule_id=rule_id, expected=ROUTING_STATE)
    await enqueue(conn, invoice_id, FETCH_STEP)
    await _reply(conn, invoice, invoice_id, rule_id, text, summary_buttons(invoice_id))


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


def _flow_line(flow: Flow, rule_id: str) -> str:
    """Return the summary's flow line for a routed invoice (spec 06 §1).

    A rule-matched flow shows that rule's message. A flow the sender picked in the BR-R03
    menu has no matched rule, so it shows the same ``➡️ {flow.label}`` line BR-R01 and
    BR-R02 carry.
    """
    if rule_id == RULE_ASK:
        return PICKED_FLOW_LINE.format(flow=flow)
    return MESSAGES[rule_id].format(flow=flow)


def _doubtful_buyer(reading: Reading, threshold: float) -> bool:
    """Return whether the buyer was read by OCR below the review threshold (BR-R04)."""
    buyer = reading.fields.get("num_doc_adq")
    return buyer is not None and buyer.source == "ocr" and buyer.confidence < threshold


def _summary(reading: Reading, flow_line: str, today: date, extraction: Extraction) -> str:
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
            flow_line=flow_line,
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
