"""Job handler that routes a read invoice through its questions and hands it to validation.

:func:`make_route_handler` builds the handler of the ``route`` job step. In ``ROUTING`` it
first claims the invoice's natural key (BR-E04, D93), then runs D92's ordered, resumable
checks over the stored answers: flow (rule match, else BR-R03),
buyer confidence (BR-R04), reimbursement cap (BR-D06), brand (BR-R09) and the
confirmation before validation (D126, BR-E03, BR-S10). The first
unanswered check moves the invoice to ``CONFIRMING`` with its rule and its question; when
every check passes the invoice stores its flow, queues its ``validate`` job and waits in
``VALIDATING`` (D124), where that job sends the summary and asks SUNAT. A refused invoice
is cancelled with the rule's message. When no routing rule matches the buyer, BR-R03's
question is asked and BR-R06's gap is raised. The state change, the next job and the
outbox row commit together.
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

from invoicebot.channels.base import MENU_MAX, Button
from invoicebot.channels.buttons import button_id
from invoicebot.config.models import Config, Flow, Profile, StoreDirectory
from invoicebot.extract.fields import Reading
from invoicebot.extract.validate import RULE_BAD_DATE, check_date
from invoicebot.gaps.kinds import fingerprint
from invoicebot.github.issue_text import IssueFields
from invoicebot.pipeline.extract import ROUTE_STEP
from invoicebot.pipeline.gaps import GapContext, GapSighting, raise_gap
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.questions import (
    RULE_BRAND,
    RULE_BUYER,
    RULE_CAP,
    RULE_CONFIRM,
    STATUS_ACTION,
    brand_question,
    buyer_question,
    candidate_flows,
    cap_question,
    confirm_buttons,
    flow_question,
)
from invoicebot.pipeline.summary import flow_line, issuer_desc, summary_text
from invoicebot.pipeline.validate import VALIDATE_STEP
from invoicebot.queue.jobs import Job, enqueue, is_superseded
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
    BUTTON_STATUS,
    KIND_SUBJECT,
    MESSAGES,
    SUMMARY_DATE_FORMAT,
    buyer_desc,
    tipo_phrase,
    tipos_desc,
)
from invoicebot.sinks.folders import comercio_for
from invoicebot.states import CANCELLED, CONFIRMING, ROUTING, VALIDATING
from invoicebot.store.clock import owner_today
from invoicebot.store.db import Pool
from invoicebot.store.flow_b import load_flow_data, merge_flow_data
from invoicebot.store.invoices import (
    Duplicate,
    InvoiceHead,
    claim_natural_key,
    load_invoice_head,
    load_reading,
    lock_invoice,
    set_flow,
    set_state,
)

logger = logging.getLogger(__name__)

#: The rule that cancels an invoice whose natural key another invoice holds (BR-E04, D93).
RULE_DUPLICATE = "BR-E04"
#: The gap a buyer RUC with no matching routing rule raises on its third sighting (BR-R06).
UNROUTED_BUYER = "UNROUTED_BUYER"
#: The reading fields that make up an invoice's natural key, in key order (D93).
NATURAL_KEY_FIELDS = ("ruc_emisor", "tipo", "serie", "numero")
#: Where a routed invoice waits; its ``validate`` job is queued with this state change (D124).
ROUTED_STATE = VALIDATING


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
    #: The menu number the question's choice buttons carry, stored with ``choices`` (R-3.5).
    menu: int | None = None
    #: The buyer document number BR-R03 could not route, raised as a gap (BR-R06).
    unrouted_buyer: str | None = None


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
    menu: int


def make_route_handler(
    pool: Pool, cfg: Config, directory: StoreDirectory, rules: tuple[CompiledRule, ...]
) -> Handler:
    """Build the handler that routes an invoice in ``ROUTING`` (S4.2, S4.3, S4.4, D92).

    The ordered checks are :func:`_resolve_flow`, :func:`_buyer_check`, :func:`_cap_check`,
    :func:`_brand_check` and :func:`_confirm_check`; a button answer stores its value and
    enqueues the route job again (step 12), so a re-run skips the answered checks.
    BR-R05, BR-R07 and BR-R08 cancel the invoice with their message. A missing reading or
    an unknown profile raises, so the queue retries.
    """

    async def handle(job: Job) -> None:
        started = time.perf_counter()
        async with pool.connection() as conn:
            invoice = await load_invoice_head(conn, job.invoice_id)
            if invoice is None or invoice.state != ROUTING:
                return
            reading = await load_reading(conn, job.invoice_id)
            flow_data = await load_flow_data(conn, job.invoice_id)
            today = await owner_today(conn, cfg.owner.timezone)
        if reading is None:
            raise RuntimeError(f"invoice {job.invoice_id} has no reading")
        profile = cfg.profiles.get(invoice.profile_id)
        if profile is None:
            raise RuntimeError(f"invoice {job.invoice_id} has an unknown profile")
        raw_answers = flow_data.get("answers")
        answers: Mapping[str, Any] = raw_answers if isinstance(raw_answers, dict) else {}
        menu = _next_menu(flow_data)
        async with pool.connection() as conn, conn.transaction():
            # An edit that committed a newer route job leaves this run nothing to do (D95).
            await lock_invoice(conn, job.invoice_id)
            if await is_superseded(conn, job):
                return
            duplicate = await _claim(conn, job.invoice_id, invoice.profile_id, reading)
            if duplicate is not None:
                await _cancel_duplicate(conn, invoice, job.invoice_id, duplicate, reading, cfg.owner.timezone)
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
                        menu=menu,
                    )
                )
                await _write(conn, invoice, job.invoice_id, step)
                await _raise_unrouted(conn, cfg, job.invoice_id, reading, step)
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
    resolved = _resolve_flow(decision)
    if isinstance(resolved, _Step):
        return resolved
    buyer = _buyer_check(decision, resolved)
    if buyer is not None:
        return buyer
    summary = summary_text(
        decision.reading,
        flow_line(resolved.flow, resolved.rule_id),
        decision.today,
        decision.cfg.extraction,
    )
    cap = _cap_check(decision.reading, resolved, decision.answers, decision.invoice_id, summary)
    if cap is not None:
        return cap
    brand = _brand_check(decision, resolved.flow_id)
    if brand is not None:
        return brand
    confirm = _confirm_check(decision, summary)
    if confirm is not None:
        return confirm
    return _Step("route", resolved.rule_id, summary, flow_id=resolved.flow_id)


def _confirm_check(decision: _Decision, summary: str) -> _Step | None:
    """Ask the sender to confirm the summary before validation (D126, BR-E03, BR-S10).

    The question is asked when routing is set to ``always``, when the issue date fails
    BR-E03, or when any field came from the LLM (R-2.11); a stored ``confirmed`` answer
    skips it. BR-E03's text comes first, then the summary and BR-S10's question.
    """
    reading = decision.reading
    if decision.answers.get("confirmed") is True:
        return None
    fecha = reading.fecha()
    bad_date = (
        fecha is not None
        and check_date(fecha, decision.today, decision.cfg.extraction.max_invoice_age_days) is not None
    )
    from_llm = any(field.source == "llm" for field in reading.fields.values())
    if not (decision.cfg.routing.confirm_mode == "always" or bad_date or from_llm):
        return None
    rule_id = RULE_BAD_DATE if bad_date else RULE_CONFIRM
    return _Step("ask", rule_id, _confirm_text(rule_id, fecha, summary), confirm_buttons(decision.invoice_id))


def _confirm_text(rule_id: str, fecha: date | None, summary: str) -> str:
    """Return the confirm question's text: BR-E03's line when it asked, the summary, BR-S10 (D126)."""
    lines = []
    if rule_id == RULE_BAD_DATE and fecha is not None:
        lines.append(MESSAGES[rule_id].format(fecha=fecha.strftime(SUMMARY_DATE_FORMAT)))
    lines.extend((summary, MESSAGES[RULE_CONFIRM]))
    return "\n".join(lines)


def _resolve_flow(decision: _Decision) -> _Resolved | _Step:
    """Run D92's first check: the stored answer or the router (BR-R03, BR-R07, refusals).

    A stored ``answers.flow`` that is no longer a candidate is dropped, so the router runs
    again and the question is re-asked.
    """
    reading = decision.reading
    answer = decision.answers.get("flow")
    if isinstance(answer, str):
        for flow_id, flow in candidate_flows(
            decision.profile.flows, decision.cfg.flows, reading.value("tipo") or ""
        ):
            if flow_id == answer:
                return _Resolved(flow_id=flow_id, flow=flow, rule_id=RULE_ASK)
    result = route(
        reading,
        decision.profile.flows,
        decision.rules,
        decision.cfg.flows,
        decision.cfg.routing.unmatched,
    )
    if result.rule in REFUSALS:
        refused_flow = None if result.flow is None else decision.cfg.flows[result.flow]
        return _Step("cancel", result.rule, _refusal(result.rule, reading, refused_flow))
    if result.rule == RULE_ASK:
        candidates = candidate_flows(decision.profile.flows, decision.cfg.flows, reading.value("tipo") or "")
        if not candidates:
            return _Step("cancel", RULE_UNMATCHED, _refusal(RULE_UNMATCHED, reading, None))
        text, buttons = flow_question(decision.invoice_id, reading, candidates, decision.menu)
        return _Step(
            "ask",
            RULE_ASK,
            text,
            buttons,
            choices=tuple(flow_id for flow_id, _ in candidates),
            menu=decision.menu,
            unrouted_buyer=_sighted_buyer(reading),
        )
    return _Resolved(
        flow_id=cast(str, result.flow),
        flow=decision.cfg.flows[cast(str, result.flow)],
        rule_id=result.rule,
    )


def _buyer_check(decision: _Decision, resolved: _Resolved) -> _Step | None:
    """Run D92's second check: BR-R04 for a doubtful buyer, BR-R03 when it was refused (S4.4).

    A flow the sender picked in the BR-R03 menu needs no buyer check: they chose it knowing
    the buyer, and asking again after [Cambiar] would loop.
    """
    if resolved.rule_id == RULE_ASK:
        return None
    buyer_ok = decision.answers.get("buyer_ok")
    if buyer_ok is False:
        candidates = candidate_flows(
            decision.profile.flows, decision.cfg.flows, decision.reading.value("tipo") or ""
        )
        text, buttons = flow_question(decision.invoice_id, decision.reading, candidates, decision.menu)
        return _Step(
            "ask",
            RULE_ASK,
            text,
            buttons,
            choices=tuple(flow_id for flow_id, _ in candidates),
            menu=decision.menu,
        )
    if buyer_ok is None and _doubtful_buyer(decision.reading, decision.cfg.extraction.review_threshold):
        text, buttons = buyer_question(decision.invoice_id, decision.reading, resolved.flow)
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


def _brand_check(decision: _Decision, flow_id: str) -> _Step | None:
    """Run D92's fourth check: BR-R09 for a multi-brand store with no serie match (BR-R09).

    The issuer's store is the one delivery looks up. A stored ``answers.comercio`` that is
    not one of its brands is dropped, so the question is asked again.
    """
    reading = decision.reading
    store = decision.directory.stores.get(reading.value("ruc_emisor") or "")
    comercio = decision.answers.get("comercio")
    if isinstance(comercio, str) and store is not None and any(b.comercio == comercio for b in store.brands):
        return None
    serie = reading.value("serie") or ""
    razon_social = reading.value("razon_social") or ""
    if store is not None and len(store.brands) > 1 and comercio_for(store, serie, razon_social) is None:
        text, buttons = brand_question(decision.invoice_id, store, decision.menu)
        return _Step(
            "ask_flow",
            RULE_BRAND,
            text,
            buttons,
            flow_id,
            choices=tuple(brand.comercio for brand in store.brands),
            menu=decision.menu,
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
    key = natural_key(profile_id, reading)
    if key is None:
        return None
    return await claim_natural_key(conn, invoice_id, key)


def natural_key(profile_id: str, reading: Reading) -> str | None:
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
    reading: Reading,
    timezone: str,
) -> None:
    """Cancel invoice_id with BR-E04, pointing at the invoice that holds its key (BR-E04, D188)."""
    text = MESSAGES[RULE_DUPLICATE].format(
        created_at=duplicate.created_at.astimezone(ZoneInfo(timezone)),
        id=duplicate.invoice_id,
        issuer=issuer_desc(reading),
        total=reading.value("total") or "",
    )
    status = Button(
        id=button_id(duplicate.invoice_id, STATUS_ACTION),
        title=BUTTON_STATUS.format(id=duplicate.invoice_id),
    )
    await set_state(conn, invoice_id, CANCELLED, rule_id=RULE_DUPLICATE, expected=ROUTING)
    await _reply(conn, invoice, invoice_id, RULE_DUPLICATE, text, [status])


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
        await set_state(conn, invoice_id, CANCELLED, rule_id=step.rule_id, expected=ROUTING)
        await _reply(conn, invoice, invoice_id, step.rule_id, step.text)
    elif step.kind == "route":
        await _routed(conn, invoice_id, step.rule_id)
    else:
        if step.choices:
            await merge_flow_data(conn, invoice_id, {"choices": list(step.choices), "menu": step.menu})
        await set_state(conn, invoice_id, CONFIRMING, rule_id=step.rule_id, expected=ROUTING)
        await _reply(conn, invoice, invoice_id, step.rule_id, step.text, step.buttons)


async def _raise_unrouted(
    conn: AsyncConnection[Any],
    cfg: Config,
    invoice_id: int,
    reading: Reading,
    step: _Step,
) -> None:
    """Raise BR-R06's gap when no routing rule matched and the buyer is known (BR-R06)."""
    if step.unrouted_buyer is None:
        return
    await raise_gap(
        conn,
        GapContext(profiles=cfg.profiles, gaps_cfg=cfg.gaps),
        _unrouted_sighting(invoice_id, reading, step.unrouted_buyer),
    )


