"""Open an approved portal link once for a waiting invoice and file what it gives (D193, R-11.5).

:func:`make_portal_probe_handler` builds the handler of the ``portal_probe`` job step. It reads
the queued probe, the approved link and the invoice, probes the link with no connection held,
and in one transaction with the invoice locked records the outcome. A matching file set that
covers the flow's documents is filed and the invoice moves to ``DELIVERING``; otherwise the
admins hear what the portal looks like (BR-S06). Either way the held issue is released and its
draft report queued. A busy portal is tried again after 15 minutes, 1 hour and 6 hours. A probe
that is gone, a link that moved or an invoice that left the waiting states writes nothing.
"""

from __future__ import annotations

import logging
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

from psycopg import AsyncConnection

from invoicebot.channels.media_limits import DIRECTORY_MODE
from invoicebot.config.models import Config, Flow
from invoicebot.fetchers.base import FetchedFile, FetchInvoice
from invoicebot.fetchers.browser.probe_protocol import ProbeOutcome, ProbeReport
from invoicebot.fetchers.portal_probe import Probe
from invoicebot.gaps.kinds import portal_gap_fingerprint
from invoicebot.net.taught_url import without_session
from invoicebot.pipeline.fetch import fetch_invoice, run_after_delay
from invoicebot.pipeline.handlers import TMP_DIRECTORY
from invoicebot.pipeline.outgoing import queue_admin_alert
from invoicebot.pipeline.probe_files import PdfReader, pdf_matches
from invoicebot.pipeline.steps import DELIVER_STEP, PORTAL_PROBE_STEP
from invoicebot.queue import outbox
from invoicebot.queue.jobs import Job, enqueue
from invoicebot.queue.worker import Handler
from invoicebot.rules.messages_es import MESSAGES, PROBE_OUTCOME_DESC
from invoicebot.states import DELIVERING, FETCHING_DOCS, MANUAL_REQUIRED
from invoicebot.store import portal_probes, taught_links
from invoicebot.store.db import Pool
from invoicebot.store.gaps import get_gap
from invoicebot.store.invoices import (
    FileRecord,
    add_file,
    load_flow,
    load_invoice_head,
    load_reading,
    lock_invoice,
    set_state,
)

logger = logging.getLogger(__name__)

#: The states a probe may still file into (D193).
_WAITING = frozenset({FETCHING_DOCS, MANUAL_REQUIRED})
#: How long a busy portal waits before each new try; after the last one the probe ends as busy (D193).
PROBE_RETRY_DELAYS: tuple[timedelta, ...] = (timedelta(minutes=15), timedelta(hours=1), timedelta(hours=6))
#: The rule of the admins' answers to a probe (BR-S06).
RULE_PROBE_FILED = "BR-S06_probe_filed"
RULE_PROBE_FORM = "BR-S06_probe_form"
RULE_PROBE_FAILED = "BR-S06_probe_failed"


class ProbePort(Protocol):
    """Opens one portal link for an invoice (D193).

    An implementation keeps every file it returns inside ``out_dir`` and deletes the rest;
    it reports expected failures as an outcome and never raises for them.
    """

    async def probe(self, url: str, invoice: FetchInvoice, out_dir: Path) -> Probe:
        """Probe ``url`` for ``invoice`` into ``out_dir``."""
        ...


@dataclass(frozen=True)
class ProbeDeps:
    """What the probe handler needs, passed in by the composition root (D193)."""

    pool: Pool
    cfg: Config
    prober: ProbePort
    pdf_reader: PdfReader
    data_dir: Path


@dataclass(frozen=True)
class _Target:
    """The queued probe and what it needs to run, read before probing (D193)."""

    probe: portal_probes.PortalProbe
    url: str
    invoice: FetchInvoice
    flow: Flow


