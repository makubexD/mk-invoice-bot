"""Integration tests for the browser helper's guard proxy (R-5.10, D82)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import pytest_asyncio
from playwright.async_api import Browser, async_playwright

from invoicebot.config.models import Portal
from invoicebot.fetchers.base import FetchReason, FetchResult
from invoicebot.fetchers.browser.__main__ import (
    _guarded_result,
    _launch_options,
    record_navigation_hosts,
)
from invoicebot.fetchers.browser.proxy import GuardProxy
from invoicebot.net.guard import GuardReason

_HEAD_END = b"\r\n\r\n"
_IDLE_TIMEOUT_S = 30
#: A short idle timeout, so the deadline tests do not wait the real one out.
_SHORT_IDLE_TIMEOUT_S = 0.2
#: How long a test waits for a request that must never be forwarded.
_SETTLE_S = 0.2
_START_HOST = "start.test"
_EVIL_HOST = "evil.test"
_ALLOWED_ADDRESS = "127.0.0.1"
_REFUSED_ADDRESS = "127.0.0.2"
#: How long a page gets to send STUN; without the WebRTC flag packets arrive within a second.
_STUN_WAIT_S = 2


class _FakeResolver:
    """A resolver with fixed answers per host."""

    def __init__(self, answers: dict[str, list[str]]) -> None:
        self._answers = answers

    async def resolve(self, host: str) -> list[str]:
        """Return the canned addresses for ``host``."""
        return self._answers.get(host, [])


class _SlowResolver:
    """A resolver that answers only after a delay, so the lookup deadline can cancel it."""

    def __init__(self, delay_s: float) -> None:
        self._delay_s = delay_s

    async def resolve(self, host: str) -> list[str]:
        """Sleep for the delay, then answer with the allowed address."""
        del host
        await asyncio.sleep(self._delay_s)
        return [_ALLOWED_ADDRESS]


def _allow(_addresses: Iterable[str]) -> None:
    """Allow every address, for the tests that pin their own loopback echo server."""


@pytest_asyncio.fixture
async def echo_port() -> AsyncIterator[int]:
    """A local echo server on 127.0.0.1 that holds connections and echoes their bytes."""
    open_writers: set[asyncio.StreamWriter] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        open_writers.add(writer)
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    return
                writer.write(chunk)
                await writer.drain()
        finally:
            open_writers.discard(writer)
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.close()
        await server.wait_closed()
        for writer in list(open_writers):
            writer.close()


async def _connect(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a client connection to the proxy on 127.0.0.1."""
    return await asyncio.open_connection("127.0.0.1", port)


@dataclass
class _CountedSite:
    """An http origin on loopback that counts the request heads it receives."""

    port: int
    requests: int = 0


@pytest_asyncio.fixture
async def counted_site() -> AsyncIterator[_CountedSite]:
    """An http origin on 127.0.0.1 that ignores ``Connection: close`` and keeps answering.

    Keeping the connection open is what lets a two-way pipe carry a second request to it.
    """
    site = _CountedSite(port=0)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                await reader.readuntil(_HEAD_END)
                site.requests += 1
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: keep-alive\r\n\r\nok")
                await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, _ALLOWED_ADDRESS, 0)
    site.port = server.sockets[0].getsockname()[1]
    try:
        yield site
    finally:
        server.close()
        await server.wait_closed()


async def test_connect_to_loopback_is_refused_as_private() -> None:
    """R-5.10: a CONNECT whose host resolves to loopback is refused and recorded."""
    resolver = _FakeResolver({"loopback.test": ["127.0.0.1"]})
    async with GuardProxy(frozenset(), resolver, idle_timeout_s=_IDLE_TIMEOUT_S) as proxy:
        reader, writer = await _connect(proxy.port)
        try:
            writer.write(b"CONNECT loopback.test:443 HTTP/1.1\r\n\r\n")
            await writer.drain()
            response = await reader.readuntil(_HEAD_END)
        finally:
            writer.close()
            await writer.wait_closed()
    assert response.startswith(b"HTTP/1.1 403")
    assert proxy.blocked == {"loopback.test": GuardReason.PRIVATE_ADDRESS}


