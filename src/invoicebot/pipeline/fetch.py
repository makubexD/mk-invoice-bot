"""Job handler that fetches an invoice's documents through its fetcher chain (S7.1, R-5.1).

:func:`make_fetch_handler` builds the handler of the ``fetch`` job step. The flow's
registry and its fetcher order feed the chain in :mod:`invoicebot.fetchers.chain`; the
handler runs the chosen fetcher and, in one transaction, records the attempt and either
stores the files with the deliver job, schedules the next try, or moves the invoice on.
Each fetcher retries on the flow's schedule: the inbox's, its portal's, or the default.
A chain with nothing left moves the invoice to ``MANUAL_REQUIRED`` with BR-F07's message.
A flow with no fetchers and no required documents goes straight to ``DELIVERING`` (D144).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection

from invoicebot.channels.base import Button, Link
from invoicebot.channels.buttons import command_button_id
from invoicebot.channels.media_limits import DIRECTORY_MODE
from invoicebot.config.digest import config_digest
from invoicebot.config.models import (
    DEFAULT_RETRY_SCHEDULE,
    EMAIL_INBOX_FETCHER,
    Config,
    Flow,
    StoreDirectory,
)
from invoicebot.extract.confirm import confirmed_by_xml, confirmed_names
from invoicebot.extract.fields import Reading
from invoicebot.extract.ubl import parse_invoice_xml
from invoicebot.fetchers.base import (
    TECHNICAL_REASONS,
    Fetcher,
    FetchInvoice,
    FetchReason,
    FetchResult,
)
from invoicebot.fetchers.chain import (
    WAIT_REASONS,
    Attempt,
    Exhausted,
    Run,
    Wait,
    decide,
    retry_delay,
    technical_retry_delay,
)
from invoicebot.fetchers.qr_url import QR_URL_FETCHER
from invoicebot.gaps.kinds import fingerprint
from invoicebot.github.issue_text import IssueFields
from invoicebot.pipeline.deliver import AUTH_PROVIDER_SLUGS, RULE_AUTH_ADMIN
from invoicebot.pipeline.gaps import GapContext, GapSighting, close_if_fixed, raise_gap
from invoicebot.pipeline.handlers import TMP_DIRECTORY
from invoicebot.pipeline.manual_message import PORTAL_FETCHER as PORTAL_FETCHER
from invoicebot.pipeline.manual_message import (
    RULE_EXHAUSTED,
    LookTimes,
    StoreFacts,
    docs_needed_text,
    manual_message,
    portal_for,
)
from invoicebot.pipeline.outgoing import Addressee, outbound, queue_admin_alert, queue_outbound
from invoicebot.pipeline.portal_link import open_portal_link
from invoicebot.pipeline.questions import (
    RULE_DOCS_NEEDED,
    ask_portal_buttons,
    docs_needed_buttons,
    retry_button,
)
from invoicebot.pipeline.steps import DELIVER_STEP
from invoicebot.pipeline.steps import FETCH_STEP as FETCH_STEP
from invoicebot.pipeline.summary import review_fields
from invoicebot.pipeline.summary_post import post_summary
from invoicebot.queue.jobs import Job, enqueue, is_superseded
from invoicebot.queue.worker import Handler
from invoicebot.rules.messages_es import (
    BUTTON_VOID,
    CONFIRMED_LINE,
    FETCH_WAITING,
    FOUND_LINE,
    GAP_LINE_PORTAL,
    KIND_SUBJECT,
    MESSAGES,
    UNKNOWN_ISSUER,
    docs_desc,
    fields_desc,
)
from invoicebot.states import DELIVERING, FETCHING_DOCS, MANUAL_REQUIRED
from invoicebot.store import taught_links
from invoicebot.store.clock import owner_today
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
    save_reading,
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
    #: Fetchers that get no D167 retry because their portal has an open broken gap (D170).
    no_retry: frozenset[str]
    #: Each fetcher's retry schedule, keyed by fetcher name (D65, D98).
    schedules: Mapping[str, Schedule]


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
            broken = reading is not None and await _portal_is_broken(conn, reading, directory)
        if reading is None:
            raise RuntimeError(f"invoice {job.invoice_id} has no reading")
        if flow_name is None:
            raise RuntimeError(f"invoice {job.invoice_id} was not routed")
        flow = cfg.flows.get(flow_name)
        if flow is None:
            raise RuntimeError(f"invoice {job.invoice_id} has unknown flow {flow_name!r}")
        registry = registries.get(flow_name, {})
        if _needs_no_fetch(flow):
            await _nothing_to_fetch(pool, cfg, job)
            logger.info(
                "step done",
                extra={
                    "step": FETCH_STEP,
                    "invoice_id": job.invoice_id,
                    "fetcher": None,
                    "portal_key": None,
                    "reason": None,
                    "outcome": "nothing_to_fetch",
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                },
            )
            return

        no_retry = frozenset({PORTAL_FETCHER}) if broken else frozenset()
        inputs = _Inputs(
            pool=pool,
            cfg=cfg,
            job=job,
            head=head,
            flow=flow,
            invoice=fetch_invoice(reading, job.invoice_id),
            attempts=_attempts(rows),
            directory=directory,
            no_retry=no_retry,
            schedules=retry_schedules,
        )
        schedule_for = _schedule_for(inputs.flow, inputs.invoice, retry_schedules)
        decision = decide(
            flow.fetchers, registry.keys(), inputs.attempts, schedule_for, no_retry=inputs.no_retry
        )
        # A Wait means the latest try ended with a wait reason or a first technical failure and a retry
        # remains; this job is the retry, since its run_after already held the delay (D65, D167).
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


def _needs_no_fetch(flow: Flow) -> bool:
    """Return True when the flow lists no fetchers and requires no documents (D144)."""
    return not flow.fetchers and not flow.required_docs


async def _nothing_to_fetch(pool: Pool, cfg: Config, job: Job) -> None:
    """Move an invoice whose flow has nothing to fetch to ``DELIVERING`` with its deliver job (D144).

    The summary goes out in the same transaction (D206).
    """
    async with pool.connection() as conn, conn.transaction():
        await set_state(conn, job.invoice_id, DELIVERING, expected=FETCHING_DOCS)
        await post_summary(conn, cfg, job.invoice_id)
        await enqueue(conn, job.invoice_id, DELIVER_STEP)


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
    schedule = schedule_for(fetcher)
    delay = (
        technical_retry_delay(fetcher, inputs.attempts, schedule, no_retry=inputs.no_retry)
        if result.reason in TECHNICAL_REASONS
        else retry_delay(fetcher, inputs.attempts, schedule)
    )
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
            if await _raise_broken_gap(conn, inputs, reading, result) and fetcher == PORTAL_FETCHER:
                # D170: the failure that raises the gap gets no retry; the next job's
                # handle sees the open gap, so decide agrees with this at-once enqueue.
                delay = None
            await _record_result(conn, inputs, reading, fetcher, result, delay)
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
) -> bool:
    """Raise PORTAL_BROKEN after the portal's newest attempts all failed technically (BR-F03).

    Return True when the portal is now broken, so the run that raises the gap gets
    no D167 retry (D170). An ignored gap is not broken: ``_portal_is_broken`` says
    the same, so the retry still waits.
    """
    if result.portal_key is None or result.reason not in TECHNICAL_REASONS:
        return False
    if not await technical_streak(conn, result.portal_key, inputs.cfg.gaps.broken_after):
        return False
    ctx = GapContext(profiles=inputs.cfg.profiles, gaps_cfg=inputs.cfg.gaps)
    gap = await raise_gap(conn, ctx, _broken_sighting(inputs, reading, result.portal_key, result.reason))
    return gap.state in _BROKEN_GAP_STATES


def _broken_sighting(inputs: _Inputs, reading: Reading, portal_key: str, reason: FetchReason) -> GapSighting:
    """Build one PORTAL_BROKEN sighting from the result and the reading (BR-F03, R-11.12, D157)."""
    ruc_emisor = reading.value("ruc_emisor")
    portal = inputs.directory.portals.get(portal_key)
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
        raised_with=None if portal is None else config_digest(portal),
    )


async def _record_result(
    conn: AsyncConnection[Any],
    inputs: _Inputs,
    reading: Reading,
    fetcher: str,
    result: FetchResult,
    delay: timedelta | None,
) -> None:
    """Store an ``ok`` result's files and enqueue deliver, or schedule the next try, in one transaction.

    A technical failure gets one retry on its first schedule entry (D167); every other
    failure, and a technical one with no retry left, moves the chain on at once. The
    summary goes out first in every outcome that does not move straight on (D206).
    """
    if result.ok:
        await _record_found(conn, inputs, reading, fetcher, result)
    elif result.reason in WAIT_REASONS:
        await _record_wait(conn, inputs, reading, fetcher, result, delay)
    elif result.reason in TECHNICAL_REASONS and delay is not None:
        # D167: a technical retry sends no notice and no admin alert.
        await post_summary(conn, inputs.cfg, inputs.job.invoice_id)
        await enqueue(conn, inputs.job.invoice_id, FETCH_STEP, await run_after_delay(conn, delay))
    else:
        await enqueue(conn, inputs.job.invoice_id, FETCH_STEP)


async def _record_found(
    conn: AsyncConnection[Any], inputs: _Inputs, reading: Reading, fetcher: str, result: FetchResult
) -> None:
    """Store an ``ok`` result's files, say so, and move the invoice on to deliver (R-5.1)."""
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
    await _found_notice(conn, inputs, reading, fetcher, result)
    await enqueue(conn, inputs.job.invoice_id, DELIVER_STEP)
    # The invoice got past the fetch step, so its fixed portal gaps close (R-11.9).
    repo = inputs.cfg.gaps.github.repo
    await close_if_fixed(conn, inputs.job.invoice_id, PORTAL_MISSING, repo)
    await close_if_fixed(conn, inputs.job.invoice_id, PORTAL_BROKEN, repo)


