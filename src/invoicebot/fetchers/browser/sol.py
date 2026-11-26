"""The SOL helper entry: sign in and download one invoice's files (S10.1, R-5.2, R-9.10).

Run as ``python -m invoicebot.fetchers.browser.sol``. It reads one JSON request from
stdin, signs in to SUNAT SOL, runs the same form filler as the portal helper and prints
one JSON result. The credentials are typed only while the page stays on a hard-coded
SUNAT origin (R-9.10), and nothing from the request is logged or printed (R-9.16).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from playwright.async_api import Page

from invoicebot.config.models import MediaMaxBytes, Portal
from invoicebot.fetchers.base import FetchInvoice, FetchReason, FetchResult
from invoicebot.fetchers.browser.form import fill_and_download
from invoicebot.fetchers.browser.protocol import (
    BadRequest,
    HelperOutput,
    SolCredentialsIn,
    SolRequest,
    error_class_of,
    parse_helper_args,
)
from invoicebot.fetchers.browser.session import (
    browse,
    emit,
    guarded_result,
    limit_resources,
    output,
    portal_options,
    read_request,
)
from invoicebot.fetchers.browser.sol_login import SolCredentials, log_in
from invoicebot.net.guard import GuardReason

__all__ = ["SolCredentialsIn", "SolRequest", "main", "run_sol"]

logger = logging.getLogger(__name__)

type Fill = Callable[[Page, Portal, FetchInvoice, Path, MediaMaxBytes], Awaitable[FetchResult]]


async def run_sol(page: Page, request: SolRequest, fill: Fill = fill_and_download) -> FetchResult:
    """Sign in to SOL and run the invoice query, only if sign-in stayed on SUNAT (R-9.10)."""
    site = request.site
    await page.goto(site.login.url)
    credentials = request.credentials
    signed_in = await log_in(
        page,
        site.login,
        SolCredentials(
            ruc=credentials.ruc.get_secret_value(),
            user=credentials.user.get_secret_value(),
            password=credentials.password.get_secret_value(),
        ),
    )
    if not signed_in:
        return FetchResult(reason=FetchReason.TECHNICAL, files=(), portal_key=site.query.key)
    return await fill(page, site.query, request.invoice, request.out_dir, request.max_bytes)


async def _browse(request: SolRequest, chromium_sandbox: bool) -> tuple[FetchResult, GuardReason | None]:
    """Run the guarded browser for one SOL request (S10.1, R-5.10)."""
    return await browse(
        portal_options(request.site.query),
        request.page_timeout_ms,
        lambda page: run_sol(page, request),
        chromium_sandbox=chromium_sandbox,
    )


def main() -> int:
    """Read the request, run one SOL fetch and print its result (S10.1)."""
    try:
        chromium_sandbox = parse_helper_args(sys.argv[1:])
        request = read_request(SolRequest)
    except BadRequest:
        emit(HelperOutput(reason=FetchReason.TECHNICAL))
        return 0
    limit_resources(request.max_bytes)
    try:
        result, block = asyncio.run(_browse(request, chromium_sandbox))
    except Exception as error:  # top-level boundary: a page error must not print page text (R-9.16, D169)
        logger.error("SOL browser helper failed: %s", type(error).__name__)
        emit(HelperOutput(reason=FetchReason.TECHNICAL, error_class=error_class_of(error)))
    else:
        emit(output(guarded_result(result, block)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
