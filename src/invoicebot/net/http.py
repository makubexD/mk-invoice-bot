"""An ``httpx`` transport that applies the R-5.10 guard and pins the checked IP.

Every request passes through :class:`GuardedTransport`, which refuses non-https URLs,
resolves the host and checks every answer, then rewrites the request so the connection goes
to the checked address while TLS still verifies the original hostname (``sni_hostname``) and
the ``Host`` header keeps the original authority. Clients must not follow redirects
themselves; each hop is re-sent so it passes this guard again.
"""

from __future__ import annotations

import httpx

from invoicebot.net.guard import (
    CheckAddresses,
    GuardReason,
    Resolver,
    check_addresses,
    check_url,
    resolve_and_check,
)


class GuardBlocked(Exception):
    """A request the R-5.10 guard refused, carrying the reason."""

    def __init__(self, reason: GuardReason) -> None:
        self.reason = reason
        super().__init__(f"request blocked: {reason.value}")


class GuardedTransport(httpx.AsyncBaseTransport):
    """Refuse, resolve, check and pin every request before handing it to ``inner``.

    ``inner`` receives a copy of the request whose URL host is the checked address, so the
    connection goes to the checked IP only. TLS still verifies the original hostname through
    the ``sni_hostname`` extension and the ``Host`` header keeps the original authority.
    Clients must use ``follow_redirects=False`` and re-send each hop, so every hop is checked
    again (R-5.10, D82).
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        resolver: Resolver,
        check: CheckAddresses = check_addresses,
    ) -> None:
        self._inner = inner
        self._resolver = resolver
        self._check = check

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Refuse or pin ``request``, then forward it to ``inner`` (R-5.10)."""
        reason = check_url(str(request.url), frozenset())
        if reason is not None:
            raise GuardBlocked(reason)
        # The IDNA (ASCII) form: what DNS and the TLS SNI field expect.
        host = request.url.raw_host.decode("ascii")
        if not host:
            raise GuardBlocked(GuardReason.BAD_URL)
        address, reason = await resolve_and_check(host, self._resolver, self._check)
        if reason is not None:
            raise GuardBlocked(reason)
        headers = request.headers.copy()
        headers["Host"] = request.url.netloc.decode("ascii")
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=address),
            headers=headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": host},
        )
        return await self._inner.handle_async_request(pinned)

    async def aclose(self) -> None:
        """Close ``inner``."""
        await self._inner.aclose()