async def test_allowed_connect_tunnels_bytes_both_ways(echo_port: int) -> None:
    """R-5.10: an allowed CONNECT tunnels bytes to the checked address and back."""
    resolver = _FakeResolver({"loopback.test": ["127.0.0.1"]})
    async with GuardProxy(
        frozenset(), resolver, idle_timeout_s=_IDLE_TIMEOUT_S, check_addresses=_allow, https_port=echo_port
    ) as proxy:
        reader, writer = await _connect(proxy.port)
        try:
            writer.write(f"CONNECT loopback.test:{echo_port} HTTP/1.1\r\n\r\n".encode())
            await writer.drain()
            response = await reader.readuntil(_HEAD_END)
            assert response.startswith(b"HTTP/1.1 200")
            writer.write(b"ping")
            await writer.drain()
            echoed = await reader.readexactly(4)
        finally:
            writer.close()
            await writer.wait_closed()
    assert echoed == b"ping"


async def test_get_to_unlisted_http_host_is_refused() -> None:
    """R-5.10: a plain GET to a host outside http_hosts is refused."""
    resolver = _FakeResolver({})
    async with GuardProxy(frozenset(), resolver, idle_timeout_s=_IDLE_TIMEOUT_S) as proxy:
        reader, writer = await _connect(proxy.port)
        try:
            writer.write(b"GET http://not-listed.test/x HTTP/1.1\r\nHost: not-listed.test\r\n\r\n")
            await writer.drain()
            response = await reader.readuntil(_HEAD_END)
        finally:
            writer.close()
            await writer.wait_closed()
    assert response.startswith(b"HTTP/1.1 403")


async def test_get_to_listed_http_host_is_forwarded_in_origin_form(echo_port: int) -> None:
    """R-5.10: a GET to a listed http host reaches the checked address in origin form."""
    resolver = _FakeResolver({"listed.test": ["127.0.0.1"]})
    async with GuardProxy(
        frozenset({"listed.test"}), resolver, idle_timeout_s=_IDLE_TIMEOUT_S, check_addresses=_allow
    ) as proxy:
        reader, writer = await _connect(proxy.port)
        try:
            writer.write(
                f"GET http://listed.test:{echo_port}/x?a=1 HTTP/1.1\r\n"
                "Host: listed.test\r\nConnection: keep-alive\r\n\r\n".encode()
            )
            await writer.drain()
            forwarded = await reader.readuntil(_HEAD_END)
        finally:
            writer.close()
            await writer.wait_closed()
    assert forwarded == b"GET /x?a=1 HTTP/1.1\r\nHost: listed.test\r\nConnection: close\r\n\r\n"


async def test_forwarded_get_never_forwards_a_second_request(counted_site: _CountedSite) -> None:
    """R-5.10: after the forwarded GET, a second request on the connection never reaches the upstream."""
    resolver = _FakeResolver({"listed.test": [_ALLOWED_ADDRESS]})
    async with GuardProxy(
        frozenset({"listed.test"}), resolver, idle_timeout_s=_SHORT_IDLE_TIMEOUT_S, check_addresses=_allow
    ) as proxy:
        reader, writer = await _connect(proxy.port)
        try:
            writer.write(
                f"GET http://listed.test:{counted_site.port}/one HTTP/1.1\r\n"
                "Host: listed.test\r\nConnection: keep-alive\r\n\r\n".encode()
            )
            await writer.drain()
            await reader.readuntil(_HEAD_END)
            with suppress(OSError):
                writer.write(
                    f"GET http://listed.test:{counted_site.port}/two HTTP/1.1\r\n"
                    "Host: listed.test\r\nConnection: keep-alive\r\n\r\n".encode()
                )
                await writer.drain()
            await asyncio.sleep(_SETTLE_S)
        finally:
            writer.close()
            await writer.wait_closed()
    assert counted_site.requests == 1