def _unrouted_sighting(invoice_id: int, reading: Reading, num_doc_adq: str) -> GapSighting:
    """Build one UNROUTED_BUYER sighting from the buyer document (BR-R06, R-11.12, D120)."""
    return GapSighting(
        kind=UNROUTED_BUYER,
        parts=(num_doc_adq,),
        subject=KIND_SUBJECT[UNROUTED_BUYER].format(num_doc_adq=num_doc_adq),
        invoice_id=invoice_id,
        fields=IssueFields(
            kind=UNROUTED_BUYER,
            fingerprint=fingerprint(UNROUTED_BUYER, num_doc_adq),
            ruc_emisor=_ruc_int(reading.value("ruc_emisor")),
            razon_social=reading.value("razon_social"),
            tipo=reading.value("tipo"),
            reasons=(),
            portal_key=None,
            hint_url=None,
        ),
    )


def _ruc_int(value: str | None) -> int | None:
    """Return ``value`` as an int when it is 11 digits, else None (R-11.12)."""
    if value is None or len(value) != 11 or not value.isdecimal():
        return None
    return int(value)


def _next_menu(flow_data: Mapping[str, Any]) -> int:
    """Return the menu number of the question about to be asked (R-3.5).

    It is the stored menu plus one, so a press on an older menu is recognisably stale; an
    invoice that has not asked yet, or whose stored menu is not an int, starts at 0. It
    wraps after ``MENU_MAX`` so the button id stays valid.
    """
    menu = flow_data.get("menu")
    if isinstance(menu, bool) or not isinstance(menu, int):
        return 0
    return (menu + 1) % (MENU_MAX + 1)


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


