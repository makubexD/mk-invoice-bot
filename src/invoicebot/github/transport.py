"""Deliver GitHub gap issues and comments from the outbox (R-11.5, R-11.10, D117).

A ``create_issue`` row first lists the repo's gap issues, at most five pages, and
reuses the issue whose body carries the row fingerprint's hidden marker, so a
lost database row does not open a second issue. Otherwise it renders the typed
issue text and creates one. A ``comment`` row posts a taught portal link to an
existing issue. A ``close_issue`` row comments on the issue and closes it. The
daily write budget, a token failure (BR-D05), a 5xx, a timeout and a rejected
token reschedule the row with :class:`SendLater`; only a 4xx that a retry cannot
fix, or a payload that can never be sent, raises :class:`PermanentSendError`.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

import httpx

from invoicebot.fetchers.base import FetchReason
from invoicebot.github.app_token import GITHUB_API_BASE_URL, GitHubTokenError
from invoicebot.github.issue_text import (
    CLOSE_COMMENT,
    IssueFields,
    issue_marker,
    render_hint_comment,
    render_issue,
)
from invoicebot.queue.outbox import OutboxRow, PermanentSendError, SendLater, Transport

#: Every GitHub request uses the same timeout as the other outbound transports.
REQUEST_TIMEOUT_S = 30.0
#: GitHub's maximum page size for the issue list.
PER_PAGE = 100
#: D117 caps the deduplication scan at five pages of issues.
MAX_ISSUE_PAGES = 5
#: One issue-list page larger than this is treated as an upstream fault, not parsed.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

#: A row over the daily write budget waits for the next local day (R-11.10).
BUDGET_DELAY = timedelta(hours=1)
#: A transient GitHub failure or a rejected token is retried after this long (BR-D05).
RETRY_DELAY = timedelta(minutes=15)

_AUTH_REJECTED = frozenset({401, 403})
_ISSUE_URL_RE = re.compile(r"/issues/(\d+)$")


class TokenSource(Protocol):
    """A cached installation token: implementers must drop the cache on invalidate (R-11.10)."""

    async def token(self) -> str: ...

    def invalidate(self) -> None: ...


@dataclass(frozen=True)
class GitHubHooks:
    """The caller's callbacks: budget, a created issue, a token failure and a gap's issue (R-11.5, D118)."""

    writes_today: Callable[[], Awaitable[int]]
    on_issue: Callable[[str, str], Awaitable[None]]
    on_token_error: Callable[[str], Awaitable[None]]
    issue_url: Callable[[str], Awaitable[str | None]]


@dataclass(frozen=True)
class _Client:
    """Everything one transport needs: the token source, the HTTP client and the repo config."""

    app: TokenSource
    http: httpx.AsyncClient
    repo: str
    labels: tuple[str, ...]
    budget: int
    hooks: GitHubHooks


@dataclass(frozen=True)
class _Request:
    """One GitHub request: method, URL and the options it carries."""

    method: str
    url: str
    json_body: object | None = None
    params: dict[str, str | int] | None = None


def make_github_transport(
    app: TokenSource,
    http: httpx.AsyncClient,
    repo: str,
    labels: list[str],
    budget: int,
    hooks: GitHubHooks,
) -> Transport:
    """Build the outbox transport that writes one gap issue, comment or close per row (R-11.5, D117)."""
    client = _Client(app, http, repo, tuple(labels), budget, hooks)

    async def transport(row: OutboxRow) -> str | None:
        return await _transport(client, row)

    return transport


async def _transport(client: _Client, row: OutboxRow) -> str | None:
    """Send one row: a create-issue write, a comment, or a close on an existing issue."""
    op = row.payload.get("op")
    if op not in ("create_issue", "comment", "close_issue"):
        raise PermanentSendError(f"unknown github op: {op!r}")
    if await client.hooks.writes_today() >= client.budget:
        raise SendLater(BUDGET_DELAY, "budget")
    if op == "create_issue":
        fingerprint = _payload_text(row.payload, "fingerprint")
        token = await _access_token(client)
        return await _create_issue(client, token, fingerprint, _issue_fields(row.payload.get("fields")))
    if op == "close_issue":
        number = _configured_issue_number(client, _payload_text(row.payload, "issue_url"))
        token = await _access_token(client)
        await _close_issue(client, token, number)
        return None
    fingerprint = _payload_text(row.payload, "fingerprint")
    hint_url = _payload_text(row.payload, "hint_url")
    issue_url = await _comment_issue_url(client, fingerprint, row.payload)
    token = await _access_token(client)
    await _comment(client, token, issue_url, hint_url)
    return None


async def _comment_issue_url(client: _Client, fingerprint: str, payload: dict[str, Any]) -> str:
    """Return the issue a comment targets, waiting while its issue is still being created (D118)."""
    issue_url = payload.get("issue_url")
    if isinstance(issue_url, str) and issue_url:
        return issue_url
    resolved = await client.hooks.issue_url(fingerprint)
    if resolved is None:
        raise SendLater(BUDGET_DELAY, "issue pending")
    return resolved


async def _access_token(client: _Client) -> str:
    """Return a token, reporting a token failure once and letting the row wait (BR-D05)."""
    try:
        return await client.app.token()
    except GitHubTokenError as exc:
        await client.hooks.on_token_error(exc.reason)
        raise SendLater(BUDGET_DELAY, f"BR-D05:{exc.reason}") from None


async def _send(client: _Client, token: str, request: _Request) -> httpx.Response:
    """Make one authenticated request, mapping its answer to SendLater or PermanentSendError."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    try:
        response = await client.http.request(
            request.method,
            request.url,
            headers=headers,
            json=request.json_body,
            params=request.params,
            timeout=REQUEST_TIMEOUT_S,
        )
    except httpx.TransportError:
        raise SendLater(RETRY_DELAY, "github unreachable") from None
    status = response.status_code
    if status in _AUTH_REJECTED:
        client.app.invalidate()
        raise SendLater(RETRY_DELAY, f"github {status}")
    if status >= 500:
        raise SendLater(RETRY_DELAY, f"github {status}")
    if status >= 400:
        raise PermanentSendError(f"github {status}")
    return response