async def _record_wait(
    conn: AsyncConnection[Any],
    inputs: _Inputs,
    reading: Reading,
    fetcher: str,
    result: FetchResult,
    delay: timedelta | None,
) -> None:
    """Schedule the retry of a wait and send its first notice; a wait with no delay moves on (D100, D206)."""
    run_after = None if delay is None else await run_after_delay(conn, delay)
    await enqueue(conn, inputs.job.invoice_id, FETCH_STEP, run_after)
    if run_after is not None:
        await post_summary(conn, inputs.cfg, inputs.job.invoice_id)
        if _is_first_attempt(inputs.attempts, fetcher, WAIT_REASONS):
            await _wait_notice(conn, inputs, reading, fetcher, run_after)
    if result.reason is FetchReason.AUTH_REQUIRED and _is_first_attempt(
        inputs.attempts, fetcher, {FetchReason.AUTH_REQUIRED}
    ):
        await _alert_sign_in(conn, inputs)


#: The fetchers whose XML confirms the reading: the issuer's portal and the printed QR link;
#: a mailbox XML is unsigned until BR-F10 (D202).
CONFIRMING_FETCHERS = frozenset({PORTAL_FETCHER, QR_URL_FETCHER})


async def _found_notice(
    conn: AsyncConnection[Any], inputs: _Inputs, reading: Reading, fetcher: str, result: FetchResult
) -> None:
    """Post the summary with BR-F16's lines, or queue BR-F16 when the summary is already out (D206, BR-F16).

    The lines come from :func:`_found_lines`; the summary carries them when it goes out
    with this ``ok`` fetch, otherwise BR-F16 is a message of its own.
    """
    text = await _found_lines(conn, inputs, reading, fetcher, result)
    if not await post_summary(conn, inputs.cfg, inputs.job.invoice_id, found_lines=text.split("\n")):
        await _notify(conn, inputs, "BR-F16", text)


