"""Tests for the GitHub outbox transport (R-11.5, R-11.10, BR-D05, R-11.12)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest

from invoicebot.github.app_token import GitHubTokenError
from invoicebot.github.issue_text import CLOSE_COMMENT
from invoicebot.github.transport import GitHubHooks, make_github_transport
from invoicebot.queue.outbox import OutboxRow, PermanentSendError, SendLater

REPO = "maku/ops"
ISSUES_URL = f"https://api.github.com/repos/{REPO}/issues"
ISSUE_URL = f"https://github.com/{REPO}/issues"
FINGERPRINT = "portal_missing:20100000001"
BUDGET = 10
ROW_ID = 1
HINT_URL = "https://user:pass@portal.example.pe/x?token=1#top"
REDUCED_HINT_URL = "https://portal.example.pe/x"

Handler = Callable[[httpx.Request], httpx.Response]


class FakeTokenSource:
    """A token source that records invalidate calls and can fail like the App does."""

    def __init__(self, error: GitHubTokenError | None = None) -> None:
        self.error = error
        self.calls = 0
        self.invalidated = 0

    async def token(self) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return "token-1"

    def invalidate(self) -> None:
        self.invalidated += 1


class Recorder:
    """Records the hooks the transport calls and the budget the caller reports."""

    def __init__(self, writes_today: int = 0, issue_url: str | None = None) -> None:
        self.writes_today = writes_today
        self.issue_url = issue_url
        self.issues: list[tuple[str, str]] = []
        self.token_errors: list[str] = []
        self.issue_url_calls: list[str] = []

    async def budget(self) -> int:
        return self.writes_today

    async def on_issue(self, fingerprint: str, issue_url: str) -> None:
        self.issues.append((fingerprint, issue_url))

    async def on_token_error(self, reason: str) -> None:
        self.token_errors.append(reason)

    async def gap_issue_url(self, fingerprint: str) -> str | None:
        self.issue_url_calls.append(fingerprint)
        return self.issue_url

    def hooks(self) -> GitHubHooks:
        return GitHubHooks(
            writes_today=self.budget,
            on_issue=self.on_issue,
            on_token_error=self.on_token_error,
            issue_url=self.gap_issue_url,
        )


def _row(payload: dict[str, Any]) -> OutboxRow:
    """Build the github outbox row a transport receives."""
    return OutboxRow(
        id=ROW_ID,
        channel="github",
        to="ops",
        payload=payload,
        template=None,
        attempts=0,
    )


def _fields(hint_url: str | None = None) -> dict[str, Any]:
    """Return the JSON issue fields of a PORTAL_MISSING gap."""
    return {
        "kind": "PORTAL_MISSING",
        "fingerprint": FINGERPRINT,
        "ruc_emisor": 20100000001,
        "razon_social": "Tienda Example",
        "tipo": "01",
        "reasons": ["no_portal"],
        "portal_key": None,
        "hint_url": hint_url,
    }


def _create_payload() -> dict[str, Any]:
    return {"op": "create_issue", "fingerprint": FINGERPRINT, "fields": _fields()}


async def _run(
    handler: Handler,
    payload: dict[str, Any],
    recorder: Recorder,
    *,
    tokens: FakeTokenSource | None = None,
    budget: int = BUDGET,
) -> str | None:
    """Run one row through a transport backed by a MockTransport handler."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        transport = make_github_transport(
            tokens or FakeTokenSource(),
            http,
            REPO,
            ["gap"],
            budget,
            recorder.hooks(),
        )
        return await transport(_row(payload))


