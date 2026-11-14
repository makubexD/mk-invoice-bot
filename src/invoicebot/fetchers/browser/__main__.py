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
import resource
import sys
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Page, Request, async_playwright

from invoicebot.config.models import MediaMaxBytes, Portal
from invoicebot.fetchers.base import FetchReason, FetchResult
from invoicebot.fetchers.browser.form import fill_and_download
from invoicebot.fetchers.browser.protocol import HelperFile, HelperOutput, HelperRequest
from invoicebot.fetchers.browser.proxy import GuardProxy
from invoicebot.fetchers.browser.routing import RouteGuard, SystemResolver
from invoicebot.net.guard import GuardReason

logger = logging.getLogger(__name__)

#: The helper reads one small JSON object; a larger stdin is a bug, not a page (R-9.2).
MAX_REQUEST_BYTES = 1024 * 1024
#: CPU seconds the whole helper may burn before the kernel ends it (R-9.2).
MAX_CPU_SECONDS = 120
#: Chromium's launch flags from spec 03 §8; images are added below for captcha pages. The
#: WebRTC policy keeps a page's UDP (STUN/TURN) off the network: it would skip the guard
#: proxy and could probe internal addresses (R-5.10).
LAUNCH_ARGS: tuple[str, ...] = (
    "--disable-dev-shm-usage",
    "--no-zygote",
    "--renderer-process-limit=1",
    "--disable-gpu",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
)
#: Disables image loading for portals without a captcha.
IMAGES_OFF = "--blink-settings=imagesEnabled=false"
#: Results a blocked navigation turns into ``guard_blocked``; a not-found page keeps its reason.
BLOCKABLE_REASONS = frozenset({FetchReason.TECHNICAL, FetchReason.TIMEOUT})


class _BadRequest(Exception):
    """The helper's stdin is not one valid request."""


def main() -> int:
    """Read the request, run one fetch and print its result (S10.1)."""
    try:
        request = _read_request()
    except _BadRequest:
        _emit(HelperOutput(reason=FetchReason.TECHNICAL))
        return 0
    _limit_resources(request.max_bytes)
    try:
        asyncio.run(_run_browser(request))
    except Exception as error:  # top-level boundary: a page error must not print page text (R-9.16)
        logger.error("browser helper failed: %s", type(error).__name__)
        _emit(HelperOutput(reason=FetchReason.TECHNICAL))
    return 0


def _read_request() -> HelperRequest:
    """Parse and validate the one JSON request from stdin, capped at ``MAX_REQUEST_BYTES``."""
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise _BadRequest
    try:
        return HelperRequest.model_validate_json(raw)
    except ValueError as error:
        # JSONDecodeError, UnicodeDecodeError and pydantic's ValidationError are all
        # ValueErrors; a malformed request is malformed whichever one it is.
        raise _BadRequest from error


def _limit_resources(max_bytes: MediaMaxBytes) -> None:
    """Cap CPU time and file writes before Chromium starts (R-9.1, R-9.2)."""
    max_file_bytes = max(max_bytes.pdf, max_bytes.xml)
    resource.setrlimit(resource.RLIMIT_CPU, (MAX_CPU_SECONDS, MAX_CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_FSIZE, (max_file_bytes, max_file_bytes))
    # shortcut: Chromium reserves terabytes of virtual address space (the V8 cage) and
    # fails to start under RLIMIT_AS even at 64 GiB (measured 2026-11-13), so the browser
    # memory cap is the app container's mem_limit (R-9.1); for ops.


async def _run_browser(request: HelperRequest) -> None:
    """Launch Chromium behind the guard proxy and print the result (S10.1)."""
    portal = request.portal
    guard = RouteGuard(allow_images=_allow_images(portal))
    async with (
        async_playwright() as playwright,
        GuardProxy(
            _http_hosts(portal),
            SystemResolver(),
            idle_timeout_s=request.page_timeout_ms / 1000,
        ) as proxy,
    ):
        browser = await playwright.chromium.launch(**_launch_options(portal, proxy.port))
        try:
            context = await browser.new_context(accept_downloads=True, service_workers="block")
            context.set_default_timeout(request.page_timeout_ms)
            await context.route("**/*", guard.on_route)
            page = await context.new_page()
            navigation_hosts = record_navigation_hosts(page)
            result = await fill_and_download(
                page, portal, request.invoice, request.out_dir, request.max_bytes
            )
        finally:
            await browser.close()
    _emit(_output(_guarded_result(result, proxy.block_for(navigation_hosts))))


def record_navigation_hosts(page: Page) -> list[str]:
    """Return a list that collects the host of every main-frame navigation, redirect hops included.

    A proxy refusal of a plain ``http`` hop still ends the navigation with a 403 page, not a
    failed request, so the helper compares these hosts with the proxy's refused hosts (R-5.10).
    """
    hosts: list[str] = []

    def on_request(request: Request) -> None:
        if request.is_navigation_request() and request.frame == page.main_frame:
            host = urlsplit(request.url).hostname
            if host is not None and host not in hosts:
                hosts.append(host)

    page.on("request", on_request)
    return hosts


def _guarded_result(result: FetchResult, block: GuardReason | None) -> FetchResult:
    """Promote a technical or timeout result to ``guard_blocked`` (R-5.10).

    ``block`` is the proxy's refusal of a host the page navigated to; a blocked sub-resource
    gives none, and a not-found page keeps its reason.
    """
    if block is None or result.reason not in BLOCKABLE_REASONS:
        return result
    return FetchResult(reason=FetchReason.GUARD_BLOCKED, files=(), portal_key=result.portal_key)


def _launch_args(portal: Portal) -> list[str]:
    """Spec 03 §8's flags, with image loading off unless a captcha needs images."""
    args = list(LAUNCH_ARGS)
    if portal.captcha == "none":
        args.append(IMAGES_OFF)
    return args


def _launch_options(portal: Portal, proxy_port: int) -> dict[str, Any]:
    """Chromium's launch keyword arguments, with every connection through the proxy.

    Playwright already adds ``--proxy-bypass-list=<-loopback>`` for a launch proxy, so
    loopback goes through the proxy too; passing that flag again would duplicate it.
    """
    return {
        "headless": True,
        "args": _launch_args(portal),
        "proxy": {"server": f"http://127.0.0.1:{proxy_port}"},
    }


def _http_hosts(portal: Portal) -> frozenset[str]:
    """The one host an ``http`` portal may reach (D76), or none."""
    if not portal.allow_http or portal.url is None:
        return frozenset()
    host = urlsplit(portal.url).hostname
    return frozenset({host}) if host else frozenset()


def _allow_images(portal: Portal) -> bool:
    """Whether the portal's captcha needs images loaded."""
    return portal.captcha != "none"


def _output(result: FetchResult) -> HelperOutput:
    """The helper's output object: the reason and its files, nothing else."""
    return HelperOutput(
        reason=result.reason,
        files=tuple(
            HelperFile(kind=file.kind, path=file.path, size=file.size, sha256=file.sha256)
            for file in result.files
        ),
    )


def _emit(output: HelperOutput) -> None:
    """Write the result as the helper's one stdout line."""
    sys.stdout.write(output.model_dump_json() + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