async def _found_lines(
    conn: AsyncConnection[Any], inputs: _Inputs, reading: Reading, fetcher: str, result: FetchResult
) -> str:
    """Confirm the reading with the fetched XML and return BR-F16's text for an ``ok`` fetch (D202, BR-F16).

    Only an XML from the issuer's portal or the printed QR link confirms the reading: a
    mailbox XML is unsigned until BR-F10, so it is filed but changes nothing (D202). The
    XML was matched to the invoice by R-5.6, so a parse failure here is a bug and raises.
    The second line names the fields the summary's ⚠️ Revisa named that are no longer doubtful.
    """
    invoice_id = inputs.job.invoice_id
    text = FOUND_LINE.format(docs_desc=docs_desc(sorted({fetched.kind for fetched in result.files})))
    xml_file = next((fetched for fetched in result.files if fetched.kind == "xml"), None)
    if xml_file is not None and fetcher in CONFIRMING_FETCHERS:
        xml_reading = parse_invoice_xml(await asyncio.to_thread(xml_file.path.read_bytes))
        confirmed = confirmed_by_xml(reading, xml_reading)
        await save_reading(conn, invoice_id, confirmed)
        today = await owner_today(conn, inputs.cfg.owner.timezone)
        still = review_fields(confirmed, inputs.cfg.extraction, today)
        named = set(confirmed_names(reading, confirmed))
        cleared = [
            name
            for name in review_fields(reading, inputs.cfg.extraction, today)
            if name in named and name not in still
        ]
        if cleared:
            text += "\n" + CONFIRMED_LINE.format(fields_desc=fields_desc(cleared))
    return text