async def test_client_that_sends_nothing_is_closed_by_the_idle_timeout() -> None:
    """R-5.10: a client that sends no request head is closed after the idle timeout."""
    resolver = _FakeResolver({})
    async with GuardProxy(frozenset(), resolver, idle_timeout_s=_SHORT_IDLE_TIMEOUT_S) as proxy:
        reader, writer = await _connect(proxy.port)
        try:
            async with asyncio.timeout(_SHORT_IDLE_TIMEOUT_S * 10):
                received = await reader.read(1)
        finally:
            writer.close()
            await writer.wait_closed()
    assert received == b""


async def test_slow_resolution_is_refused_as_unresolvable() -> None:
    """R-5.10: a DNS lookup that outlasts the idle timeout is refused and recorded."""
    resolver = _SlowResolver(delay_s=_SHORT_IDLE_TIMEOUT_S * 5)
    async with GuardProxy(frozenset(), resolver, idle_timeout_s=_SHORT_IDLE_TIMEOUT_S) as proxy:
        reader, writer = await _connect(proxy.port)
        try:
            writer.write(b"CONNECT slow.test:443 HTTP/1.1\r\n\r\n")
            await writer.drain()
            response = await reader.readuntil(_HEAD_END)
        finally:
            writer.close()
            await writer.wait_closed()
    assert response.startswith(b"HTTP/1.1 403")
    assert proxy.blocked == {"slow.test": GuardReason.UNRESOLVABLE}


async def test_malformed_head_is_bad_request() -> None:
    """R-5.10: a head that is not a request line is refused with 400."""
    resolver = _FakeResolver({})
    async with GuardProxy(frozenset(), resolver, idle_timeout_s=_IDLE_TIMEOUT_S) as proxy:
        reader, writer = await _connect(proxy.port)
        try:
            writer.write(b"garbage\r\n\r\n")
            await writer.drain()
            response = await reader.readuntil(_HEAD_END)
        finally:
            writer.close()
            await writer.wait_closed()
    assert response.startswith(b"HTTP/1.1 400")


async def test_thirty_third_tunnel_is_refused(echo_port: int) -> None:
    """R-5.10: the 33rd open tunnel gets 429 without recording a block."""
    resolver = _FakeResolver({"loopback.test": ["127.0.0.1"]})
    async with GuardProxy(
        frozenset(), resolver, idle_timeout_s=_IDLE_TIMEOUT_S, check_addresses=_allow, https_port=echo_port
    ) as proxy:
        tunnels: list[asyncio.StreamWriter] = []
        try:
            for _ in range(32):
                reader, writer = await _connect(proxy.port)
                tunnels.append(writer)
                writer.write(f"CONNECT loopback.test:{echo_port} HTTP/1.1\r\n\r\n".encode())
                await writer.drain()
                assert (await reader.readuntil(_HEAD_END)).startswith(b"HTTP/1.1 200")
            reader, writer = await _connect(proxy.port)
            tunnels.append(writer)
            writer.write(f"CONNECT loopback.test:{echo_port} HTTP/1.1\r\n\r\n".encode())
            await writer.drain()
            response = await reader.readuntil(_HEAD_END)
        finally:
            for tunnel in tunnels:
                tunnel.close()
            for tunnel in tunnels:
                await tunnel.wait_closed()
    assert response.startswith(b"HTTP/1.1 429")
    assert proxy.blocked == {}


