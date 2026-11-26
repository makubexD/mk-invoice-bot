"""The portal probe helper entry: read one form and print its report (D193, R-9.16).

Run as ``python -m invoicebot.fetchers.browser.probe``. It reads one ``ProbeRequest``
from stdin, runs :func:`probe` behind the guard proxy with no ``http`` host and images
off, and prints one ``ProbeReport`` line. Nothing from the page or the invoice is
printed; a failure prints only its class name and the helper exits 0.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from invoicebot.fetchers.browser.probe_fill import probe
from invoicebot.fetchers.browser.probe_protocol import ProbeOutcome, ProbeReport, ProbeRequest
from invoicebot.fetchers.browser.protocol import BadRequest, error_class_of, parse_helper_args
from invoicebot.fetchers.browser.session import BrowseOptions, browse, emit, limit_resources, read_request
from invoicebot.net.guard import GuardReason

__all__ = ["guarded_report", "main"]

logger = logging.getLogger(__name__)

#: Outcomes a block of a navigated host turns into ``guard_blocked``; a captcha keeps its own (D193).
BLOCKABLE_OUTCOMES = frozenset({ProbeOutcome.TECHNICAL, ProbeOutcome.BUSY, ProbeOutcome.NO_DOWNLOAD})
#: The probe never reaches a plain ``http`` host and needs no images (D193).
_OPTIONS = BrowseOptions(http_hosts=frozenset(), allow_images=False)


def guarded_report(report: ProbeReport, block: GuardReason | None) -> ProbeReport:
    """Promote a technical, busy or no-download report to ``guard_blocked`` after a block (R-5.10, D193)."""
    if block is None or report.outcome not in BLOCKABLE_OUTCOMES:
        return report
    return ProbeReport(outcome=ProbeOutcome.GUARD_BLOCKED)


def main() -> int:
    """Read the request, run one probe and print its report (D193)."""
    try:
        chromium_sandbox = parse_helper_args(sys.argv[1:])
        request = read_request(ProbeRequest)
    except BadRequest:
        emit(ProbeReport(outcome=ProbeOutcome.TECHNICAL))
        return 0
    limit_resources(request.max_bytes)
    try:
        report, block = asyncio.run(
            browse(
                _OPTIONS,
                request.page_timeout_ms,
                lambda page: probe(page, request),
                chromium_sandbox=chromium_sandbox,
            )
        )
    except Exception as error:  # top-level boundary: a page error must not print page text (R-9.16, D169)
        logger.error("probe helper failed: %s", type(error).__name__)
        emit(ProbeReport(outcome=ProbeOutcome.TECHNICAL, error_class=error_class_of(error)))
    else:
        emit(guarded_report(report, block))
    return 0


if __name__ == "__main__":
    sys.exit(main())
