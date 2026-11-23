"""The Chromium session both helper entries share (S10.1, R-9.1, R-5.10).

The portal entry and the SOL entry each read one JSON request, ask this module for one
browser session and print one result. Here live the resource limits, the launch behind
the guard proxy, the navigation-host record and the JSON output, so neither entry copies
them. Playwright and Chromium live only in the helper process and exit with it (R-9.1).
"""

from __future__ import annotations

import resource
import sys
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Page, Request, async_playwright
from pydantic import BaseModel

from invoicebot.config.models import MediaMaxBytes, Portal
from invoicebot.fetchers.base import FetchReason, FetchResult
from invoicebot.fetchers.browser.protocol import BadRequest, HelperFile, HelperOutput
from invoicebot.fetchers.browser.proxy import GuardProxy
from invoicebot.fetchers.browser.routing import RouteGuard
from invoicebot.net.guard import GuardReason, SystemResolver

#: The helper reads one small JSON object; a larger stdin is a bug, not a page (R-9.2).
MAX_REQUEST_BYTES = 1024 * 1024
#: CPU seconds the whole helper may burn before the kernel ends it (R-9.2).
MAX_CPU_SECONDS = 120
#: Chromium's launch flags from spec 03 §8; images are added below for captcha pages. The
#: WebRTC policy keeps a page's UDP (STUN/TURN) off the network: it would skip the guard
#: proxy and could probe internal addresses (R-5.10).
LAUNCH_ARGS: tuple[str, ...] = (
    "--disable-dev-shm-usage",
    "--renderer-process-limit=1",
    "--disable-gpu",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
)
#: Only without Chromium's own sandbox, which refuses to start alongside it (D168).
NO_ZYGOTE = "--no-zygote"
#: Disables image loading for portals without a captcha.
IMAGES_OFF = "--blink-settings=imagesEnabled=false"
#: Results a blocked navigation turns into ``guard_blocked``; a not-found page keeps its reason.
BLOCKABLE_REASONS = frozenset({FetchReason.TECHNICAL, FetchReason.TIMEOUT})


type BrowserWork = Callable[[Page], Awaitable[FetchResult]]


def read_request[T: BaseModel](model: type[T]) -> T:
    """Parse and validate the one JSON request from stdin, capped at ``MAX_REQUEST_BYTES``.

    Raises:
        BadRequest: stdin is oversized or is not one valid ``model``.
    """
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise BadRequest
    try:
        return model.model_validate_json(raw)
    except ValueError as error:
        # JSONDecodeError, UnicodeDecodeError and pydantic's ValidationError are all
        # ValueErrors; a malformed request is malformed whichever one it is.
        raise BadRequest from error


def limit_resources(max_bytes: MediaMaxBytes) -> None:
    """Cap CPU time and file writes before Chromium starts (R-9.1, R-9.2)."""
    max_file_bytes = max(max_bytes.pdf, max_bytes.xml)
    resource.setrlimit(resource.RLIMIT_CPU, (MAX_CPU_SECONDS, MAX_CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_FSIZE, (max_file_bytes, max_file_bytes))
    # shortcut: Chromium reserves terabytes of virtual address space (the V8 cage) and
    # fails to start under RLIMIT_AS even at 64 GiB (measured 2026-11-13), so the browser
    # memory cap is the app container's mem_limit (R-9.1); for ops.


async def browse(
    portal: Portal,
    page_timeout_ms: int,
    work: BrowserWork,
    *,
    chromium_sandbox: bool,
) -> FetchResult:
    """Run ``work`` on one guarded page and return its result (S10.1, R-9.1, R-5.10).

    ``portal`` supplies the launch flags, the one allowed ``http`` host and whether
    images load. ``chromium_sandbox`` runs Chromium's own sandbox (D147). Every
    request goes through the guard proxy, and a refused host the page navigated to
    promotes a technical or timed-out result to ``guard_blocked``.
    """
    guard = RouteGuard(allow_images=_allow_images(portal))
    async with (
        async_playwright() as playwright,
        GuardProxy(
            _http_hosts(portal),
            SystemResolver(),
            idle_timeout_s=page_timeout_ms / 1000,
        ) as proxy,
    ):
        browser = await playwright.chromium.launch(
            **launch_options(portal, proxy.port, chromium_sandbox=chromium_sandbox)
        )
        try:
            context = await browser.new_context(accept_downloads=True, service_workers="block")
            context.set_default_timeout(page_timeout_ms)
            await context.route("**/*", guard.on_route)
            page = await context.new_page()
            navigation_hosts = record_navigation_hosts(page)
            result = await work(page)
        finally:
            await browser.close()
    return guarded_result(result, proxy.block_for(navigation_hosts))


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


def guarded_result(result: FetchResult, block: GuardReason | None) -> FetchResult:
    """Promote a technical or timeout result to ``guard_blocked`` (R-5.10).

    ``block`` is the proxy's refusal of a host the page navigated to; a blocked sub-resource
    gives none, and a not-found page keeps its reason.
    """
    if block is None or result.reason not in BLOCKABLE_REASONS:
        return result
    return FetchResult(reason=FetchReason.GUARD_BLOCKED, files=(), portal_key=result.portal_key)


def _launch_args(portal: Portal, chromium_sandbox: bool) -> list[str]:
    """Spec 03 §8's flags, with image loading off unless a captcha needs images.

    ``--no-zygote`` is added only when Chromium's own sandbox is off, since Chromium
    refuses to start with both (D168).
    """
    args = list(LAUNCH_ARGS)
    if portal.captcha == "none":
        args.append(IMAGES_OFF)
    if not chromium_sandbox:
        args.append(NO_ZYGOTE)
    return args


def launch_options(portal: Portal, proxy_port: int, chromium_sandbox: bool) -> dict[str, Any]:
    """Chromium's launch keyword arguments, with every connection through the proxy.

    Playwright already adds ``--proxy-bypass-list=<-loopback>`` for a launch proxy, so
    loopback goes through the proxy too; passing that flag again would duplicate it.
    ``chromium_sandbox`` turns Chromium's own sandbox on (D147), which drops
    ``--no-zygote`` from the flags (D168).
    """
    return {
        "headless": True,
        "args": _launch_args(portal, chromium_sandbox),
        "proxy": {"server": f"http://127.0.0.1:{proxy_port}"},
        "chromium_sandbox": chromium_sandbox,
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


def output(result: FetchResult) -> HelperOutput:
    """The helper's output object: the reason and its files, nothing else."""
    return HelperOutput(
        reason=result.reason,
        files=tuple(
            HelperFile(kind=file.kind, path=file.path, size=file.size, sha256=file.sha256)
            for file in result.files
        ),
    )


def emit(message: HelperOutput) -> None:
    """Write the result as the helper's one stdout line."""
    sys.stdout.write(message.model_dump_json() + "\n")
    sys.stdout.flush()