def make_portal_probe_handler(deps: ProbeDeps) -> Handler:
    """Build the handler of the ``portal_probe`` step (D193).

    The handler ends quietly when no probe is queued for the invoice or its link is no longer
    approved at the probe's version. An invoice that left ``FETCHING_DOCS``/``MANUAL_REQUIRED``
    ends the probe as ``technical``. The folder the probe wrote into is deleted unless its files
    were filed, also when the prober raises.
    """

    async def handle(job: Job) -> None:
        started = time.perf_counter()
        target = await _read_target(deps, job)
        if target is None:
            return
        out_dir = deps.data_dir / TMP_DIRECTORY / str(job.invoice_id) / f"{job.id}-{uuid.uuid4().hex}"
        out_dir.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
        stored = False
        try:
            probe = await _probed(deps, target, out_dir)
            async with deps.pool.connection() as conn, conn.transaction():
                stored = await _conclude(conn, deps, job, target, probe)
        finally:
            if not stored:
                shutil.rmtree(out_dir, ignore_errors=True)
        logger.info(
            "step done",
            extra={
                "step": PORTAL_PROBE_STEP,
                "invoice_id": job.invoice_id,
                "outcome": probe.report.outcome.value,
                "filed": stored,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            },
        )

    return handle


async def _read_target(deps: ProbeDeps, job: Job) -> _Target | None:
    """Return what the probe needs, or None when it ends quietly (D193).

    An invoice that left the waiting states ends its probe as ``technical`` first.
    """
    async with deps.pool.connection() as conn:
        probe = await portal_probes.get_for_invoice(conn, job.invoice_id)
        if probe is None:
            return None
        head = await load_invoice_head(conn, job.invoice_id)
        if head is None or head.state not in _WAITING:
            await _abandon(deps.pool, probe, job.invoice_id)
            return None
        url = await taught_links.approved_url(conn, probe.ruc_emisor, probe.link_version)
        reading = await load_reading(conn, job.invoice_id)
        flow_name = await load_flow(conn, job.invoice_id)
    if url is None:
        return None
    if reading is None:
        raise RuntimeError(f"invoice {job.invoice_id} has no reading")
    flow = None if flow_name is None else deps.cfg.flows.get(flow_name)
    if flow is None:
        raise RuntimeError(f"invoice {job.invoice_id} has no known flow")
    return _Target(probe, without_session(url), fetch_invoice(reading, job.invoice_id), flow)


async def _abandon(pool: Pool, probe: portal_probes.PortalProbe, invoice_id: int) -> None:
    """End a probe whose invoice left the waiting states and release its issue (D193)."""
    async with pool.connection() as conn, conn.transaction():
        await lock_invoice(conn, invoice_id)
        report = ProbeReport(outcome=ProbeOutcome.TECHNICAL)
        finished = await portal_probes.finish(
            conn, probe.ruc_emisor, probe.link_version, report.outcome.value, report.model_dump(mode="json")
        )
        if finished:
            await outbox.release_issue(conn, portal_gap_fingerprint(probe.ruc_emisor))


async def _probed(deps: ProbeDeps, target: _Target, out_dir: Path) -> Probe:
    """Probe the link, then drop a lone PDF that does not read as this invoice (D193, R-5.6)."""
    probe = await deps.prober.probe(target.url, target.invoice, out_dir)
    if probe.files and not any(file.kind == "xml" for file in probe.files):
        for file in probe.files:
            if not await pdf_matches(file.path, target.invoice, deps.pdf_reader):
                return Probe(probe.report, ())
    return probe