def _is_first_attempt(attempts: Sequence[Attempt], fetcher: str, reasons: Collection[FetchReason]) -> bool:
    """Return True when ``fetcher`` has no earlier attempt whose reason is in ``reasons`` (D100)."""
    return not any(attempt.fetcher == fetcher and attempt.reason in reasons for attempt in attempts)


async def _wait_notice(
    conn: AsyncConnection[Any],
    inputs: _Inputs,
    reading: Reading,
    fetcher: str,
    next_at: datetime,
) -> None:
    """Queue the first wait's message: BR-F13 for the inbox, the portal line for any other (D100, D189)."""
    invoice_id = inputs.job.invoice_id
    if fetcher != EMAIL_INBOX_FETCHER:
        await _notify(conn, inputs, None, FETCH_WAITING.format(id=invoice_id))
        return
    owner_timezone = ZoneInfo(inputs.cfg.owner.timezone)
    inbox_schedule = _schedule_entries(EMAIL_INBOX_FETCHER, inputs.flow, inputs.invoice, inputs.schedules)
    last_at = next_at + sum(inbox_schedule[1:], timedelta())
    today = await owner_today(conn, inputs.cfg.owner.timezone)
    looks = LookTimes(last_at.astimezone(owner_timezone), today)
    taught_url = await _approved_link_url(conn, reading)
    text = docs_needed_text(
        invoice_id,
        reading,
        inputs.flow,
        inputs.attempts,
        StoreFacts(inputs.directory, taught_url is not None),
        looks,
    )
    offer_portal = await _offer_portal(conn, inputs, reading)
    await _notify(
        conn,
        inputs,
        RULE_DOCS_NEEDED,
        text,
        docs_needed_buttons(invoice_id, offer_portal),
        _portal_links(inputs, reading, taught_url),
    )


async def _approved_link_url(conn: AsyncConnection[Any], reading: Reading) -> str | None:
    """Return the URL of the issuer's approved taught link, or None when it has none (D208)."""
    ruc_emisor = reading.value("ruc_emisor")
    if ruc_emisor is None:
        return None
    link = await taught_links.get(conn, int(ruc_emisor))
    return link.url if link is not None and link.state == "approved" else None


