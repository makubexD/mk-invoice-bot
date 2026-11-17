"""Job handler that fetches an invoice's documents through its fetcher chain (S7.1, R-5.1).

:func:`make_fetch_handler` builds the handler of the ``fetch`` job step. The flow's
registry and its fetcher order feed the chain in :mod:`invoicebot.fetchers.chain`; the
handler runs the chosen fetcher and, in one transaction, records the attempt and either
stores the files with the deliver job, schedules the next try, or moves the invoice on.
Each fetcher retries on the flow's schedule: the inbox's, its portal's, or the default.
A chain with nothing left moves the invoice to ``MANUAL_REQUIRED`` with BR-F07's message.
"""

from __future__ import annotations

import logging
import shutil
import time
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from psycopg import AsyncConnection

from invoicebot.channels.whatsapp.media import DIRECTORY_MODE
from invoicebot.config.models import (
    DEFAULT_RETRY_SCHEDULE,
    EMAIL_INBOX_FETCHER,
    Config,
    Flow,
    StoreDirectory,
)
from invoicebot.extract.fields import Reading
from invoicebot.fetchers.base import Fetcher, FetchInvoice, FetchReason, FetchResult
from invoicebot.fetchers.chain import WAIT_REASONS, Attempt, Exhausted, Run, Wait, decide, retry_delay
from invoicebot.gaps.kinds import fingerprint
from invoicebot.github.issue_text import IssueFields
from invoicebot.pipeline.deliver import AUTH_PROVIDER_SLUGS, RULE_AUTH_ADMIN
from invoicebot.pipeline.gaps import GapContext, GapSighting, raise_gap
from invoicebot.pipeline.handlers import TMP_DIRECTORY
from invoicebot.pipeline.manual_message import PORTAL_FETCHER as PORTAL_FETCHER
from invoicebot.pipeline.manual_message import (
    RULE_EXHAUSTED,
    TECHNICAL_REASONS,
    manual_message,
    portal_for,
)
from invoicebot.pipeline.outgoing import Addressee, queue_admin_alert, queue_message
from invoicebot.pipeline.steps import DELIVER_STEP
from invoicebot.pipeline.steps import FETCH_STEP as FETCH_STEP
from invoicebot.queue.jobs import Job, enqueue, is_superseded
from invoicebot.queue.worker import Handler
from invoicebot.rules.messages_es import (
    FETCH_WAITING,
    FETCH_WAITING_MAIL,
    GAP_LINE_PORTAL,
    KIND_SUBJECT,
    MESSAGES,
    UNKNOWN_ISSUER,
)
from invoicebot.states import DELIVERING, FETCHING_DOCS, MANUAL_REQUIRED
from invoicebot.store import taught_links
from invoicebot.store.db import Pool
from invoicebot.store.fetch_attempts import list_attempts, record_attempt, technical_streak
from invoicebot.store.gaps import GapRow, get_gap
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

#: The gap kind raised when no fetcher knows the issuer's portal (BR-F02, spec 06 §3).
PORTAL_MISSING = "PORTAL_MISSING"
#: The gap kind raised when the portal fails technically too many times in a row (BR-F03).
PORTAL_BROKEN = "PORTAL_BROKEN"
#: The gap states in which the issuer's portal is still known to be broken (BR-F03).
_BROKEN_GAP_STATES = frozenset({"open", "reported"})
#: The rule id of the question asking the sender for an unknown issuer's portal (BR-F02).
RULE_ASK_PORTAL = "BR-F02"
#: The first-wait text per fetcher; every other fetcher says the portal line (D100).
_WAITING_TEXTS: Mapping[str, str] = {EMAIL_INBOX_FETCHER: FETCH_WAITING_MAIL}
#: How one fetcher's retry delays are chosen from its flow and invoice (D65, D98).
type Schedule = Callable[[Flow, FetchInvoice], tuple[timedelta, ...]]