async def test_new_gap_creates_one_issue_with_both_labels_and_reports_it() -> None:
    """R-11.5: a new gap creates one issue with the gap and kind labels and calls on_issue."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"html_url": f"{ISSUE_URL}/7", "number": 7})

    recorder = Recorder()
    tokens = FakeTokenSource()
    result = await _run(handler, _create_payload(), recorder, tokens=tokens)

    assert result == "7"
    assert [request.method for request in requests] == ["GET", "POST"]
    created = json.loads(requests[1].content)
    assert created["labels"] == ["gap", "gap:PORTAL_MISSING"]
    assert created["title"] == "gap: PORTAL_MISSING RUC 20100000001"
    assert f"<!-- gap:{FINGERPRINT} -->" in created["body"]
    assert recorder.issues == [(FINGERPRINT, f"{ISSUE_URL}/7")]
    assert tokens.calls == 1


async def test_existing_issue_with_the_marker_is_reused_without_a_write() -> None:
    """R-11.5: a lost DB row reuses the issue whose body holds the fingerprint marker."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "html_url": f"{ISSUE_URL}/9",
                    "body": f"texto\n\n<!-- gap:{FINGERPRINT} -->",
                }
            ],
        )

    recorder = Recorder()
    result = await _run(handler, _create_payload(), recorder)

    assert result == "9"
    assert [request.method for request in requests] == ["GET"]
    assert recorder.issues == [(FINGERPRINT, f"{ISSUE_URL}/9")]


async def test_budget_reached_reschedules_without_a_request() -> None:
    """R-11.10: a row over the daily budget raises SendLater before any request is sent."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent over budget")

    recorder = Recorder(writes_today=BUDGET)
    tokens = FakeTokenSource()
    with pytest.raises(SendLater) as raised:
        await _run(handler, _create_payload(), recorder, tokens=tokens)

    assert raised.value.delay == timedelta(hours=1)
    assert raised.value.reason == "budget"
    assert tokens.calls == 0


async def test_token_error_calls_the_hook_and_reschedules() -> None:
    """BR-D05: a token failure calls on_token_error and raises SendLater for an hour."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent without a token")

    recorder = Recorder()
    tokens = FakeTokenSource(error=GitHubTokenError("bad_key"))
    with pytest.raises(SendLater) as raised:
        await _run(handler, _create_payload(), recorder, tokens=tokens)

    assert recorder.token_errors == ["bad_key"]
    assert raised.value.delay == timedelta(hours=1)
    assert raised.value.reason == "BR-D05:bad_key"


async def test_server_error_reschedules_for_fifteen_minutes() -> None:
    """R-11.10: a 502 reschedules the row without counting an attempt."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={})

    with pytest.raises(SendLater) as raised:
        await _run(handler, _create_payload(), Recorder())

    assert raised.value.delay == timedelta(minutes=15)
    assert raised.value.reason == "github 502"


async def test_comment_posts_to_the_issue_number_of_the_url() -> None:
    """R-11.5: a comment row posts the reduced hint URL in a code fence to its issue number."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={})

    payload = {
        "op": "comment",
        "fingerprint": FINGERPRINT,
        "issue_url": f"{ISSUE_URL}/42",
        "hint_url": HINT_URL,
    }
    result = await _run(handler, payload, Recorder())

    assert result is None
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == f"{ISSUES_URL}/42/comments"
    assert json.loads(requests[0].content) == {"body": f"```\n{REDUCED_HINT_URL}\n```"}


async def test_comment_without_an_issue_url_posts_to_the_issue_the_hook_returns() -> None:
    """D118: a comment row with no issue_url posts to the issue number the hook resolves."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={})

    payload = {"op": "comment", "fingerprint": FINGERPRINT, "hint_url": HINT_URL}
    recorder = Recorder(issue_url=f"{ISSUE_URL}/77")
    result = await _run(handler, payload, recorder)

    assert result is None
    assert len(requests) == 1
    assert str(requests[0].url) == f"{ISSUES_URL}/77/comments"
    assert recorder.issue_url_calls == [FINGERPRINT]


async def test_comment_without_an_issue_yet_waits_without_a_request() -> None:
    """D118: a comment row whose issue is not created yet reschedules before fetching a token."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a pending issue makes no request")

    payload = {"op": "comment", "fingerprint": FINGERPRINT, "hint_url": HINT_URL}
    recorder = Recorder(issue_url=None)
    tokens = FakeTokenSource()
    with pytest.raises(SendLater) as raised:
        await _run(handler, payload, recorder, tokens=tokens)

    assert raised.value.delay == timedelta(hours=1)
    assert raised.value.reason == "issue pending"
    assert tokens.calls == 0
    assert recorder.issue_url_calls == [FINGERPRINT]