def _portal_links(inputs: _Inputs, reading: Reading, taught_url: str | None) -> Sequence[Link]:
    """Return the one "open the portal" link D208 offers for this reading, or no link."""
    ruc_emisor = reading.value("ruc_emisor")
    portal = None if ruc_emisor is None else portal_for(ruc_emisor, inputs.directory)
    link = open_portal_link(inputs.attempts, portal, taught_url)
    return () if link is None else (link,)


async def _offer_portal(conn: AsyncConnection[Any], inputs: _Inputs, reading: Reading) -> bool:
    """Return True when BR-F13 offers [🔗]: teaching is on and the issuer has no portal or link (D189)."""
    ruc_emisor = reading.value("ruc_emisor")
    if not inputs.cfg.gaps.teach_via_chat or ruc_emisor is None:
        return False
    if portal_for(ruc_emisor, inputs.directory) is not None:
        return False
    return not await taught_links.exists(conn, int(ruc_emisor))


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
        await post_summary(conn, inputs.cfg, inputs.job.invoice_id)
        gap = await _raise_portal_gap(conn, inputs, reading)
        taught_url = await _approved_link_url(conn, reading)
        text = manual_message(
            reading,
            inputs.flow,
            inputs.attempts,
            StoreFacts(
                inputs.directory,
                taught_url is not None,
                await _portal_is_broken(conn, reading, inputs.directory),
            ),
            gap_line=_gap_line(gap),
        )
        await set_state(
            conn, inputs.job.invoice_id, MANUAL_REQUIRED, rule_id=RULE_EXHAUSTED, expected=FETCHING_DOCS
        )
        await _notify(
            conn,
            inputs,
            RULE_EXHAUSTED,
            text,
            [
                retry_button(inputs.job.invoice_id),
                Button(id=command_button_id("cancelar", str(inputs.job.invoice_id)), title=BUTTON_VOID),
            ],
            _portal_links(inputs, reading, taught_url),
        )
        await _ask_for_portal(conn, inputs, gap, reading)
    return _Outcome(fetcher=None, portal_key=None, reason=RULE_EXHAUSTED)


async def _portal_is_broken(conn: AsyncConnection[Any], reading: Reading, directory: StoreDirectory) -> bool:
    """Return True when the issuer's portal has an open or reported PORTAL_BROKEN gap (BR-F03)."""
    ruc_emisor = reading.value("ruc_emisor")
    if ruc_emisor is None:
        return False
    portal = portal_for(ruc_emisor, directory)
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
    """Ask the sender for the issuer's portal once, when BR-F02 is due (BR-F02, D190)."""
    if gap is None or not gap.raised_now or not inputs.cfg.gaps.teach_via_chat:
        return
    ruc_emisor = reading.value("ruc_emisor")
    if ruc_emisor is None or await taught_links.exists(conn, int(ruc_emisor)):
        return
    razon_social = reading.value("razon_social") or UNKNOWN_ISSUER
    text = MESSAGES[RULE_ASK_PORTAL].format(razon_social=razon_social, ruc_emisor=ruc_emisor)
    await _notify(conn, inputs, RULE_ASK_PORTAL, text, ask_portal_buttons(inputs.job.invoice_id))


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
    conn: AsyncConnection[Any],
    inputs: _Inputs,
    rule_id: str | None,
    text: str,
    buttons: Sequence[Button] = (),
    links: Sequence[Link] = (),
) -> None:
    """Queue ``text`` with its buttons and links for the invoice's sender in the caller's transaction."""
    head = inputs.head
    message = outbound(
        Addressee(head.origin_channel, head.profile_id), text, rule_id=rule_id, buttons=buttons, links=links
    )
    await queue_outbound(conn, message, invoice_id=inputs.job.invoice_id)


async def run_after_delay(conn: AsyncConnection[Any], delay: timedelta) -> datetime:
    """Return the database clock plus ``delay``; the caller owns the transaction (D65)."""
    cur = await conn.execute("SELECT now() + %s", (delay,))
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("SELECT now() + delay yielded no row")
    return cast(datetime, row[0])


def fetch_invoice(reading: Reading, invoice_id: int) -> FetchInvoice:
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