@dataclass(frozen=True)
class _Inputs:
    """One invoice's fetch inputs, shared by every decision branch."""

    pool: Pool
    cfg: Config
    job: Job
    head: InvoiceHead
    flow: Flow
    invoice: FetchInvoice
    attempts: Sequence[Attempt]
    directory: StoreDirectory


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
    registries: Mapping[str, Mapping[str, Fetcher]],
    data_dir: Path,
    *,
    schedules: Mapping[str, Schedule] | None = None,
) -> Handler:
    """Build the handler that fetches documents for an invoice in ``FETCHING_DOCS`` (S7.1).

    Each flow has its own registry and fetcher order; both, with the recorded attempts,
    feed the chain decision (D65, D98). ``schedules`` holds the retry schedule of each
    fetcher that has its own; a fetcher without an entry uses ``DEFAULT_RETRY_SCHEDULE``.
    A missing reading, an unrouted invoice or an unknown flow raises, so the queue retries
    the job.
    """
    retry_schedules = schedules or {}

    async def handle(job: Job) -> None:
        started = time.perf_counter()
        async with pool.connection() as conn:
            head = await load_invoice_head(conn, job.invoice_id)
            if head is None or head.state != FETCHING_DOCS:
                return
            # A redelivered job whose next job already ran is stale (R-5.1).
            if await is_superseded(conn, job):
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
        registry = registries.get(flow_name, {})

        inputs = _Inputs(
            pool=pool,
            cfg=cfg,
            job=job,
            head=head,
            flow=flow,
            invoice=_fetch_invoice(reading, job.invoice_id),
            attempts=_attempts(rows),
            directory=directory,
        )
        schedule_for = _schedule_for(inputs.flow, inputs.invoice, retry_schedules)
        decision = decide(flow.fetchers, registry.keys(), inputs.attempts, schedule_for)
        # A Wait means the latest try ended with a wait reason and retries remain; this job is the
        # retry, since its run_after already held the delay (D65), so the fetcher runs now.
        if isinstance(decision, Run | Wait):
            outcome = await _run(inputs, reading, decision.fetcher, registry, schedule_for, data_dir)
        elif isinstance(decision, Exhausted):
            outcome = await _exhausted(inputs, reading)
        else:
            # Done: the ok attempt, the DELIVERING commit and the deliver job happen together
            # (R-5.1), so an invoice that still fetches has no ok attempt; the queue retries
            # or parks this job.
            raise RuntimeError(f"invoice {job.invoice_id} has an ok attempt but is still fetching")
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
    reading: Reading,
    fetcher: str,
    registry: Mapping[str, Fetcher],
    schedule_for: Callable[[str], Sequence[timedelta]],
    data_dir: Path,
) -> _Outcome:
    """Run one fetcher and record its result in one transaction (S7.1, R-5.7)."""
    # One folder per run: a duplicate run's cleanup cannot remove the files another run stored.
    out_dir = data_dir / TMP_DIRECTORY / str(inputs.job.invoice_id) / f"{inputs.job.id}-{uuid.uuid4().hex}"
    out_dir.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
    started = time.perf_counter()
    try:
        result = await registry[fetcher].fetch(inputs.invoice, out_dir)
    except BaseException:
        # R-9.2: a fetcher that raises leaves no partial files behind.
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
    duration_ms = int((time.perf_counter() - started) * 1000)
    result = _with_required_docs(result, inputs.flow.required_docs)
    if not result.ok:
        # R-9.2: a failed fetch, a helper killed on timeout included, leaves nothing.
        shutil.rmtree(out_dir, ignore_errors=True)
    delay = retry_delay(fetcher, inputs.attempts, schedule_for(fetcher))
    outcome = _Outcome(fetcher=fetcher, portal_key=result.portal_key, reason=result.reason.value)
    committed = False
    try:
        async with inputs.pool.connection() as conn, conn.transaction():
            # Two runs of one job serialize on the job's row lock; the loser writes nothing (R-5.1).
            if not await _is_current(conn, inputs.job):
                # R-9.2: this run stored nothing, so its files must not stay.
                shutil.rmtree(out_dir, ignore_errors=True)
                return outcome
            await record_attempt(
                conn, inputs.job.invoice_id, fetcher, result.portal_key, result.reason.value, duration_ms
            )
            await _raise_broken_gap(conn, inputs, reading, result)
            await _record_result(conn, inputs, fetcher, result, delay)
            committed = True
    except BaseException:
        # R-9.2: a run that did not commit leaves no files behind; a committed run keeps them.
        if not committed:
            shutil.rmtree(out_dir, ignore_errors=True)
        raise
    return outcome


async def _is_current(conn: AsyncConnection[Any], job: Job) -> bool:
    """Return True when ``job`` may write; the loser of two runs writes nothing (R-5.1).

    ``is_superseded`` takes the job's row lock first, so two runs of one job
    serialize before the invoice state is read.
    """
    if await is_superseded(conn, job):
        return False
    head = await load_invoice_head(conn, job.invoice_id)
    return head is not None and head.state == FETCHING_DOCS


async def _raise_broken_gap(
    conn: AsyncConnection[Any], inputs: _Inputs, reading: Reading, result: FetchResult
) -> None:
    """Raise PORTAL_BROKEN after the portal's newest attempts all failed technically (BR-F03)."""
    if result.portal_key is None or result.reason not in TECHNICAL_REASONS:
        return
    if not await technical_streak(conn, result.portal_key, inputs.cfg.gaps.broken_after):
        return
    ctx = GapContext(profiles=inputs.cfg.profiles, gaps_cfg=inputs.cfg.gaps)
    await raise_gap(conn, ctx, _broken_sighting(inputs, reading, result.portal_key, result.reason))


