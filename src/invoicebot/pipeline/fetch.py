"""Job handler that fetches an invoice's documents through its fetcher chain (S7.1, R-5.1).

:func:`make_fetch_handler` builds the handler of the ``fetch`` job step. The chain in
:mod:`invoicebot.fetchers.chain` picks the fetcher from the recorded attempts; the
handler runs it and, in one transaction, records the attempt and either stores the
files, schedules the next try, or moves the invoice on. A chain with nothing left
moves the invoice to ``MANUAL_REQUIRED`` with BR-F07's message.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from psycopg import AsyncConnection

from invoicebot.channels.base import OutboundMessage
from invoicebot.config.checks import MANUAL_PORTAL
from invoicebot.config.models import DEFAULT_RETRY_SCHEDULE, Config, Flow, Portal, StoreDirectory
from invoicebot.extract.fields import Reading
from invoicebot.fetchers.base import Fetcher, FetchInvoice, FetchReason, FetchResult
from invoicebot.fetchers.chain import Attempt, Done, Run, Wait, decide, parse_delay
from invoicebot.queue import outbox
from invoicebot.queue.jobs import Job, enqueue
from invoicebot.queue.worker import Handler
from invoicebot.rules.messages_es import (
    FETCH_NOT_FOUND_LINE,
    FETCH_WAITING,
    MESSAGES,
    docs_desc,
    fetcher_desc,
)
from invoicebot.store.db import Pool
from invoicebot.store.fetch_attempts import list_attempts, record_attempt
from invoicebot.store.invoices import (
    FileRecord,
    InvoiceHead,
    add_file,
    load_flow,
    load_invoice_head,
    load_reading,
    set_state,
)

logger = logging.getLogger(__name__)

#: The job step this handler runs.
FETCH_STEP = "fetch"
#: The only state this handler acts on; any other leaves the invoice as it is (R-9.4).
FETCHING_STATE = "FETCHING_DOCS"
#: Where an invoice goes once its required documents are stored (R-5.1).
DELIVERING_STATE = "DELIVERING"
#: Where an invoice goes when no fetcher can get its documents (BR-F07).
MANUAL_STATE = "MANUAL_REQUIRED"
#: BR-F07: the rule that reports an invoice nobody could fetch.
RULE_EXHAUSTED = "BR-F07"
#: The fetcher whose retry schedule comes from the store's portal (D65).
PORTAL_FETCHER = "portal_registry"
#: Directory, under the data dir, where a fetcher writes before the files are stored.
TMP_DIR = "tmp"

#: The BR-F message of a fetcher's chain-final reason (spec 06 §1, BR-F07).
_REASON_RULES: dict[FetchReason, str] = {
    FetchReason.XML_MISMATCH: "BR-F06",
    FetchReason.GUARD_BLOCKED: "BR-F08",
    FetchReason.XML_INVALID: "BR-F09",
}


@dataclass(frozen=True)
class _Inputs:
    """One invoice's fetch inputs, shared by every decision branch."""

    pool: Pool
    job: Job
    head: InvoiceHead
    flow: Flow
    invoice: FetchInvoice
    attempts: Sequence[Attempt]


@dataclass(frozen=True)
class _Outcome:
    """The values the ``step done`` log line carries for one decision (R-9.16)."""

    fetcher: str | None
    portal_key: str | None
    reason: str | None


def make_fetch_handler(
    pool: Pool,
    cfg: Config,
    directory: StoreDirectory,
    registry: Mapping[str, Fetcher],
    data_dir: Path,
) -> Handler:
    """Build the handler that fetches documents for an invoice in ``FETCHING_DOCS`` (S7.1).

    The flow's fetcher order, the registry and the recorded attempts feed the chain
    decision (D65). A missing reading, an unrouted invoice or an unknown flow raises,
    so the queue retries the job.
    """

    async def handle(job: Job) -> None:
        started = time.perf_counter()
        async with pool.connection() as conn:
            head = await load_invoice_head(conn, job.invoice_id)
            if head is None or head.state != FETCHING_STATE:
                return
            reading = await load_reading(conn, job.invoice_id)
            flow_name = await load_flow(conn, job.invoice_id)
            rows = await list_attempts(conn, job.invoice_id)
        if reading is None:
            raise RuntimeError(f"invoice {job.invoice_id} has no reading")
        if flow_name is None:
            raise RuntimeError(f"invoice {job.invoice_id} was not routed")
        flow = cfg.flows.get(flow_name)
        if flow is None:
            raise RuntimeError(f"invoice {job.invoice_id} has unknown flow {flow_name!r}")

        inputs = _Inputs(
            pool=pool,
            job=job,
            head=head,
            flow=flow,
            invoice=_fetch_invoice(reading, job.invoice_id),
            attempts=_attempts(rows),
        )
        schedule_for = _schedule_for(inputs.invoice.ruc_emisor, directory)
        decision = decide(flow.fetchers, registry.keys(), inputs.attempts, schedule_for)
        # A Wait means the latest try was not found yet and retries remain; this job is the
        # retry, since its run_after already held the delay (D65), so the fetcher runs now.
        if isinstance(decision, Run | Wait):
            outcome = await _run(inputs, decision.fetcher, registry, schedule_for, data_dir)
        elif isinstance(decision, Done):
            outcome = await _done(inputs, decision.fetcher)
        else:
            outcome = await _exhausted(inputs, reading)
        logger.info(
            "step done",
            extra={
                "step": FETCH_STEP,
                "invoice_id": job.invoice_id,
                "fetcher": outcome.fetcher,
                "portal_key": outcome.portal_key,
                "reason": outcome.reason,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            },
        )

    return handle


