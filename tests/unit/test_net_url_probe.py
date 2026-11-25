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
from invoicebot.net.url_probe import Probed, first_answering

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
        assert await first_answering(client, [PORTAL]) == Probed(PORTAL, False)


async def test_first_answering_skips_a_url_that_does_not_answer() -> None:
    """R-5.10, D193: a connection error gives no status and the next candidate is tried."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == PORTAL:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [PORTAL, OTHER]) == Probed(OTHER, False)


@pytest.mark.parametrize("status", [403, 404], ids=["forbidden", "not_found"])
async def test_first_answering_does_not_answer_on_an_error_status(status: int) -> None:
    """R-5.10, D192: a 403 or 404 is no answer."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [PORTAL]) is None

    assert requested == [PORTAL]


@pytest.mark.parametrize(
    "status",
    [429, 500, 502, 503],
    ids=["too_many_requests", "server_error", "bad_gateway", "unavailable"],
)
async def test_first_answering_reports_a_busy_status(status: int) -> None:
    """R-5.10, D192: a 429 or a 5xx means the address is busy."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [PORTAL]) == Probed(PORTAL, True)

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
        assert await first_answering(client, [PORTAL]) == Probed(PORTAL, False)


@pytest.mark.parametrize(
    "target",
    [
        "http://other.example.pe/",
        "https://127.0.0.1/",
        "https://e-consulta.sunat.gob.pe/",
        "https://x.example.pe:8443/",
        "https://x.example.pe:99999/",
        "https://x.example.pe:abc/",
    ],
    ids=["plain_http", "loopback", "sunat", "other_port", "port_out_of_range", "port_not_a_number"],
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


async def test_first_answering_follows_five_redirects() -> None:
    """R-5.10, D192: a page reached on the fifth redirect answers with the URL given."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if len(requested) > 5:
            return httpx.Response(200)
        return httpx.Response(302, headers={"location": f"https://portal.example.pe/hop{len(requested)}"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [PORTAL]) == Probed(PORTAL, False)

    assert len(requested) == 6


async def test_first_answering_stops_after_five_redirects() -> None:
    """R-5.10, D192: the first URL and five hops are requested, and a sixth target is not."""
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
        "https://portal.example.pe/hop4",
        "https://portal.example.pe/hop5",
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


async def test_first_answering_reports_a_busy_redirect_hop() -> None:
    """R-5.10, D192: a 5xx on a hop of a redirect means the address is busy."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == PORTAL:
            return httpx.Response(302, headers={"location": "/portal"})
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [PORTAL]) == Probed(PORTAL, True)


@pytest.mark.parametrize(
    "error",
    [
        GuardBlocked(GuardReason.PRIVATE_ADDRESS),
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("read timed out"),
        httpx.RemoteProtocolError("protocol error"),
    ],
    ids=["guard_blocked", "connect_error", "read_timeout", "protocol_error"],
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
        assert await first_answering(client, [PORTAL, OTHER]) == Probed(OTHER, False)


async def test_first_answering_reports_a_dropped_connection_as_busy() -> None:
    """R-5.10, D192: a connection dropped after it opened means the address is busy."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        raise httpx.ReadError("connection dropped")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [PORTAL, OTHER]) == Probed(PORTAL, True)

    assert requested == [PORTAL]


async def test_first_answering_stops_at_a_busy_url() -> None:
    """R-5.10, D192: a busy address stops the probe before a later one that answers."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(503 if str(request.url) == PORTAL else 200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [PORTAL, OTHER]) == Probed(PORTAL, True)

    assert requested == [PORTAL]


async def test_first_answering_reports_the_busy_url_after_a_no_answer() -> None:
    """R-5.10, D193: the first busy URL after a no-status one is reported with its own URL."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if str(request.url) == PORTAL:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(503 if str(request.url) == OTHER else 404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [PORTAL, OTHER]) == Probed(OTHER, True)

    assert requested == [PORTAL, OTHER]


async def test_first_answering_skips_a_look_alike_after_the_read_host_answered() -> None:
    """R-5.10, D193: a status on the read host stops another host, while a same-host URL is tried."""
    read_x = "https://portal.example.pe/x"
    read_y = "https://portal.example.pe/y"
    look_alike = "https://other.example.pe/x"
    requested: list[str] = []
    statuses = {read_x: 404, read_y: 200}

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(statuses[str(request.url)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [read_x, read_y, look_alike]) == Probed(read_y, False)

    assert requested == [read_x, read_y]


async def test_first_answering_skips_a_look_alike_after_the_read_host_answered_an_error() -> None:
    """R-5.10, D193: a 403 on the read host is still a status, so the other host is not tried."""
    read_x = "https://portal.example.pe/x"
    look_alike = "https://other.example.pe/x"
    requested: list[str] = []
    statuses = {read_x: 403, look_alike: 200}

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(statuses[str(request.url)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [read_x, look_alike]) is None

    assert requested == [read_x]


async def test_first_answering_tries_a_look_alike_after_a_connect_error() -> None:
    """R-5.10, D193: a connection error gives no status, so the other host is tried and answers."""
    read_x = "https://portal.example.pe/x"
    look_alike = "https://other.example.pe/x"
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if str(request.url) == read_x:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [read_x, look_alike]) == Probed(look_alike, False)

    assert requested == [read_x, look_alike]


async def test_first_answering_tries_a_look_alike_after_a_guard_block() -> None:
    """R-5.10, D193: a guard refusal gives no status, so the other host is tried and is busy."""
    read_x = "https://portal.example.pe/x"
    look_alike = "https://other.example.pe/x"
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if str(request.url) == read_x:
            raise GuardBlocked(GuardReason.PRIVATE_ADDRESS)
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [read_x, look_alike]) == Probed(look_alike, True)

    assert requested == [read_x, look_alike]


async def test_first_answering_skips_a_look_alike_after_a_refused_redirect() -> None:
    """R-5.10, D193: a redirect status counts, so the other host is not tried though its hop is refused."""
    read_x = "https://portal.example.pe/x"
    look_alike = "https://other.example.pe/x"
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if str(request.url) == read_x:
            return httpx.Response(301, headers={"location": "http://other.example.pe/"})
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [read_x, look_alike]) is None

    assert requested == [read_x]


