"""Decide which QR document URLs the bot may request, and fetch them (R-5.2, R-5.6, R-5.10).

A scanned QR code points at a document on SUNAT or on a configured portal. Only
``https`` URLs on those hosts are fetched; everything else is refused before a
request is made. :class:`QrUrlFetcher` follows redirects one checked hop at a time
and writes the invoice's files by kind.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlsplit

import httpx

from invoicebot.config.models import MediaMaxBytes, StoreDirectory
from invoicebot.fetchers.base import (
    ASCII_WHITESPACE,
    UTF8_BOM,
    FetchedFile,
    FetchInvoice,
    FetchReason,
    FetchResult,
    is_pdf,
)
from invoicebot.fetchers.portal import sha256_hex, xml_reason
from invoicebot.net.guard import GuardReason
from invoicebot.net.http import GuardBlocked

SUNAT_DOMAIN = "sunat.gob.pe"

#: The name this fetcher uses in a flow's ``fetchers`` list (R-5.2).
QR_URL_FETCHER = "qr_url"
#: Redirect hops followed before the URL counts as a loop and is refused (R-5.10).
MAX_REDIRECTS = 3
#: The first bytes that make a ``<`` document an HTML page rather than an XML (R-5.4).
_HTML_PREFIXES = (b"<!doctype html", b"<html")

_DocumentKind = Literal["pdf", "xml", "html"]

#: The result of a download that ran into its read or overall deadline (R-5.1).
_TIMEOUT_RESULT = FetchResult(FetchReason.TIMEOUT, (), None)


def allowed_hosts(directory: StoreDirectory) -> frozenset[str]:
    """Return the lowercased hosts of every portal that has a URL (R-5.2)."""
    hosts: set[str] = set()
    for portal in directory.portals.values():
        if portal.url is None:
            continue
        host = urlsplit(portal.url).hostname
        if host is not None:
            hosts.add(host.lower())
    return frozenset(hosts)


def is_allowed(url: str, hosts: frozenset[str]) -> bool:
    """Return whether ``url`` is an https URL on SUNAT or an allowed host (R-5.2, R-5.10)."""
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        return False
    if parts.scheme != "https":
        return False
    if parts.username is not None or parts.password is not None:
        return False
    host = parts.hostname
    if host is None:
        return False
    host = host.lower()
    return host == SUNAT_DOMAIN or host.endswith("." + SUNAT_DOMAIN) or host in hosts


@dataclass(frozen=True)
class _Reply:
    """One HTTP hop: its status, its redirect target and, for a 200, its body."""

    status: int
    location: str | None
    body: bytes


class _TooManyRedirects(Exception):
    """The redirect chain is longer than ``MAX_REDIRECTS`` (R-5.10)."""


class _BodyTooLarge(Exception):
    """The body outgrew the larger of the PDF and XML caps (R-5.6)."""


class QrUrlFetcher:
    """Fetch an invoice's files from the URL its QR code carried (R-5.2, R-5.6)."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        hosts: frozenset[str],
        max_bytes: MediaMaxBytes,
        timeout_s: float,
    ) -> None:
        """Keep the client, the allowed hosts, the body caps and the fetch deadline (R-5.1)."""
        self._http = http
        self._hosts = hosts
        self._max_bytes = max_bytes
        self._timeout_s = timeout_s

    async def fetch(self, invoice: FetchInvoice, out_dir: Path) -> FetchResult:
        """Fetch ``invoice``'s document from its QR URL and report why (R-5.1, R-5.2, R-5.6)."""
        url = invoice.document_url
        if url is None:
            return FetchResult(FetchReason.NO_URL, (), None)
        try:
            async with asyncio.timeout(self._timeout_s):
                reply = await self._follow(url)
        except (GuardBlocked, _TooManyRedirects):
            return FetchResult(FetchReason.GUARD_BLOCKED, (), None)
        except (TimeoutError, httpx.TimeoutException):
            return _TIMEOUT_RESULT
        except (httpx.HTTPError, _BodyTooLarge):
            return FetchResult(FetchReason.TECHNICAL, (), None)
        if reply.status != httpx.codes.OK:
            return FetchResult(FetchReason.TECHNICAL, (), None)
        return _store(reply.body, out_dir, invoice, self._max_bytes)

    async def _follow(self, url: str) -> _Reply:
        """Return the final hop, following at most ``MAX_REDIRECTS`` checked redirects (R-5.10)."""
        current = url
        redirects = 0
        while True:
            if not is_allowed(current, self._hosts):
                raise GuardBlocked(GuardReason.BAD_URL)
            reply = await self._send(current)
            if not _is_redirect(reply.status) or reply.location is None:
                return reply
            if redirects >= MAX_REDIRECTS:
                raise _TooManyRedirects
            current = urljoin(current, reply.location)
            redirects += 1

    async def _send(self, url: str) -> _Reply:
        """Make one un-followed GET, reading a 200's body under the larger cap (R-5.6)."""
        async with self._http.stream("GET", url, follow_redirects=False) as response:
            if response.status_code != httpx.codes.OK:
                return _Reply(response.status_code, response.headers.get("location"), b"")
            return _Reply(response.status_code, None, await self._read_body(response))

    async def _read_body(self, response: httpx.Response) -> bytes:
        """Read the body into memory, refusing it once past the larger kind cap (R-5.6)."""
        cap = max(self._max_bytes.pdf, self._max_bytes.xml)
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > cap:
                raise _BodyTooLarge
            chunks.append(chunk)
        return b"".join(chunks)


def _is_redirect(status: int) -> bool:
    """Return whether ``status`` is a redirect the fetcher follows (R-5.10)."""
    return 300 <= status < 400


def _classify(data: bytes) -> _DocumentKind | None:
    """Return what the body is, judged by its first bytes (R-5.6)."""
    if is_pdf(data):
        return "pdf"
    body = data.removeprefix(UTF8_BOM).lstrip(ASCII_WHITESPACE)
    if body.startswith(b"<"):
        # HTML pages are a not-found answer (R-5.4).
        if body[:64].lower().startswith(_HTML_PREFIXES):
            return "html"
        return "xml"
    return None


def _store(data: bytes, out_dir: Path, invoice: FetchInvoice, max_bytes: MediaMaxBytes) -> FetchResult:
    """Write the body under its kind and return it, or why it is unusable (R-5.6)."""
    kind = _classify(data)
    if kind is None or kind == "html":
        return FetchResult(FetchReason.NOT_FOUND, (), None)
    cap = max_bytes.pdf if kind == "pdf" else max_bytes.xml
    if len(data) > cap:
        return FetchResult(FetchReason.TECHNICAL, (), None)
    path = out_dir / f"{kind}.{kind}"
    path.write_bytes(data)
    digest = sha256_hex(path)
    if kind == "xml":
        reason = xml_reason(path, invoice)
        if reason is not None:
            path.unlink(missing_ok=True)
            return FetchResult(reason, (), None)
    return FetchResult(FetchReason.OK, (FetchedFile(kind, path, len(data), digest),), None)
