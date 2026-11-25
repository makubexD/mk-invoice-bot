"""Which of a few guessed portal addresses answers, reading no body (R-5.10, D192).

The caller passes a client whose transport is :class:`invoicebot.net.http.GuardedTransport`, so
every request is refused or pinned by the shared guard. A redirect is followed one checked hop at
a time; only the status decides, and no response body is ever read.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from urllib.parse import urljoin

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
#: The probe asks for an HTML page and refuses a compressed body it never reads.
_PROBE_HEADERS = {"Accept": "text/html", "Accept-Encoding": "identity"}
#: Failures that mean "no answer" rather than an error the caller must handle (D192).
_NO_ANSWER_ERRORS = (GuardBlocked, httpx.HTTPError, httpx.InvalidURL, ValueError)


async def first_answering(http: httpx.AsyncClient, urls: Sequence[str]) -> str | None:
    """Return the first of ``urls`` that answers 2xx, or None (R-5.10, D192).

    Candidates are tried one at a time, each within ``PROBE_TIMEOUT_S``; the whole probe stops
    at ``PROBE_TOTAL_S``. The URL returned is the one given, not where it redirects.
    """
    try:
        async with asyncio.timeout(PROBE_TOTAL_S):
            for url in urls:
                if await _answers(http, url):
                    return url
    except TimeoutError:
        logger.info("probe gave up at its deadline", extra={"error": "TimeoutError"})
    return None


async def _answers(http: httpx.AsyncClient, url: str) -> bool:
    """Return whether ``url`` answers within its own deadline (R-5.10, D192)."""
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_S):
            return await _follow(http, url)
    except TimeoutError:
        logger.debug("probe timed out", extra={"error": "TimeoutError"})
        return False


async def _follow(http: httpx.AsyncClient, url: str) -> bool:
    """Return whether ``url`` or the last hop it redirects to answers (R-5.10, D192)."""
    current = url
    redirects = 0
    while True:
        refusal = taught_url_refusal(current)
        if refusal is not None:
            logger.debug("probe refused", extra={"reason": refusal.value})
            return False
        try:
            response = await _send(http, current)
        except _NO_ANSWER_ERRORS as err:
            logger.debug("probe failed", extra={"error": type(err).__name__})
            return False
        try:
            if httpx.codes.is_success(response.status_code):
                logger.debug("probe answered", extra={"status": response.status_code})
                return True
            location = _redirect_location(response)
            if location is None:
                logger.debug("probe got no answer", extra={"status": response.status_code})
                return False
        finally:
            await response.aclose()
        if redirects >= MAX_PROBE_REDIRECTS:
            return False
        try:
            current = urljoin(current, location)
        except ValueError as err:
            logger.debug("probe failed", extra={"error": type(err).__name__})
            return False
        redirects += 1


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
