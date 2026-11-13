"""Integration tests for the browser helper's guard proxy (R-5.10, D82)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable

import pytest_asyncio

from invoicebot.fetchers.browser.proxy import GuardProxy
from invoicebot.net.guard import GuardReason

_HEAD_END = b"\r\n\r\n"
_IDLE_TIMEOUT_S = 30


class _FakeResolver:
    """A resolver with fixed answers per host."""

    def __init__(self, answers: dict[str, list[str]]) -> None:
        self._answers = answers

    async def resolve(self, host: str) -> list[str]:
        """Return the canned addresses for ``host``."""
        return self._answers.get(host, [])


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
    assert proxy.first_block is GuardReason.PRIVATE_ADDRESS


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
    assert proxy.first_block is None


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