async def _find_issue(client: _Client, token: str, marker: str) -> str | None:
    """Return the html_url of the issue whose body holds ``marker``, else None (D117)."""
    for page in range(1, MAX_ISSUE_PAGES + 1):
        response = await _send(
            client,
            token,
            _Request(
                "GET",
                f"{_api_url(client)}/issues",
                params={
                    "labels": ",".join(client.labels),
                    "state": "all",
                    "per_page": PER_PAGE,
                    "page": page,
                },
            ),
        )
        issues = _json_list(response)
        for issue in issues:
            if isinstance(issue, dict) and marker in str(issue.get("body") or ""):
                url = issue.get("html_url")
                if not isinstance(url, str):
                    raise SendLater(RETRY_DELAY, "github issue has no html_url")
                return url
        if len(issues) < PER_PAGE:
            return None
    return None


async def _create_issue(client: _Client, token: str, fingerprint: str, fields: IssueFields) -> str:
    """Reuse or create the issue for ``fingerprint`` and report it to the caller (R-11.5)."""
    existing = await _find_issue(client, token, issue_marker(fingerprint))
    if existing is not None:
        number = _issue_number(existing)
        await client.hooks.on_issue(fingerprint, existing)
        return number
    title, body = render_issue(fields)
    response = await _send(
        client,
        token,
        _Request(
            "POST",
            f"{_api_url(client)}/issues",
            json_body={"title": title, "body": body, "labels": [*client.labels, f"gap:{fields.kind}"]},
        ),
    )
    created = _json_object(response)
    html_url = created.get("html_url")
    if not isinstance(html_url, str):
        raise SendLater(RETRY_DELAY, "github issue created without html_url")
    number = _issue_number(html_url)
    await client.hooks.on_issue(fingerprint, html_url)
    return number


