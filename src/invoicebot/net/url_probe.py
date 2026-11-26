"""Which of a few guessed portal addresses answers or is busy (R-5.10, D192, D193).

The caller passes a client whose transport is :class:`invoicebot.net.http.GuardedTransport`, so
every request is refused or pinned by the shared guard. A redirect is followed one checked hop at
a time; only the status decides, and no response body is ever read. Once the host the strip was
read on gives any status, the candidates on other hosts are not tried, so a look-alike site that
answers is never offered in its place (D193).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urljoin, urlsplit

import httpx

from invoicebot.net.http import GuardBlocked
from invoicebot.net.taught_url import taught_url_refusal

logger = logging.getLogger(__name__)

#: Longest one candidate may take, its redirect hops included (D192).
PROBE_TIMEOUT_S = 8.0
#: Longest the whole probe may take, every candidate included (D192).
PROBE_TOTAL_S = 20.0
#: Redirect hops followed from one candidate before it counts as no answer (D192).
MAX_PROBE_REDIRECTS = 5
#: Redirect statuses whose ``location`` is followed; any other 3xx is no answer (D192).
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
#: Statuses that mean the server exists but cannot serve now: busy (D192).
_BUSY_STATUSES = frozenset({429, *range(500, 600)})
#: The probe asks for an HTML page and refuses a compressed body it never reads.
_PROBE_HEADERS = {"Accept": "text/html", "Accept-Encoding": "identity"}
#: Failures that mean "no answer" rather than an error the caller must handle (D192).
_NO_ANSWER_ERRORS = (GuardBlocked, httpx.HTTPError, httpx.InvalidURL, ValueError)


@dataclass(frozen=True)
class Probed:
    """The first candidate that answered or was busy, and which of the two (R-5.10, D192)."""

    url: str
    busy: bool


class _Outcome(StrEnum):
    """How one candidate ended, as the log line names it (D192)."""

    ANSWERED = "answered"
    BUSY = "busy"
    NONE = "none"


async def first_answering(http: httpx.AsyncClient, urls: Sequence[str]) -> Probed | None:
    """Return the first of ``urls`` that answers or is busy, or None (R-5.10, D192, D193).

    Candidates are tried one at a time, each within ``PROBE_TIMEOUT_S``; the whole probe stops
    at ``PROBE_TOTAL_S``. The URL returned is the one given, not where it redirects. One INFO
    record reports the outcome and a reason for every candidate started. The first URL's host
    is the host as read: once any URL of that host gives an HTTP status (a redirect included),
    later candidates on another host are skipped, with no reason and no request (D193).
    """
    reasons: list[str] = []
    read_host = _host(urls[0]) if urls else None
    read_host_answered = False
    try:
        async with asyncio.timeout(PROBE_TOTAL_S):
            for url in urls:
                if read_host_answered and _host(url) != read_host:
                    continue
                statuses: list[int] = []
                outcome, reason = await _probe(http, url, statuses)
                reasons.append(reason)
                read_host_answered = read_host_answered or (_host(url) == read_host and bool(statuses))
                if outcome is not _Outcome.NONE:
                    _log_probe(outcome, reasons)
                    return Probed(url, busy=outcome is _Outcome.BUSY)
    except TimeoutError:
        reasons.append("TimeoutError")
    _log_probe(_Outcome.NONE, reasons)
    return None


def _host(url: str) -> str | None:
    """Return the host ``url`` names, for an exact comparison (D193)."""
    return urlsplit(url).hostname


async def _probe(http: httpx.AsyncClient, url: str, statuses: list[int]) -> tuple[_Outcome, str]:
    """Return how ``url`` ended within its own deadline, and why (R-5.10, D192, D193).

    Every status that came back, a redirect's included, is appended to ``statuses`` before
    ``_follow`` could fail, so a timeout past it still counts the host as answered (D193).
    """
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_S):
            return await _follow(http, url, statuses)
    except TimeoutError:
        return _Outcome.NONE, "TimeoutError"


async def _follow(http: httpx.AsyncClient, url: str, statuses: list[int]) -> tuple[_Outcome, str]:
    """Return how ``url`` or the last hop it redirects to ended, and why (R-5.10, D192, D193)."""
    current = url
    redirects = 0
    while True:
        refusal = taught_url_refusal(current)
        if refusal is not None:
            return _Outcome.NONE, refusal.value
        try:
            response = await _send(http, current)
        except httpx.ReadError as err:
            return _Outcome.BUSY, type(err).__name__
        except _NO_ANSWER_ERRORS as err:
            return _Outcome.NONE, type(err).__name__
        statuses.append(response.status_code)
        await response.aclose()
        location = _redirect_location(response)
        if location is None:
            return _status_outcome(response.status_code), str(response.status_code)
        if redirects >= MAX_PROBE_REDIRECTS:
            return _Outcome.NONE, "redirects"
        try:
            current = urljoin(current, location)
        except ValueError as err:
            return _Outcome.NONE, type(err).__name__
        redirects += 1


def _status_outcome(status: int) -> _Outcome:
    """Return what a final status means for its candidate (D192)."""
    if httpx.codes.is_success(status):
        return _Outcome.ANSWERED
    return _Outcome.BUSY if status in _BUSY_STATUSES else _Outcome.NONE


def _log_probe(outcome: _Outcome, reasons: Sequence[str]) -> None:
    """Log the one line a probe emits, with no URL, host or location in it (R-5.10, D192)."""
    logger.info("probe finished", extra={"outcome": outcome.value, "tried": len(reasons), "reasons": reasons})


async def _send(http: httpx.AsyncClient, url: str) -> httpx.Response:
    """Make one un-followed GET with the client's cookies removed and the body unread (D192)."""
    request = http.build_request("GET", url, headers=_PROBE_HEADERS)
    request.headers.pop("cookie", None)
    return await http.send(request, stream=True, follow_redirects=False)


def _redirect_location(response: httpx.Response) -> str | None:
    """Return the location a followed redirect points at, or None (D192)."""
    if response.status_code not in _REDIRECT_STATUSES:
        return None
    return response.headers.get("location") or None
