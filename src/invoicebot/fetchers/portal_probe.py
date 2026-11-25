"""The bot's side of the portal probe helper (D193, R-9.2).

``PortalProber`` runs the probe helper once for a portal link, in its own process and
temporary folder like the portal fetcher's helper, and checks what comes back. Every file
the helper reports must lie inside the output directory, fit its kind's byte limit and
match the bot's own SHA-256. With an XML among the files, the files must also pass the
portal fetcher's own match against the invoice; a lone PDF is checked for its header only,
because its content is judged later. A failed check deletes the files. Nothing here logs an
invoice value, page text or the URL.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from invoicebot.config.models import MediaMaxBytes
from invoicebot.fetchers.base import FetchedFile, FetchInvoice, FetchReason, FetchResult, is_pdf
from invoicebot.fetchers.browser.probe_protocol import ProbeOutcome, ProbeReport, ProbeRequest
from invoicebot.fetchers.browser.protocol import HelperFile, HelperOutput
from invoicebot.fetchers.portal import (
    PAGE_TIMEOUT_MS,
    BrowserJob,
    check_files,
    delete_inside,
    result_from,
    run_browser_job,
)

#: The probe helper module; ``-P`` keeps the working directory off its import path.
DEFAULT_PROBE_COMMAND = (sys.executable, "-P", "-m", "invoicebot.fetchers.browser.probe")
#: The name a failed probe job logs under.
PROBE_NAME = "portal_probe"
#: How much of a PDF is read to look for its header.
_PDF_HEAD_BYTES = 1024


@dataclass(frozen=True)
class Probe:
    """One probe's checked report and the files that passed the checks (D193)."""

    report: ProbeReport
    files: tuple[FetchedFile, ...]


class PortalProber:
    """Runs the probe helper for one link and checks its report and files (D193, R-9.2)."""

    def __init__(
        self,
        max_bytes: MediaMaxBytes,
        timeout_s: float,
        *,
        helper_command: Sequence[str] = DEFAULT_PROBE_COMMAND,
    ) -> None:
        self._max_bytes = max_bytes
        self._timeout_s = timeout_s
        self._helper_command = helper_command

    async def probe(self, url: str, invoice: FetchInvoice, out_dir: Path) -> Probe:
        """Probe ``url`` for ``invoice``; the files are inside ``out_dir`` or deleted (D193)."""
        request = ProbeRequest(
            url=url,
            invoice=invoice,
            out_dir=out_dir,
            max_bytes=self._max_bytes,
            page_timeout_ms=PAGE_TIMEOUT_MS,
        )
        job = BrowserJob(
            command=self._helper_command,
            payload=request.model_dump_json().encode(),
            timeout_s=self._timeout_s,
            name=PROBE_NAME,
            out_dir=out_dir,
            model=ProbeReport,
        )
        output = await run_browser_job(job)
        if isinstance(output, FetchResult):
            busy = output.reason is FetchReason.TIMEOUT
            return _without_files(ProbeReport(outcome=ProbeOutcome.BUSY if busy else ProbeOutcome.TECHNICAL))
        return self._checked(output, invoice, out_dir)

    def _checked(self, report: ProbeReport, invoice: FetchInvoice, out_dir: Path) -> Probe:
        """Return ``report`` with the files that passed the checks, or the failing outcome."""
        root = out_dir.resolve()
        if report.outcome is not ProbeOutcome.OK:
            delete_inside(report.files, root)
            return _without_files(report)
        if not report.files:
            return _without_files(report, ProbeOutcome.NO_DOWNLOAD)
        if any(file.kind == "xml" for file in report.files):
            return self._with_xml(report, invoice, out_dir)
        return self._pdf_only(report, root)

    def _with_xml(self, report: ProbeReport, invoice: FetchInvoice, out_dir: Path) -> Probe:
        """Check the files as the portal fetcher does; a mismatch is ``not_found`` (R-5.6)."""
        output = HelperOutput(reason=FetchReason.OK, files=report.files)
        result = result_from(output, out_dir, invoice, self._max_bytes, PROBE_NAME)
        if result.reason is FetchReason.OK:
            return Probe(report, result.files)
        outcome = ProbeOutcome.TECHNICAL if result.reason is FetchReason.TECHNICAL else ProbeOutcome.NOT_FOUND
        return _without_files(report, outcome)

    def _pdf_only(self, report: ProbeReport, root: Path) -> Probe:
        """Check a PDF's path, size, hash and header; its content is judged later (D193)."""
        digests: dict[Path, str] = {}
        reason = check_files(report.files, root, self._max_bytes, digests)
        if reason is FetchReason.OK and all(_has_pdf_header(file) for file in report.files):
            files = tuple(
                FetchedFile(file.kind, file.path, file.path.stat().st_size, digests[file.path])
                for file in report.files
            )
            return Probe(report, files)
        delete_inside(report.files, root)
        return _without_files(report, ProbeOutcome.TECHNICAL)


def _has_pdf_header(file: HelperFile) -> bool:
    """Return whether the file starts like a PDF; an unreadable file does not."""
    try:
        with file.path.open("rb") as stream:
            return is_pdf(stream.read(_PDF_HEAD_BYTES))
    except OSError:
        return False


def _without_files(report: ProbeReport, outcome: ProbeOutcome | None = None) -> Probe:
    """Return a probe with no files, whose report drops the file list and may change outcome."""
    update = {"files": (), "outcome": outcome} if outcome is not None else {"files": ()}
    return Probe(report.model_copy(update=update), ())
