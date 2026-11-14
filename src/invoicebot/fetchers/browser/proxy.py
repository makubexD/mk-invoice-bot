"""The helper's guard proxy: every browser connection is checked here (R-5.10, D82).

Chromium follows a redirect itself, without running ``context.route`` for the hops after
the first, so a page (or anything it loads) can bounce to loopback, Postgres or the cloud
metadata service and be fetched unchecked. The helper therefore launches Chromium through
this proxy, which checks the host of every ``CONNECT`` and every absolute-form ``GET`` with
``invoicebot.net.guard`` and opens the connection to the address it checked, pinning it
against DNS rebinding (D82).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from types import TracebackType
from urllib.parse import SplitResult, urlsplit

from invoicebot.net.guard import (
    CheckAddresses,
    GuardReason,
    Resolver,
    check_addresses,
    check_url,
    resolve_and_check,
)

logger = logging.getLogger(__name__)

#: The most request-head bytes accepted before the request is refused (R-9.2).
_HEAD_LIMIT = 16 * 1024
#: The byte sequence that ends a request head.
_HEAD_END = b"\r\n\r\n"
#: The most tunnels/forwards open at once; the next one gets 429 (R-5.10).
_MAX_OPEN = 32
#: The most refused hosts remembered; a page cannot grow the map without bound (R-9.1).
_MAX_BLOCKED_HOSTS = 64
#: Read size for the two-way pipe; the server's reader limit equals the head limit.
_PIPE_CHUNK = _HEAD_LIMIT
_LOOPBACK = "127.0.0.1"
_HTTP_400 = b"HTTP/1.1 400 Bad Request\r\n\r\n"
_HTTP_403 = b"HTTP/1.1 403 Forbidden\r\n\r\n"
_HTTP_429 = b"HTTP/1.1 429 Too Many Requests\r\n\r\n"
_HTTP_502 = b"HTTP/1.1 502 Bad Gateway\r\n\r\n"
_ESTABLISHED = b"HTTP/1.1 200 Connection Established\r\n\r\n"
_CLOSE_HEADER = b"Connection: close\r\n"


class GuardProxy:
    """An asyncio HTTP proxy that checks every hop and pins the checked address (R-5.10).

    ``CONNECT`` (HTTPS and ``wss``) is allowed only to ``https_port`` after the host's
    addresses pass ``check_addresses``; a plain ``GET`` in absolute ``http://`` form must
    first pass ``check_url`` against ``http_hosts`` and then the same address check. The
    upstream connection is opened to ``addresses[0]``, never to the host name, so a
    rebinding answer cannot redirect a checked request elsewhere (D82). Each refused host's
    first reason is kept in ``blocked``, so the helper can tell a blocked navigation from a
    blocked sub-resource (``block_for``); the 33rd open tunnel gets ``429`` without being
    recorded.
    """

    def __init__(
        self,
        http_hosts: frozenset[str],
        resolver: Resolver,
        *,
        idle_timeout_s: float,
        check_addresses: CheckAddresses = check_addresses,
        https_port: int = 443,
    ) -> None:
        self._http_hosts = http_hosts
        self._resolver = resolver
        self._idle_timeout_s = idle_timeout_s
        self._check_addresses = check_addresses
        self._https_port = https_port
        self.port = 0
        self.blocked: dict[str, GuardReason] = {}
        self._server: asyncio.Server | None = None
        self._open: set[asyncio.StreamWriter] = set()
        self._opening = 0
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        """Bind the proxy on 127.0.0.1 port 0 and record the chosen port."""
        self._server = await asyncio.start_server(self._on_connection, _LOOPBACK, 0, limit=_HEAD_LIMIT)
        self.port = self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        """Stop accepting, cancel every connection and close any tunnel left open."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await _cancel_and_await(list(self._tasks))
        self._tasks.clear()
        for writer in list(self._open):
            writer.close()
        self._open.clear()

    async def __aenter__(self) -> GuardProxy:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Track and run one client connection without blocking the server's accept loop."""
        task = asyncio.create_task(self._handle_connection(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Read one request head and dispatch it, closing the client in every case."""
        try:
            try:
                async with asyncio.timeout(self._idle_timeout_s):
                    head = await reader.readuntil(_HEAD_END)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                head = None
            if head is None:
                await _write(writer, _HTTP_400)
                return
            request = _parse_head(head)
            if request is None:
                await _write(writer, _HTTP_400)
                return
            await self._dispatch(reader, writer, request)
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError) as error:
            logger.debug("proxy connection failed: %s", type(error).__name__)
        finally:
            writer.close()

    async def _dispatch(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        request: tuple[bytes, bytes, bytes, bytes],
    ) -> None:
        """Serve the one allowed request kinds; anything else is malformed."""
        method, target, version, header_block = request
        if method == b"CONNECT":
            await self._connect(reader, writer, target)
        elif method == b"GET":
            await self._forward_get(reader, writer, target, version, header_block)
        else:
            await _write(writer, _HTTP_400)

    async def _connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target: bytes
    ) -> None:
        """Serve a CONNECT: check the host, then tunnel to the pinned address on https_port."""
        parsed = _parse_connect_target(target)
        if parsed is None:
            await self._refuse(writer, GuardReason.BAD_URL, None)
            return
        host, port = parsed
        if port != self._https_port:
            await self._refuse(writer, GuardReason.BAD_URL, host)
            return
        pinned, reason = await self._resolve_pin(host)
        if reason is not None:
            await self._refuse(writer, reason, host)
            return
        upstream = await self._open_upstream(writer, pinned, self._https_port)
        if upstream is None:
            return
        upstream_reader, upstream_writer = upstream
        try:
            await _write(writer, _ESTABLISHED)
            await _pipe(reader, writer, upstream_reader, upstream_writer, self._idle_timeout_s)
        finally:
            self._open.discard(upstream_writer)
            upstream_writer.close()

    async def _forward_get(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        target: bytes,
        version: bytes,
        header_block: bytes,
    ) -> None:
        """Serve one absolute-form GET: check the URL, forward it once and relay the answer.

        Only the upstream's answer is relayed: a second request written on the same client
        connection ends it instead of reaching the pinned upstream unchecked.
        """
        try:
            url = target.decode("ascii")
        except UnicodeDecodeError:
            await _write(writer, _HTTP_400)
            return
        parsed = _absolute_http_url(url)
        if parsed is None:
            await _write(writer, _HTTP_400)
            return
        host, port, origin = parsed
        reason = check_url(url, self._http_hosts)
        if reason is not None:
            await self._refuse(writer, reason, host)
            return
        pinned, reason = await self._resolve_pin(host)
        if reason is not None:
            await self._refuse(writer, reason, host)
            return
        upstream = await self._open_upstream(writer, pinned, port)
        if upstream is None:
            return
        upstream_reader, upstream_writer = upstream
        try:
            upstream_writer.write(_forward_request(version, origin, header_block))
            await upstream_writer.drain()
            await _relay_response(reader, writer, upstream_reader, self._idle_timeout_s)
        finally:
            self._open.discard(upstream_writer)
            upstream_writer.close()

    async def _open_upstream(
        self, writer: asyncio.StreamWriter, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        """Open the pinned upstream, answering 429 when full and 502 when it cannot connect.

        The slot is reserved before the awaited open, so open plus opening never exceeds
        ``_MAX_OPEN`` even when requests arrive together.
        """
        if len(self._open) + self._opening >= _MAX_OPEN:
            await _write(writer, _HTTP_429)
            return None
        self._opening += 1
        try:
            async with asyncio.timeout(self._idle_timeout_s):
                upstream = await asyncio.open_connection(host, port)
        except (OSError, TimeoutError):
            upstream = None
        finally:
            self._opening -= 1
        if upstream is None:
            await _write(writer, _HTTP_502)
            return None
        upstream_reader, upstream_writer = upstream
        self._open.add(upstream_writer)
        return upstream_reader, upstream_writer

    async def _resolve_pin(self, host: str) -> tuple[str, GuardReason | None]:
        """Resolve ``host`` once and return the address to pin, or the reason to refuse.

        A lookup that outlasts the idle timeout is refused as unresolvable.
        """
        try:
            async with asyncio.timeout(self._idle_timeout_s):
                return await resolve_and_check(host, self._resolver, self._check_addresses)
        except TimeoutError:
            return "", GuardReason.UNRESOLVABLE

    async def _refuse(self, writer: asyncio.StreamWriter, reason: GuardReason, host: str | None) -> None:
        """Answer 403 and keep the host's first refusal reason, up to ``_MAX_BLOCKED_HOSTS``."""
        if host is not None and host not in self.blocked and len(self.blocked) < _MAX_BLOCKED_HOSTS:
            self.blocked[host] = reason
        await _write(writer, _HTTP_403)

    def block_for(self, hosts: Iterable[str]) -> GuardReason | None:
        """Return the refusal of the first of ``hosts`` the proxy refused, or None (R-5.10)."""
        for host in hosts:
            reason = self.blocked.get(host)
            if reason is not None:
                return reason
        return None


def _parse_head(head: bytes) -> tuple[bytes, bytes, bytes, bytes] | None:
    """Split a request head into (method, target, version, header block), or None."""
    line, separator, rest = head.partition(b"\r\n")
    if not separator:
        return None
    parts = line.split()
    if len(parts) != 3:
        return None
    method, target, version = parts
    if not method or not version.startswith(b"HTTP/"):
        return None
    if not rest.endswith(b"\r\n"):
        return None
    return method, target, version, rest[:-2]


def _parse_connect_target(target: bytes) -> tuple[str, int] | None:
    """Split a CONNECT target ``host:port`` into (host, port), or None when malformed."""
    try:
        parts = urlsplit("//" + target.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None
    host = parts.hostname
    if host is None:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None:
        return None
    return host, port


def _absolute_http_url(url: str) -> tuple[str, int, str] | None:
    """Parse an absolute ``http://`` URL into (host, port, origin-form), or None."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = parts.hostname
    if parts.scheme.lower() != "http" or host is None:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    return host, port or 80, _origin_form(parts)


def _origin_form(parts: SplitResult) -> str:
    """The origin-form of a URL: its path and query, without scheme, host or fragment."""
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def _forward_request(version: bytes, origin: str, header_block: bytes) -> bytes:
    """Rewrite a GET to origin-form, dropping proxy Connection headers and forcing close."""
    lines = [b"GET " + origin.encode("ascii") + b" " + version + b"\r\n"]
    for line in header_block.split(b"\r\n"):
        if not line or line.lower().startswith(b"connection:"):
            continue
        lines.append(line + b"\r\n")
    lines.append(_CLOSE_HEADER)
    lines.append(b"\r\n")
    return b"".join(lines)


async def _write(writer: asyncio.StreamWriter, response: bytes) -> None:
    """Write one prebuilt response and flush it."""
    writer.write(response)
    await writer.drain()


async def _pump(source: asyncio.StreamReader, sink: asyncio.StreamWriter, idle_timeout_s: float) -> None:
    """Copy ``source`` into ``sink`` until it closes or a read idles out."""
    while True:
        async with asyncio.timeout(idle_timeout_s):
            chunk = await source.read(_PIPE_CHUNK)
        if not chunk:
            return
        sink.write(chunk)
        await sink.drain()


async def _relay_response(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    idle_timeout_s: float,
) -> None:
    """Copy the upstream's answer to the client, one way, until either side ends.

    The client's bytes are never forwarded: anything it sends after the first request (a
    second request on a kept-alive connection) or its close ends the relay (R-5.10).
    """
    tasks = [
        asyncio.create_task(_pump(upstream_reader, client_writer, idle_timeout_s)),
        asyncio.create_task(_until_readable(client_reader)),
    ]
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        await _cancel_and_await(tasks)


async def _until_readable(reader: asyncio.StreamReader) -> None:
    """Return once ``reader`` has a byte or reaches its end; the byte is discarded."""
    await reader.read(1)


async def _pipe(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
    idle_timeout_s: float,
) -> None:
    """Pipe bytes both ways until one side closes or an idle read times out."""
    pumps = [
        asyncio.create_task(_pump(client_reader, upstream_writer, idle_timeout_s)),
        asyncio.create_task(_pump(upstream_reader, client_writer, idle_timeout_s)),
    ]
    try:
        done, _pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        await _cancel_and_await(pumps)


async def _cancel_and_await(tasks: list[asyncio.Task[None]]) -> None:
    """Cancel ``tasks`` and wait for them, so none is left unretrieved."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