async def test_concurrent_connects_never_exceed_the_slot_cap(echo_port: int) -> None:
    """R-5.10: 40 concurrent CONNECTs reserve slots before opening, so at most 32 get 200."""
    resolver = _FakeResolver({"loopback.test": ["127.0.0.1"]})
    async with GuardProxy(
        frozenset(), resolver, idle_timeout_s=_IDLE_TIMEOUT_S, check_addresses=_allow, https_port=echo_port
    ) as proxy:
        writers: list[asyncio.StreamWriter] = []

        async def connect() -> bytes:
            reader, writer = await _connect(proxy.port)
            writers.append(writer)
            writer.write(f"CONNECT loopback.test:{echo_port} HTTP/1.1\r\n\r\n".encode())
            await writer.drain()
            return await reader.readuntil(_HEAD_END)

        try:
            responses = await asyncio.gather(*(connect() for _ in range(40)))
        finally:
            for writer in writers:
                writer.close()
            for writer in writers:
                await writer.wait_closed()

    established = [response for response in responses if response.startswith(b"HTTP/1.1 200")]
    assert len(established) <= 32


async def test_head_over_16_kib_is_bad_request() -> None:
    """R-5.10: a request head over the 16 KiB cap is refused with 400."""
    resolver = _FakeResolver({})
    oversized = b"GET / HTTP/1.1\r\nX-Filler: " + b"a" * (16 * 1024) + b"\r\n\r\n"
    async with GuardProxy(frozenset(), resolver, idle_timeout_s=_IDLE_TIMEOUT_S) as proxy:
        reader, writer = await _connect(proxy.port)
        try:
            writer.write(oversized)
            await writer.drain()
            response = await reader.readuntil(_HEAD_END)
        finally:
            writer.close()
            await writer.wait_closed()
    assert response.startswith(b"HTTP/1.1 400")


def _refuse_evil(addresses: Iterable[str]) -> GuardReason | None:
    """Allow the start page's loopback and refuse the evil host's second loopback."""
    return GuardReason.PRIVATE_ADDRESS if _REFUSED_ADDRESS in addresses else None


def _portal() -> Portal:
    """A minimal portal; only the launch flags depend on it here."""
    return Portal(key="synthetic", adapter="generic_form")


@dataclass
class _EvilSite:
    """The server on the refused address, counting every connection it receives."""

    port: int
    hits: int = 0


@pytest_asyncio.fixture
async def evil_site() -> AsyncIterator[_EvilSite]:
    """A page server on 127.0.0.2 that must never be reached."""
    site = _EvilSite(port=0)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        site.hits += 1
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, _REFUSED_ADDRESS, 0)
    site.port = server.sockets[0].getsockname()[1]
    try:
        yield site
    finally:
        server.close()
        await server.wait_closed()


@pytest_asyncio.fixture
async def start_site(evil_site: _EvilSite) -> AsyncIterator[int]:
    """A page on 127.0.0.1: ``/`` redirects to evil, ``/script`` loads a script from it, any
    other path opens a WebSocket to it."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(_HEAD_END)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            writer.close()
            await writer.wait_closed()
            return
        path = head.split(b"\r\n", 1)[0].split(b" ")[1]
        if path == b"/":
            response = (
                "HTTP/1.1 302 Found\r\n"
                f"Location: http://{_EVIL_HOST}:{evil_site.port}/\r\n"
                "Content-Length: 0\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
        else:
            if path == b"/script":
                page = f"<script src='http://{_EVIL_HOST}:{evil_site.port}/x.js'></script>"
            else:
                page = f"<script>new WebSocket('ws://{_EVIL_HOST}:{evil_site.port}/');</script>"
            body = page.encode()
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/html\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode() + body
        writer.write(response)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, _ALLOWED_ADDRESS, 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


@pytest_asyncio.fixture
async def browser_proxy() -> AsyncIterator[GuardProxy]:
    """A guard proxy that lets start.test through and refuses evil.test."""
    resolver = _FakeResolver({_START_HOST: [_ALLOWED_ADDRESS], _EVIL_HOST: [_REFUSED_ADDRESS]})
    async with GuardProxy(
        frozenset({_START_HOST, _EVIL_HOST}),
        resolver,
        idle_timeout_s=_IDLE_TIMEOUT_S,
        check_addresses=_refuse_evil,
    ) as proxy:
        yield proxy


@pytest_asyncio.fixture
async def browser(browser_proxy: GuardProxy) -> AsyncIterator[Browser]:
    """A headless Chromium launched with the helper's options through the proxy."""
    async with async_playwright() as playwright:
        launched = await playwright.chromium.launch(**_launch_options(_portal(), browser_proxy.port))
        try:
            yield launched
        finally:
            await launched.close()


