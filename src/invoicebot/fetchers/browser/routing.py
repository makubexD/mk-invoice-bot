"""Route interception for the browser helper's one context (S7.6, R-5.10, D82).

Every request Chromium makes, redirects included, goes through :class:`RouteGuard`: it
drops resources no portal needs and runs the same URL and address checks as the HTTP
transport (``invoicebot.net.guard``). A refused request is aborted in the browser; only a
refused navigation keeps its reason, so a dead sub-resource cannot turn a not-found page
into ``guard_blocked``.
"""

from __future__ import annotations

import asyncio
import socket
from urllib.parse import urlsplit

from playwright.async_api import Route

from invoicebot.net.guard import GuardReason, Resolver, check_url, resolve_and_check

#: Resource types a portal page never needs: glyphs and audio/video downloads.
BLOCKED_RESOURCE_TYPES = frozenset({"font", "media"})
#: Analytics and tracking hosts blocked outright (spec 03 §8).
ANALYTICS_HOSTS = frozenset(
    {
        "google-analytics.com",
        "googletagmanager.com",
        "doubleclick.net",
        "connect.facebook.net",
        "hotjar.com",
        "clarity.ms",
    }
)
#: Chromium's abort code for a request a client-side rule refused.
BLOCKED_BY_CLIENT = "blockedbyclient"


class RouteGuard:
    """Refuse unsafe or unneeded requests in the helper's context (R-5.10, spec 03 §8).

    Fonts, media, analytics hosts and (unless a captcha needs them) images are aborted
    without recording a reason. Every other request is checked by ``check_url`` and then
    by ``resolve_and_check``; the first refused navigation's reason is kept in
    ``first_block`` for the caller, while a refused sub-resource is aborted unrecorded.
    """

    def __init__(
        self,
        http_hosts: frozenset[str],
        resolver: Resolver,
        *,
        allow_images: bool,
    ) -> None:
        self.first_block: GuardReason | None = None
        self._http_hosts = http_hosts
        self._resolver = resolver
        self._allow_images = allow_images

    async def on_route(self, route: Route) -> None:
        """Abort a refused request or let it continue (R-5.10)."""
        url = route.request.url
        resource_type = route.request.resource_type
        host = _host(url)
        if (
            resource_type in BLOCKED_RESOURCE_TYPES
            or _is_analytics_host(host)
            or (resource_type == "image" and not self._allow_images)
        ):
            await route.abort(BLOCKED_BY_CLIENT)
            return
        reason = check_url(url, self._http_hosts)
        # shortcut: Chromium resolves the host again after this check, so DNS rebinding is
        # closed by the container's egress rule (spec 03 §6), not here; noted for ops.
        if reason is None:
            _, reason = await resolve_and_check(host, self._resolver)
        if reason is None:
            await route.continue_()
            return
        if route.request.is_navigation_request() and self.first_block is None:
            self.first_block = reason
        await route.abort(BLOCKED_BY_CLIENT)


class SystemResolver:
    """Resolve hosts through the operating system's resolver (R-5.10)."""

    async def resolve(self, host: str) -> list[str]:
        """Return the host's unique address strings in answer order; none if it fails."""
        loop = asyncio.get_running_loop()
        try:
            answers = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return []
        addresses: list[str] = []
        for answer in answers:
            address = answer[4][0]
            if isinstance(address, str) and address not in addresses:
                addresses.append(address)
        return addresses


def _host(url: str) -> str:
    """The lower-cased host of ``url``, or an empty string when it cannot be parsed."""
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _is_analytics_host(host: str) -> bool:
    """Whether ``host`` is an analytics host or one of its subdomains."""
    return any(host == blocked or host.endswith(f".{blocked}") for blocked in ANALYTICS_HOSTS)
