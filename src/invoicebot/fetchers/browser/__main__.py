"""Browser helper process: fill a portal form and print one JSON result (S10.1, D82).

The fetch handler starts this module as ``python -m invoicebot.fetchers.browser`` and writes
one JSON request to stdin: the portal, the invoice fields, the output folder, the media size
limits and the page timeout. The helper launches Chromium's headless shell with spec 03 §8's
flags behind its guard proxy, so every hop is checked, drops unneeded resources with the
route guard, fills the form and downloads the invoice's files, then prints exactly one JSON
line: ``{"reason": <code>, "files": [...]}``. Playwright
and Chromium live only in this process and exit with it, so the bot never holds them while
idle (R-9.1). Nothing from the page is printed, and every unexpected error becomes
``technical`` with exit code 0; stderr gets only the exception's class name (R-9.16).
"""

from __future__ import annotations

import asyncio
import logging
import sys

from invoicebot.fetchers.base import FetchReason
from invoicebot.fetchers.browser.form import fill_and_download
from invoicebot.fetchers.browser.protocol import HelperOutput, HelperRequest
from invoicebot.fetchers.browser.session import (
    BadRequest,
    _guarded_result,
    _launch_options,
    browse,
    emit,
    limit_resources,
    output,
    read_request,
    record_navigation_hosts,
)

#: Re-exported from :mod:`.session` because the integration tests import them here.
__all__ = ["_guarded_result", "_launch_options", "record_navigation_hosts"]

logger = logging.getLogger(__name__)

#: The portal entry's name for the shared parse failure.
_BadRequest = BadRequest


def main() -> int:
    """Read the request, run one fetch and print its result (S10.1)."""
    try:
        request = _read_request()
    except _BadRequest:
        emit(HelperOutput(reason=FetchReason.TECHNICAL))
        return 0
    limit_resources(request.max_bytes)
    try:
        asyncio.run(_run_browser(request))
    except Exception as error:  # top-level boundary: a page error must not print page text (R-9.16)
        logger.error("browser helper failed: %s", type(error).__name__)
        emit(HelperOutput(reason=FetchReason.TECHNICAL))
    return 0


def _read_request() -> HelperRequest:
    """Parse and validate the one JSON request from stdin, capped at ``MAX_REQUEST_BYTES``."""
    return read_request(HelperRequest)


async def _run_browser(request: HelperRequest) -> None:
    """Launch Chromium behind the guard proxy and print the result (S10.1)."""
    portal = request.portal
    result = await browse(
        portal,
        request.page_timeout_ms,
        lambda page: fill_and_download(page, portal, request.invoice, request.out_dir, request.max_bytes),
    )
    emit(output(result))


if __name__ == "__main__":
    sys.exit(main())