async def test_first_answering_skips_a_look_alike_after_a_redirect_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-5.10, D193: a redirect status counts even when the next hop hangs, so the other host is not tried."""
    monkeypatch.setattr(url_probe, "PROBE_TIMEOUT_S", 0.05)
    read_x = "https://portal.example.pe/x"
    read_hang = "https://portal.example.pe/hang"
    look_alike = "https://other.example.pe/x"
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if str(request.url) == read_x:
            return httpx.Response(302, headers={"location": "/hang"})
        await asyncio.Event().wait()
        return httpx.Response(200)  # pragma: no cover

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [read_x, look_alike]) is None

    assert requested == [read_x, read_hang]


async def test_first_answering_skips_a_look_alike_after_a_same_host_no_answer() -> None:
    """R-5.10, D193: a 404 on a second URL of the read host still stops a later host."""
    read_x = "https://portal.example.pe/x"
    read_y = "https://portal.example.pe/y"
    look_alike = "https://other.example.pe/x"
    requested: list[str] = []
    statuses = {read_y: 404, look_alike: 200}

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if str(request.url) == read_x:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(statuses[str(request.url)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await first_answering(client, [read_x, read_y, look_alike]) is None

    assert requested == [read_x, read_y]


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
        assert await first_answering(client, [PORTAL, OTHER]) == Probed(OTHER, False)


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
        assert await first_answering(client, [PORTAL]) == Probed(PORTAL, False)

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


@pytest.mark.parametrize(
    ("status", "outcome"),
    [(200, "answered"), (503, "busy"), (404, "none")],
    ids=["answered", "busy", "none"],
)
async def test_first_answering_logs_one_outcome_per_call(
    status: int,
    outcome: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R-5.10, D192: one probe finished record reports the outcome, the count and the reasons."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    with caplog.at_level(logging.INFO, logger="invoicebot.net.url_probe"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await first_answering(client, [PORTAL])

    records = [record for record in caplog.records if record.name == "invoicebot.net.url_probe"]
    assert [record.getMessage() for record in records] == ["probe finished"]
    assert records[0].__dict__["outcome"] == outcome
    assert records[0].__dict__["tried"] == 1
    assert records[0].__dict__["reasons"] == [str(status)]


async def test_first_answering_logs_the_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R-5.10, D192: a probe stopped by its total deadline logs none with TimeoutError."""
    monkeypatch.setattr(url_probe, "PROBE_TOTAL_S", 0.05)

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        return httpx.Response(200)  # pragma: no cover

    with caplog.at_level(logging.INFO, logger="invoicebot.net.url_probe"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await first_answering(client, [PORTAL, OTHER]) is None

    records = [record for record in caplog.records if record.name == "invoicebot.net.url_probe"]
    assert [record.getMessage() for record in records] == ["probe finished"]
    assert records[0].__dict__["outcome"] == "none"
    assert records[0].__dict__["tried"] == 1
    assert records[0].__dict__["reasons"] == ["TimeoutError"]


async def test_first_answering_logs_no_url_or_host(caplog: pytest.LogCaptureFixture) -> None:
    """R-5.10, D192: a refused, an erroring and an answering URL are logged without either."""
    secret = f"cannot reach {BROKEN}"

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == BROKEN:
            raise httpx.ConnectError(secret)
        return httpx.Response(200)

    with caplog.at_level(logging.DEBUG, logger="invoicebot.net.url_probe"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await first_answering(client, [REFUSED, BROKEN, PORTAL]) == Probed(PORTAL, False)

    records = [record for record in caplog.records if record.name == "invoicebot.net.url_probe"]
    assert [record.getMessage() for record in records] == ["probe finished"]
    assert records[0].__dict__["outcome"] == "answered"
    assert records[0].__dict__["tried"] == 3
    assert records[0].__dict__["reasons"] == ["http_not_allowed", "ConnectError", "200"]
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