def _broken_sighting(inputs: _Inputs, reading: Reading, portal_key: str, reason: FetchReason) -> GapSighting:
    """Build one PORTAL_BROKEN sighting from the result and the reading (BR-F03, R-11.12)."""
    ruc_emisor = reading.value("ruc_emisor")
    return GapSighting(
        kind=PORTAL_BROKEN,
        parts=(portal_key,),
        subject=KIND_SUBJECT[PORTAL_BROKEN].format(portal_key=portal_key),
        invoice_id=inputs.job.invoice_id,
        fields=IssueFields(
            kind=PORTAL_BROKEN,
            fingerprint=fingerprint(PORTAL_BROKEN, portal_key),
            ruc_emisor=None if ruc_emisor is None else int(ruc_emisor),
            razon_social=reading.value("razon_social"),
            tipo=reading.value("tipo"),
            reasons=(reason,),
            portal_key=portal_key,
            hint_url=None,
        ),
    )


async def _record_result(
    conn: AsyncConnection[Any],
    inputs: _Inputs,
    fetcher: str,
    result: FetchResult,
    delay: timedelta | None,
) -> None:
    """Store an ``ok`` result's files and enqueue deliver, or schedule the next try, in one transaction."""
    if result.ok:
        # shortcut: a fetched file's local_path stays under tmp/ until deliver moves it.
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
        await set_state(conn, inputs.job.invoice_id, DELIVERING, expected=FETCHING_DOCS)
        await enqueue(conn, inputs.job.invoice_id, DELIVER_STEP)
    elif result.reason in WAIT_REASONS:
        run_after = None if delay is None else await _run_after(conn, delay)
        await enqueue(conn, inputs.job.invoice_id, FETCH_STEP, run_after)
        if delay is not None and _is_first_attempt(inputs.attempts, fetcher, WAIT_REASONS):
            await _notify(
                conn, inputs.head, inputs.job.invoice_id, None, _waiting_text(fetcher, inputs.job.invoice_id)
            )
        if result.reason is FetchReason.AUTH_REQUIRED and _is_first_attempt(
            inputs.attempts, fetcher, {FetchReason.AUTH_REQUIRED}
        ):
            await _alert_sign_in(conn, inputs)
    else:
        await enqueue(conn, inputs.job.invoice_id, FETCH_STEP)


def _is_first_attempt(attempts: Sequence[Attempt], fetcher: str, reasons: Collection[FetchReason]) -> bool:
    """Return True when ``fetcher`` has no earlier attempt whose reason is in ``reasons`` (D100)."""
    return not any(attempt.fetcher == fetcher and attempt.reason in reasons for attempt in attempts)


def _waiting_text(fetcher: str, invoice_id: int) -> str:
    """Return ``fetcher``'s first-wait text; every fetcher but the inbox says the portal line (D100)."""
    return _WAITING_TEXTS.get(fetcher, FETCH_WAITING).format(id=invoice_id)


async def _alert_sign_in(conn: AsyncConnection[Any], inputs: _Inputs) -> None:
    """Alert the admins that the flow's mailbox needs a sign-in (BR-D01, D98, D100)."""
    inbox = inputs.flow.email_inbox
    if inbox is None:
        return
    slug = AUTH_PROVIDER_SLUGS.get(inbox.provider)
    if slug is None:
        return
    text = MESSAGES[RULE_AUTH_ADMIN].format(provider=slug, account=inbox.account)
    await queue_admin_alert(conn, inputs.cfg.profiles, RULE_AUTH_ADMIN, text)


async def _exhausted(inputs: _Inputs, reading: Reading) -> _Outcome:
    """Move the invoice to MANUAL_REQUIRED with BR-F07's message (BR-F07)."""
    async with inputs.pool.connection() as conn, conn.transaction():
        if not await _is_current(conn, inputs.job):
            return _Outcome(fetcher=None, portal_key=None, reason=RULE_EXHAUSTED)
        gap = await _raise_portal_gap(conn, inputs, reading)
        text = manual_message(
            reading,
            inputs.flow,
            inputs.attempts,
            inputs.directory,
            gap_line=_gap_line(gap),
            portal_broken=await _portal_is_broken(conn, inputs, reading),
        )
        await set_state(
            conn, inputs.job.invoice_id, MANUAL_REQUIRED, rule_id=RULE_EXHAUSTED, expected=FETCHING_DOCS
        )
        await _notify(conn, inputs.head, inputs.job.invoice_id, RULE_EXHAUSTED, text)
        await _ask_for_portal(conn, inputs, gap, reading)
    return _Outcome(fetcher=None, portal_key=None, reason=RULE_EXHAUSTED)


