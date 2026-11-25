"""Tests for the portal URL probe (R-5.10, D192)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import httpx
import pytest

from invoicebot.net import url_probe
from invoicebot.net.guard import GuardReason, Resolver
from invoicebot.net.http import GuardBlocked, GuardedTransport
from invoicebot.net.url_probe import first_answering

PORTAL = "https://portal.example.pe/"
OTHER = "https://other.example.pe/"
BROKEN = "https://broken.example.pe/"
REFUSED = "http://refused.example.pe/"


class FakeResolver(Resolver):
    """A ``Resolver`` that answers from a fixed list of addresses."""

    def __init__(self, addresses: list[str]) -> None:
        self.addresses = addresses

    async def resolve(self, host: str) -> list[str]:
        """Return the fixed addresses, whatever the host is."""
        return list(self.addresses)


class RaisingStream(httpx.AsyncByteStream):
    """A body that raises when read and records when it is closed."""

    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        raise RuntimeError("body read")
        yield b""  # pragma: no cover

    async def aclose(self) -> None:
        self.closed = True


async def test_first_answering_returns_the_url_that_answers() -> None:
    """R-5.10, D192: a 200 answers and the URL returned is the one given."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [PORTAL]) == PORTAL


async def test_first_answering_skips_a_url_that_does_not_answer() -> None:
    """R-5.10, D192: a 404 is no answer and the next candidate is tried."""

    async def handler(request: httpx.Request) -> httpx.Response:
        status = 404 if str(request.url) == PORTAL else 200
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [PORTAL, OTHER]) == OTHER


@pytest.mark.parametrize("status", [403, 500], ids=["forbidden", "server_error"])
async def test_first_answering_does_not_answer_on_an_error_status(status: int) -> None:
    """R-5.10, D192: a non-2xx status is no answer."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [PORTAL]) is None

    assert requested == [PORTAL]


async def test_first_answering_requests_nothing_for_no_urls() -> None:
    """R-5.10, D192: an empty candidate list answers None without a request."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, []) is None

    assert requested == []


async def test_first_answering_follows_a_public_redirect() -> None:
    """R-5.10, D192: a redirect to another public https URL answers with the URL given."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == PORTAL:
            return httpx.Response(302, headers={"location": OTHER})
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [PORTAL]) == PORTAL


@pytest.mark.parametrize(
    "target",
    [
        "http://other.example.pe/",
        "https://127.0.0.1/",
        "https://e-consulta.sunat.gob.pe/",
        "https://x.example.pe:8443/",
    ],
    ids=["plain_http", "loopback", "sunat", "other_port"],
)
async def test_first_answering_refuses_a_redirect_target(target: str) -> None:
    """R-5.10, D192: a redirect the taught-link checks refuse is no answer and is never requested."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": target})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [PORTAL]) is None

    assert requested == [PORTAL]


async def test_first_answering_stops_after_three_redirects() -> None:
    """R-5.10, D192: the first URL and three hops are requested, and a fourth target is not."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": f"https://portal.example.pe/hop{len(requested)}"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, ["https://portal.example.pe/start"]) is None

    assert requested == [
        "https://portal.example.pe/start",
        "https://portal.example.pe/hop1",
        "https://portal.example.pe/hop2",
        "https://portal.example.pe/hop3",
    ]


async def test_first_answering_does_not_answer_on_a_redirect_without_location() -> None:
    """R-5.10, D192: a 302 without a location is no answer."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [PORTAL]) is None


async def test_first_answering_does_not_answer_on_a_malformed_location() -> None:
    """R-5.10, D192: a location ``urljoin`` cannot parse is no answer."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://[::1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [PORTAL]) is None


@pytest.mark.parametrize(
    "error",
    [
        GuardBlocked(GuardReason.PRIVATE_ADDRESS),
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("read timed out"),
    ],
    ids=["guard_blocked", "connect_error", "read_timeout"],
)
async def test_first_answering_tries_the_next_url_after_a_transport_error(
    error: Exception,
) -> None:
    """R-5.10, D192: a blocked or failed request is no answer and the next URL is tried."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == PORTAL:
            raise error
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [PORTAL, OTHER]) == OTHER


async def test_first_answering_tries_the_next_url_after_a_hanging_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-5.10, D192: a URL past its own deadline is no answer and the next one is tried."""
    monkeypatch.setattr(url_probe, "PROBE_TIMEOUT_S", 0.05)
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == PORTAL:
            await gate.wait()
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [PORTAL, OTHER]) == OTHER


async def test_first_answering_gives_up_at_the_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-5.10, D192: the whole probe stops at its total deadline even before each URL's."""
    monkeypatch.setattr(url_probe, "PROBE_TOTAL_S", 0.05)

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        return httpx.Response(200)  # pragma: no cover

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [PORTAL, OTHER]) is None


async def test_first_answering_does_not_read_the_body() -> None:
    """R-5.10, D192: a body that raises when read still answers, and the response is closed."""
    stream = RaisingStream()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [PORTAL]) == PORTAL

    assert stream.closed


async def test_first_answering_sends_no_cookie_header() -> None:
    """R-5.10, D192: a cookie set by one answer is not sent on the next request."""
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"set-cookie": "session=abc; Path=/"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await first_answering(client, [PORTAL])
        assert client.cookies.get("session") == "abc"
        await first_answering(client, [PORTAL])

    assert len(requests) == 2
    assert "cookie" not in requests[1].headers


async def test_first_answering_logs_no_url_or_host(caplog: pytest.LogCaptureFixture) -> None:
    """R-5.10, D192: a refused, an erroring and an answering URL are logged without either."""
    secret = f"cannot reach {BROKEN}"

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == BROKEN:
            raise httpx.ConnectError(secret)
        return httpx.Response(200)

    with caplog.at_level(logging.DEBUG, logger="invoicebot.net.url_probe"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await first_answering(client, [REFUSED, BROKEN, PORTAL]) == PORTAL

    records = [record for record in caplog.records if record.name == "invoicebot.net.url_probe"]
    assert any(record.__dict__.get("reason") == "http_not_allowed" for record in records)
    assert any(record.__dict__.get("error") == "ConnectError" for record in records)
    assert any(record.__dict__.get("status") == 200 for record in records)
    logged = repr([record.__dict__ for record in records])
    for value in (
        REFUSED,
        BROKEN,
        PORTAL,
        "refused.example.pe",
        "broken.example.pe",
        "portal.example.pe",
        secret,
    ):
        assert value not in logged


async def test_first_answering_blocks_a_private_address() -> None:
    """R-5.10, D192: over the guarded transport a private answer is no answer, sent nowhere."""
    received: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200)

    transport = GuardedTransport(httpx.MockTransport(handler), FakeResolver(["10.0.0.1"]))
    async with httpx.AsyncClient(transport=transport) as client:
        assert await first_answering(client, [PORTAL]) is None

    assert received == []