async def _run(
    inputs: _Inputs,
    fetcher: str,
    registry: Mapping[str, Fetcher],
    schedule_for: Callable[[str], Sequence[timedelta]],
    data_dir: Path,
) -> _Outcome:
    """Run one fetcher and record its result in one transaction (S7.1, R-5.7)."""
    out_dir = data_dir / TMP_DIR / str(inputs.job.invoice_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    result = await registry[fetcher].fetch(inputs.invoice, out_dir)
    duration_ms = int((time.perf_counter() - started) * 1000)
    result = _with_required_docs(result, inputs.flow.required_docs)
    delay = _next_delay(schedule_for(fetcher), _not_found_count(inputs.attempts, fetcher))
    first_not_found = _not_found_count(inputs.attempts, None) == 0
    async with inputs.pool.connection() as conn, conn.transaction():
        await record_attempt(
            conn, inputs.job.invoice_id, fetcher, result.portal_key, result.reason.value, duration_ms
        )
        await _record_result(conn, inputs, result, delay, first_not_found)
    return _Outcome(fetcher=fetcher, portal_key=result.portal_key, reason=result.reason.value)


async def _record_result(
    conn: AsyncConnection[Any],
    inputs: _Inputs,
    result: FetchResult,
    delay: timedelta | None,
    first_not_found: bool,
) -> None:
    """Store an ``ok`` result's files, or schedule the next try, in the caller's transaction."""
    if result.ok:
        for fetched in result.files:
            await add_file(
                conn,
                inputs.job.invoice_id,
                FileRecord(
                    kind=fetched.kind,
                    local_path=str(fetched.path),
                    remote_ref=None,
                    sha256=fetched.sha256,
                    size=fetched.size,
                ),
            )
        await set_state(conn, inputs.job.invoice_id, DELIVERING_STATE)
    elif result.reason is FetchReason.NOT_FOUND_YET:
        run_after = None if delay is None else await _run_after(conn, delay)
        await enqueue(conn, inputs.job.invoice_id, FETCH_STEP, run_after)
        if first_not_found:
            await _notify(conn, inputs.head, None, FETCH_WAITING.format(id=inputs.job.invoice_id))
    else:
        await enqueue(conn, inputs.job.invoice_id, FETCH_STEP)


async def _done(inputs: _Inputs, fetcher: str) -> _Outcome:
    """Move an invoice whose documents are already stored on to delivery (R-5.1)."""
    async with inputs.pool.connection() as conn, conn.transaction():
        await set_state(conn, inputs.job.invoice_id, DELIVERING_STATE)
    return _Outcome(fetcher=fetcher, portal_key=None, reason=FetchReason.OK.value)


async def _exhausted(inputs: _Inputs, reading: Reading) -> _Outcome:
    """Move the invoice to MANUAL_REQUIRED with BR-F07's message (BR-F07)."""
    text = _manual_message(reading, inputs.flow.required_docs, inputs.attempts)
    async with inputs.pool.connection() as conn, conn.transaction():
        await set_state(conn, inputs.job.invoice_id, MANUAL_STATE, rule_id=RULE_EXHAUSTED)
        await _notify(conn, inputs.head, RULE_EXHAUSTED, text)
    return _Outcome(fetcher=None, portal_key=None, reason=RULE_EXHAUSTED)


async def _notify(conn: AsyncConnection[Any], head: InvoiceHead, rule_id: str | None, text: str) -> None:
    """Queue ``text`` for the invoice's sender in the caller's transaction."""
    message = OutboundMessage(
        channel=head.origin_channel,
        recipient=head.profile_id,
        profile_id=head.profile_id,
        rule_id=rule_id,
        text=text,
    )
    await outbox.add(conn, head.origin_channel, head.profile_id, message.model_dump(mode="json"))


async def _run_after(conn: AsyncConnection[Any], delay: timedelta) -> datetime:
    """Return the database clock plus ``delay``; the caller owns the transaction (D65)."""
    cur = await conn.execute("SELECT now() + %s", (delay,))
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("SELECT now() + delay yielded no row")
    return cast(datetime, row[0])


def _fetch_invoice(reading: Reading, invoice_id: int) -> FetchInvoice:
    """Return the fields a fetcher needs, or raise when the reading lacks one (R-5.1)."""
    ruc_emisor = reading.value("ruc_emisor")
    tipo = reading.value("tipo")
    serie = reading.value("serie")
    numero = reading.value("numero")
    fecha = reading.fecha()
    total = reading.amount("total")
    if (
        ruc_emisor is None
        or tipo is None
        or serie is None
        or numero is None
        or fecha is None
        or total is None
    ):
        raise RuntimeError(f"invoice {invoice_id} lacks the fields to fetch documents")
    return FetchInvoice(
        ruc_emisor=ruc_emisor,
        tipo=tipo,
        serie=serie,
        numero=numero,
        fecha=fecha,
        total=total,
    )


def _attempts(rows: Sequence[tuple[str, str]]) -> list[Attempt]:
    """Return the stored ``(fetcher, reason)`` rows as chain attempts (R-5.7)."""
    return [Attempt(fetcher, FetchReason(reason)) for fetcher, reason in rows]


def _with_required_docs(result: FetchResult, required_docs: Sequence[str]) -> FetchResult:
    """Turn an ``ok`` fetch that lacks a required kind into ``missing_docs`` (R-5.1)."""
    if not result.ok:
        return result
    if not set(required_docs) - {fetched.kind for fetched in result.files}:
        return result
    for fetched in result.files:
        fetched.path.unlink(missing_ok=True)
    return FetchResult(FetchReason.MISSING_DOCS, (), result.portal_key)


def _next_delay(schedule: Sequence[timedelta], previous: int) -> timedelta | None:
    """Return the wait after ``previous`` earlier not-found-yet attempts of one fetcher (D65).

    ``None`` when the schedule is used up: the next job runs at once and the chain moves on.
    """
    return schedule[previous] if previous < len(schedule) else None


def _not_found_count(attempts: Sequence[Attempt], fetcher: str | None) -> int:
    """Count not-found-yet attempts, for one fetcher or for the whole invoice (D65)."""
    return sum(
        1
        for attempt in attempts
        if attempt.reason is FetchReason.NOT_FOUND_YET and (fetcher is None or attempt.fetcher == fetcher)
    )


def _manual_message(reading: Reading, required_docs: Sequence[str], attempts: Sequence[Attempt]) -> str:
    """Return BR-F07's message for an invoice no fetcher could complete (BR-F07)."""
    return MESSAGES[RULE_EXHAUSTED].format(
        serie=reading.value("serie") or "",
        numero=reading.value("numero") or "",
        fetcher_lines=_fetcher_lines(attempts),
        manual_docs_desc=docs_desc(required_docs),
        # shortcut: gaps land in S7.4 (widen-b); until then the line is always empty.
        gap_line="",
    )


def _fetcher_lines(attempts: Sequence[Attempt]) -> str:
    """Return one BR-F07 bullet per fetcher in attempt order, with its last reason (BR-F07).

    shortcut: the full list of S7.4 (widen-b) adds the other fetchers' failure texts;
    here a fetcher with no BR-F text of its own reads as "not found".
    """
    last: dict[str, FetchReason] = {attempt.fetcher: attempt.reason for attempt in attempts}
    return "\n".join(_reason_line(fetcher, reason) for fetcher, reason in last.items())


def _reason_line(fetcher: str, reason: FetchReason) -> str:
    """Return the BR-F07 bullet for one fetcher's final reason (BR-F07)."""
    rule_id = _REASON_RULES.get(reason)
    if rule_id is None:
        return FETCH_NOT_FOUND_LINE.format(fetcher=fetcher_desc(fetcher))
    return MESSAGES[rule_id].format(fetcher=fetcher_desc(fetcher))


def _schedule_for(ruc_emisor: str, directory: StoreDirectory) -> Callable[[str], Sequence[timedelta]]:
    """Return each fetcher's retry schedule, parsed from its config texts (D65)."""

    def schedule(fetcher: str) -> Sequence[timedelta]:
        entries = _schedule_entries(fetcher, ruc_emisor, directory)
        return tuple(parse_delay(entry) for entry in entries)

    return schedule


def _schedule_entries(fetcher: str, ruc_emisor: str, directory: StoreDirectory) -> Sequence[str]:
    """Return the delay texts of a fetcher: its portal's schedule or the D65 default (D65)."""
    if fetcher != PORTAL_FETCHER:
        return DEFAULT_RETRY_SCHEDULE
    portal = _portal_for(ruc_emisor, directory)
    return DEFAULT_RETRY_SCHEDULE if portal is None else portal.retry_schedule


def _portal_for(ruc_emisor: str, directory: StoreDirectory) -> Portal | None:
    """Return the issuer's configured portal, or None when it has none (R-5.8)."""
    store = directory.stores.get(ruc_emisor)
    if store is None or store.portal in (None, MANUAL_PORTAL):
        return None
    return directory.portals.get(store.portal)