async def _portal_is_broken(conn: AsyncConnection[Any], inputs: _Inputs, reading: Reading) -> bool:
    """Return True when the issuer's portal has an open or reported PORTAL_BROKEN gap (BR-F03)."""
    ruc_emisor = reading.value("ruc_emisor")
    if ruc_emisor is None:
        return False
    portal = portal_for(ruc_emisor, inputs.directory)
    if portal is None:
        return False
    gap = await get_gap(conn, fingerprint(PORTAL_BROKEN, portal.key))
    return gap is not None and gap.state in _BROKEN_GAP_STATES


async def _raise_portal_gap(conn: AsyncConnection[Any], inputs: _Inputs, reading: Reading) -> GapRow | None:
    """Raise PORTAL_MISSING for an unknown portal and return its gap, or None (BR-F02, R-11.2)."""
    ruc_emisor = reading.value("ruc_emisor")
    if ruc_emisor is None or not _saw_no_portal(inputs.attempts) or reading.document_url is not None:
        return None
    ctx = GapContext(profiles=inputs.cfg.profiles, gaps_cfg=inputs.cfg.gaps)
    return await raise_gap(conn, ctx, _portal_sighting(inputs, reading, ruc_emisor))


def _gap_line(gap: GapRow | None) -> str:
    """Return BR-F07's portal line when ``gap`` was raised and is not ignored (R-11.3)."""
    return "" if gap is None or gap.state == "ignored" else GAP_LINE_PORTAL


async def _ask_for_portal(
    conn: AsyncConnection[Any], inputs: _Inputs, gap: GapRow | None, reading: Reading
) -> None:
    """Ask the sender for the issuer's portal once, when BR-F02 is due (BR-F02)."""
    if gap is None or not gap.raised_now or not inputs.cfg.gaps.teach_via_chat:
        return
    ruc_emisor = reading.value("ruc_emisor")
    if ruc_emisor is None or await taught_links.exists(conn, int(ruc_emisor)):
        return
    razon_social = reading.value("razon_social") or UNKNOWN_ISSUER
    text = MESSAGES[RULE_ASK_PORTAL].format(razon_social=razon_social, ruc_emisor=ruc_emisor)
    await _notify(conn, inputs.head, inputs.job.invoice_id, RULE_ASK_PORTAL, text)


def _saw_no_portal(attempts: Sequence[Attempt]) -> bool:
    """Return True when a fetcher reported that it found no portal (BR-F02)."""
    return any(attempt.reason is FetchReason.NO_PORTAL for attempt in attempts)


def _portal_sighting(inputs: _Inputs, reading: Reading, ruc_emisor: str) -> GapSighting:
    """Build one PORTAL_MISSING sighting from the reading and the attempts (BR-F02, R-11.12)."""
    razon_social = reading.value("razon_social")
    return GapSighting(
        kind=PORTAL_MISSING,
        parts=(ruc_emisor,),
        subject=KIND_SUBJECT[PORTAL_MISSING].format(ruc_emisor=ruc_emisor),
        invoice_id=inputs.job.invoice_id,
        fields=IssueFields(
            kind=PORTAL_MISSING,
            fingerprint=fingerprint(PORTAL_MISSING, ruc_emisor),
            ruc_emisor=int(ruc_emisor),
            razon_social=razon_social,
            tipo=reading.value("tipo"),
            reasons=tuple(attempt.reason for attempt in inputs.attempts),
            portal_key=None,
            hint_url=None,
        ),
    )


async def _notify(
    conn: AsyncConnection[Any], head: InvoiceHead, invoice_id: int, rule_id: str | None, text: str
) -> None:
    """Queue ``text`` for the invoice's sender in the caller's transaction."""
    await queue_message(
        conn,
        Addressee(head.origin_channel, head.profile_id),
        invoice_id=invoice_id,
        rule_id=rule_id,
        text=text,
    )


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
        document_url=reading.document_url,
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


def _schedule_for(
    flow: Flow, invoice: FetchInvoice, schedules: Mapping[str, Schedule]
) -> Callable[[str], Sequence[timedelta]]:
    """Return each fetcher's retry schedule as parsed delays (D65, D98)."""

    def schedule(fetcher: str) -> Sequence[timedelta]:
        return _schedule_entries(fetcher, flow, invoice, schedules)

    return schedule


def _schedule_entries(
    fetcher: str, flow: Flow, invoice: FetchInvoice, schedules: Mapping[str, Schedule]
) -> Sequence[timedelta]:
    """Return a fetcher's delays from ``schedules``, or the D65 default when it has none (D65, D98)."""
    schedule = schedules.get(fetcher)
    return DEFAULT_RETRY_SCHEDULE if schedule is None else schedule(flow, invoice)