async def _routed(conn: AsyncConnection[Any], invoice_id: int, rule_id: str) -> None:
    """Queue the validate job and move the invoice to ``VALIDATING`` (D124).

    The validate step sends the routed summary and asks SUNAT, so routing no longer
    writes an outbox row of its own.
    """
    await set_state(conn, invoice_id, ROUTED_STATE, rule_id=rule_id, expected=ROUTING)
    await enqueue(conn, invoice_id, VALIDATE_STEP)


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
    buyer = buyer_desc(
        reading.value("tipo") or "", reading.value("tipo_doc_adq") or "", reading.value("num_doc_adq") or ""
    )
    return MESSAGES[RULE_UNMATCHED].format(buyer_desc=buyer)


#: The readers whose text D212 repairs and D213 matches a buyer from: an OCR read or a PDF text layer.
_INFERRING_SOURCES = frozenset({"ocr", "pdf"})


def _sighted_buyer(reading: Reading) -> str | None:
    """Return the buyer RUC an UNROUTED_BUYER sighting counts (BR-R06), or None for an inferred one.

    A repaired or inferred buyer may exist only through OCR, so it never raises the gap (D213).
    """
    buyer = reading.fields.get("num_doc_adq")
    return None if buyer is None or buyer.flag == "inferred" else buyer.value


def _doubtful_buyer(reading: Reading, threshold: float) -> bool:
    """Return whether the buyer was read by OCR below the review threshold or inferred (BR-R04).

    An inferred buyer (D212, D213) is matched from a misread RUC and a name, so it is asked
    about whatever its confidence.
    """
    buyer = reading.fields.get("num_doc_adq")
    if buyer is None:
        return False
    if buyer.flag == "inferred" and buyer.source in _INFERRING_SOURCES:
        return True
    return buyer.source == "ocr" and buyer.confidence < threshold