async def _conclude(
    conn: AsyncConnection[Any], deps: ProbeDeps, job: Job, target: _Target, probe: Probe
) -> bool:
    """Record the probe in the caller's transaction and return whether its files were filed (D193).

    The invoice is locked and the probe row read again, so a probe that ended, a link that moved
    or a second run of the same job writes nothing.
    """
    invoice_id = job.invoice_id
    await lock_invoice(conn, invoice_id)
    current = await portal_probes.get_for_invoice(conn, invoice_id)
    if current is None or (current.ruc_emisor, current.link_version) != (
        target.probe.ruc_emisor,
        target.probe.link_version,
    ):
        return False
    head = await load_invoice_head(conn, invoice_id)
    waiting_state = None if head is None or head.state not in _WAITING else head.state
    report = probe.report
    if (
        report.outcome is ProbeOutcome.BUSY
        and waiting_state is not None
        and current.tries < len(PROBE_RETRY_DELAYS)
    ):
        await _retry_later(conn, job, current)
        return False
    ruc = current.ruc_emisor
    stored_report = report.model_copy(update={"files": ()}).model_dump(mode="json")
    if not await portal_probes.finish(conn, ruc, current.link_version, report.outcome.value, stored_report):
        return False
    issue_key = portal_gap_fingerprint(ruc)
    await outbox.release_issue(conn, issue_key)
    await _queue_report(conn, deps.cfg, issue_key, target.url, stored_report)
    filed = waiting_state is not None and _covers(target.flow, probe.files)
    if filed and waiting_state is not None:
        await _file(conn, invoice_id, probe.files, waiting_state)
    await _tell_admins(conn, deps.cfg, ruc, invoice_id, report, filed=filed)
    return filed


async def _retry_later(conn: AsyncConnection[Any], job: Job, current: portal_probes.PortalProbe) -> None:
    """Count the try and enqueue the probe again after its next delay (D193)."""
    await portal_probes.count_try(conn, current.ruc_emisor, current.link_version)
    run_after = await run_after_delay(conn, PROBE_RETRY_DELAYS[current.tries])
    await enqueue(conn, job.invoice_id, PORTAL_PROBE_STEP, run_after)


async def _queue_report(
    conn: AsyncConnection[Any],
    cfg: Config,
    gap_fingerprint: str,
    url: str,
    report: dict[str, Any],
) -> None:
    """Queue the report as an issue comment when GitHub is on and the gap exists (D193, R-11.12)."""
    if not cfg.gaps.github.enabled or await get_gap(conn, gap_fingerprint) is None:
        return
    await outbox.add(
        conn,
        "github",
        cfg.gaps.github.repo,
        {"op": "probe_report", "fingerprint": gap_fingerprint, "url": url, "report": report},
    )


def _covers(flow: Flow, files: tuple[FetchedFile, ...]) -> bool:
    """Return True when ``files`` hold every document kind the flow requires (D193)."""
    return bool(files) and set(flow.required_docs) <= {file.kind for file in files}


async def _file(
    conn: AsyncConnection[Any],
    invoice_id: int,
    files: tuple[FetchedFile, ...],
    state: str,
) -> None:
    """Store the files, move the invoice to ``DELIVERING`` and enqueue deliver (D193, R-5.1)."""
    # shortcut: a probed file's local_path stays under tmp/ until deliver moves it, as a fetched one's does.
    for file in files:
        record = FileRecord(
            kind=file.kind, local_path=str(file.path), remote_ref=None, sha256=file.sha256, size=file.size
        )
        await add_file(conn, invoice_id, record)
    await set_state(conn, invoice_id, DELIVERING, expected=state)
    await enqueue(conn, invoice_id, DELIVER_STEP)


async def _tell_admins(
    conn: AsyncConnection[Any],
    cfg: Config,
    ruc_emisor: int,
    invoice_id: int,
    report: ProbeReport,
    *,
    filed: bool,
) -> None:
    """Queue BR-S06's answer: filed, a form was read, or the portal could not be checked (D193)."""
    rule_id = RULE_PROBE_FILED if filed else RULE_PROBE_FORM if report.fields else RULE_PROBE_FAILED
    text = MESSAGES[rule_id].format(
        ruc_emisor=ruc_emisor, id=invoice_id, outcome_desc=PROBE_OUTCOME_DESC[report.outcome.value]
    )
    await queue_admin_alert(conn, cfg.profiles, rule_id, text)