async def test_comment_with_an_issue_url_does_not_ask_the_hook() -> None:
    """D118: a comment row that carries issue_url never calls the issue_url hook."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={})

    payload = {
        "op": "comment",
        "fingerprint": FINGERPRINT,
        "issue_url": f"{ISSUE_URL}/42",
        "hint_url": HINT_URL,
    }
    recorder = Recorder(issue_url=f"{ISSUE_URL}/99")
    await _run(handler, payload, recorder)

    assert recorder.issue_url_calls == []
    assert len(requests) == 1
    assert str(requests[0].url) == f"{ISSUES_URL}/42/comments"


async def test_a_malformed_issue_url_is_permanent() -> None:
    """R-11.5: an issue URL without a trailing issue number is never retried."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a malformed issue URL makes no request")

    payload = {
        "op": "comment",
        "fingerprint": FINGERPRINT,
        "issue_url": f"https://github.com/{REPO}/pull/42",
        "hint_url": REDUCED_HINT_URL,
    }
    with pytest.raises(PermanentSendError):
        await _run(handler, payload, Recorder())


async def test_a_rejected_token_drops_the_cache() -> None:
    """R-11.10: a 401 on a write invalidates the cached token and waits fifteen minutes."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(401, json={})

    tokens = FakeTokenSource()
    with pytest.raises(SendLater) as raised:
        await _run(handler, _create_payload(), Recorder(), tokens=tokens)

    assert tokens.invalidated == 1
    assert raised.value.delay == timedelta(minutes=15)


async def test_a_token_rejected_on_the_issue_search_drops_the_cache() -> None:
    """R-11.10: a 401 on the marker search GET also invalidates the cached token."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "a rejected search makes no write"
        return httpx.Response(401, json={})

    tokens = FakeTokenSource()
    with pytest.raises(SendLater) as raised:
        await _run(handler, _create_payload(), Recorder(), tokens=tokens)

    assert tokens.invalidated == 1
    assert raised.value.delay == timedelta(minutes=15)


async def test_close_issue_posts_the_close_comment_then_closes_the_issue() -> None:
    """R-11.9: a close_issue row comments and then patches state closed on its issue number."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={})

    payload = {"op": "close_issue", "issue_url": f"{ISSUE_URL}/42"}
    result = await _run(handler, payload, Recorder())

    assert result is None
    assert [request.method for request in requests] == ["POST", "PATCH"]
    assert str(requests[0].url) == f"{ISSUES_URL}/42/comments"
    assert json.loads(requests[0].content) == {"body": CLOSE_COMMENT}
    assert str(requests[1].url) == f"{ISSUES_URL}/42"
    assert json.loads(requests[1].content) == {"state": "closed"}


async def test_close_issue_with_a_non_numeric_last_segment_is_permanent() -> None:
    """R-11.9: an issue_url whose last path segment is not digits is never retried."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a malformed issue URL makes no request")

    tokens = FakeTokenSource()
    payload = {"op": "close_issue", "issue_url": f"{ISSUE_URL}/not-a-number"}
    with pytest.raises(PermanentSendError):
        await _run(handler, payload, Recorder(), tokens=tokens)

    assert tokens.calls == 0


async def test_close_issue_server_error_on_the_patch_reschedules() -> None:
    """R-11.9: a 5xx on the closing PATCH reschedules the row like any other write."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "PATCH":
            return httpx.Response(502, json={})
        return httpx.Response(201, json={})

    payload = {"op": "close_issue", "issue_url": f"{ISSUE_URL}/42"}
    with pytest.raises(SendLater) as raised:
        await _run(handler, payload, Recorder())

    assert raised.value.delay == timedelta(minutes=15)
    assert raised.value.reason == "github 502"
    assert [request.method for request in requests] == ["POST", "PATCH"]