async def _comment(client: _Client, token: str, issue_url: str, hint_url: str) -> None:
    """Post the taught portal link as a code-fenced comment (R-11.12, D118)."""
    number = _issue_number(issue_url)
    body = render_hint_comment(hint_url)
    if body is None:
        raise PermanentSendError("github hint url fails R-11.12")
    await _send(
        client,
        token,
        _Request("POST", f"{_api_url(client)}/issues/{number}/comments", json_body={"body": body}),
    )


async def _close_issue(client: _Client, token: str, number: str) -> None:
    """Close the issue once: a retry that finds it closed writes nothing (R-11.9).

    The PATCH runs before the close comment, so a retry after it finds the issue
    closed. A comment POST that fails after the PATCH is not retried; the issue is
    closed, which is what matters.
    """
    issue_url = f"{_api_url(client)}/issues/{number}"
    existing = _json(await _send(client, token, _Request("GET", issue_url)))
    if isinstance(existing, dict) and existing.get("state") == "closed":
        return
    await _send(client, token, _Request("PATCH", issue_url, json_body={"state": "closed"}))
    await _send(
        client,
        token,
        _Request("POST", f"{issue_url}/comments", json_body={"body": CLOSE_COMMENT}),
    )


def _api_url(client: _Client) -> str:
    """Return the API base URL of the configured repo."""
    return f"{GITHUB_API_BASE_URL}/repos/{client.repo}"


def _payload_text(payload: dict[str, Any], key: str) -> str:
    """Return a required non-empty string from a row payload, or fail permanently."""
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise PermanentSendError(f"github payload has no {key}")
    return value


def _issue_fields(raw: object) -> IssueFields:
    """Rebuild the typed issue fields a JSON payload carried, or fail permanently."""
    if not isinstance(raw, dict):
        raise PermanentSendError("github create_issue payload has no fields")
    values: dict[str, Any] = dict(raw)
    reasons = values.get("reasons", ())
    if not isinstance(reasons, list | tuple):
        raise PermanentSendError("github issue reasons are not a list")
    try:
        values["reasons"] = tuple(FetchReason(reason) for reason in reasons)
        return IssueFields(**values)
    except (TypeError, ValueError) as exc:
        raise PermanentSendError(f"invalid github issue fields: {exc}") from exc


def _configured_issue_number(client: _Client, issue_url: str) -> str:
    """Return the number of an issue in the configured repo, or fail permanently (R-11.9).

    The whole URL must match, ignoring case as GitHub does, so a trailing ``..`` or a
    second ``/issues/`` cannot point the close at another issue.
    """
    pattern = rf"https://github\.com/{re.escape(client.repo)}/issues/(\d+)"
    match = re.fullmatch(pattern, issue_url, re.IGNORECASE)
    if match is None:
        raise PermanentSendError("github issue url is not in the configured repo")
    return match.group(1)


def _issue_number(issue_url: str) -> str:
    """Return the issue number at the end of an issue URL, or fail permanently."""
    match = _ISSUE_URL_RE.search(issue_url)
    if match is None:
        raise PermanentSendError(f"malformed github issue url: {issue_url!r}")
    return match.group(1)


def _json(response: httpx.Response) -> object:
    """Parse a response body after capping its size, a bad one waiting for a retry."""
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise SendLater(RETRY_DELAY, "github response too large")
    try:
        return response.json()
    except ValueError:
        raise SendLater(RETRY_DELAY, "github response is not JSON") from None


def _json_list(response: httpx.Response) -> list[object]:
    """Return the issue-list page body as a list, a malformed one waiting for a retry."""
    body = _json(response)
    if not isinstance(body, list):
        raise SendLater(RETRY_DELAY, "github issue list is not a list")
    return body


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """Return the created-issue body as an object, a malformed one waiting for a retry."""
    body = _json(response)
    if not isinstance(body, dict):
        raise SendLater(RETRY_DELAY, "github issue is not an object")
    return body