async def _wait_until_blocked(proxy: GuardProxy) -> None:
    """Wait up to five seconds for the WebSocket handshake to be refused."""
    for _ in range(100):
        if proxy.blocked:
            return
        await asyncio.sleep(0.05)


async def test_redirect_to_a_refused_address_never_reaches_it(
    browser: Browser, browser_proxy: GuardProxy, start_site: int, evil_site: _EvilSite
) -> None:
    """R-5.10: a 302 to a host the proxy refuses never reaches that host."""
    page = await browser.new_page()
    navigation_hosts = record_navigation_hosts(page)
    try:
        await page.goto(f"http://{_START_HOST}:{start_site}/")
    finally:
        await page.close()

    assert evil_site.hits == 0
    assert navigation_hosts == [_START_HOST, _EVIL_HOST]
    block = browser_proxy.block_for(navigation_hosts)
    assert block is GuardReason.PRIVATE_ADDRESS
    technical = FetchResult(reason=FetchReason.TECHNICAL, files=(), portal_key="synthetic")
    assert _guarded_result(technical, block).reason is FetchReason.GUARD_BLOCKED


async def test_websocket_to_a_refused_address_is_blocked(
    browser: Browser, browser_proxy: GuardProxy, start_site: int, evil_site: _EvilSite
) -> None:
    """R-5.10: a page's WebSocket to a refused address never reaches the host."""
    page = await browser.new_page()
    try:
        await page.goto(f"http://{_START_HOST}:{start_site}/ws")
        await _wait_until_blocked(browser_proxy)
    finally:
        await page.close()

    assert evil_site.hits == 0
    assert _EVIL_HOST in browser_proxy.blocked


async def test_blocked_sub_resource_keeps_a_technical_result(
    browser: Browser, browser_proxy: GuardProxy, start_site: int, evil_site: _EvilSite
) -> None:
    """R-5.10: a refused script on a page that loaded is no blocked navigation; technical stays."""
    page = await browser.new_page()
    navigation_hosts = record_navigation_hosts(page)
    try:
        await page.goto(f"http://{_START_HOST}:{start_site}/script")
        await _wait_until_blocked(browser_proxy)
    finally:
        await page.close()

    assert evil_site.hits == 0
    assert _EVIL_HOST in browser_proxy.blocked
    assert navigation_hosts == [_START_HOST]
    technical = FetchResult(reason=FetchReason.TECHNICAL, files=(), portal_key="synthetic")
    assert _guarded_result(technical, browser_proxy.block_for(navigation_hosts)) is technical


class _UdpCounter(asyncio.DatagramProtocol):
    """Counts the datagrams that reach a local UDP port."""

    def __init__(self) -> None:
        self.hits = 0

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        """Count one datagram."""
        del data, addr
        self.hits += 1


async def test_webrtc_stun_never_leaves_the_browser(browser: Browser) -> None:
    """R-5.10: a page's WebRTC STUN request skips the proxy, so the launch flags must stop it."""
    loop = asyncio.get_running_loop()
    counter = _UdpCounter()
    transport, _ = await loop.create_datagram_endpoint(lambda: counter, local_addr=(_REFUSED_ADDRESS, 0))
    udp_port = transport.get_extra_info("sockname")[1]
    page = await browser.new_page()
    try:
        await page.set_content(
            "<script>const pc = new RTCPeerConnection("
            f"{{iceServers: [{{urls: 'stun:{_REFUSED_ADDRESS}:{udp_port}'}}]}});"
            "pc.createDataChannel('probe');"
            "pc.createOffer().then(offer => pc.setLocalDescription(offer));</script>"
        )
        await asyncio.sleep(_STUN_WAIT_S)
    finally:
        await page.close()
        transport.close()

    assert counter.hits == 0
