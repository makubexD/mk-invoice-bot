"""Tests for the guarded httpx transport (R-5.10)."""

from __future__ import annotations

import httpx
import pytest

from invoicebot.net.guard import GuardReason, Resolver
from invoicebot.net.http import GuardBlocked, GuardedTransport

PUBLIC_ADDRESS = "93.184.216.34"
IPV6_ADDRESS = "2606:2800:220:1:248:1893:25c8:1946"


class FakeResolver(Resolver):
    """A ``Resolver`` that answers from a fixed list of addresses."""

    def __init__(self, addresses: list[str]) -> None:
        self.addresses = addresses

    async def resolve(self, host: str) -> list[str]:
        """Return the fixed addresses, whatever the host is."""
        return list(self.addresses)


def make_transport(addresses: list[str]) -> tuple[GuardedTransport, list[httpx.Request]]:
    """Build a guarded transport over a ``MockTransport`` that records what it receives."""
    captured: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    inner = httpx.MockTransport(handle)
    return GuardedTransport(inner, FakeResolver(addresses)), captured


async def test_https_pins_the_address_and_keeps_the_hostname() -> None:
    """R-5.10: a public host goes out to its IP with the original Host and SNI hostname."""
    transport, captured = make_transport([PUBLIC_ADDRESS])

    async with httpx.AsyncClient(transport=transport) as client:
        await client.get("https://portal.example/invoice")

    assert len(captured) == 1
    outgoing = captured[0]
    assert outgoing.url.host == PUBLIC_ADDRESS
    assert str(outgoing.url) == f"https://{PUBLIC_ADDRESS}/invoice"
    assert outgoing.headers["host"] == "portal.example"
    assert outgoing.extensions["sni_hostname"] == "portal.example"


async def test_http_url_is_refused() -> None:
    """R-5.10: a plain http URL is refused as http_not_allowed before any request."""
    transport, captured = make_transport([PUBLIC_ADDRESS])

    with pytest.raises(GuardBlocked) as exc_info:
        await transport.handle_async_request(httpx.Request("GET", "http://portal.example/"))

    assert exc_info.value.reason is GuardReason.HTTP_NOT_ALLOWED
    assert captured == []


async def test_file_url_is_refused() -> None:
    """R-5.10: a file URL is refused as scheme_blocked."""
    transport, captured = make_transport([PUBLIC_ADDRESS])

    with pytest.raises(GuardBlocked) as exc_info:
        await transport.handle_async_request(httpx.Request("GET", "file:///etc/passwd"))

    assert exc_info.value.reason is GuardReason.SCHEME_BLOCKED
    assert captured == []


async def test_private_address_is_refused() -> None:
    """R-5.10: a host resolving to a private address is refused as private_address."""
    transport, captured = make_transport(["10.0.0.1"])

    with pytest.raises(GuardBlocked) as exc_info:
        await transport.handle_async_request(httpx.Request("GET", "https://portal.example/"))

    assert exc_info.value.reason is GuardReason.PRIVATE_ADDRESS
    assert captured == []


async def test_rebinding_answer_is_refused() -> None:
    """R-5.10: one public and one private answer is refused (DNS rebinding)."""
    transport, captured = make_transport([PUBLIC_ADDRESS, "10.0.0.1"])

    with pytest.raises(GuardBlocked) as exc_info:
        await transport.handle_async_request(httpx.Request("GET", "https://portal.example/"))

    assert exc_info.value.reason is GuardReason.PRIVATE_ADDRESS
    assert captured == []


async def test_unresolvable_host_is_refused() -> None:
    """R-5.10: a host with no addresses is refused as unresolvable."""
    transport, captured = make_transport([])

    with pytest.raises(GuardBlocked) as exc_info:
        await transport.handle_async_request(httpx.Request("GET", "https://portal.example/"))

    assert exc_info.value.reason is GuardReason.UNRESOLVABLE
    assert captured == []


async def test_ipv6_address_is_bracketed_in_the_url() -> None:
    """R-5.10: an IPv6 address is pinned in bracketed form in the outgoing URL."""
    transport, captured = make_transport([IPV6_ADDRESS])

    async with httpx.AsyncClient(transport=transport) as client:
        await client.get("https://portal.example/")

    assert len(captured) == 1
    assert f"[{IPV6_ADDRESS}]" in str(captured[0].url)
    assert captured[0].url.host == IPV6_ADDRESS
