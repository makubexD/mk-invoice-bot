"""Tests for the bounce report reader and the draft maker (D207, R-9.16)."""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import httpx
import pytest

from invoicebot.auth.msal_cache import AuthRequired
from invoicebot.sinks.mail_bounce import (
    MAX_DRAFT_BYTES,
    MAX_REPORT_BYTES,
    Bounce,
    DraftUnavailable,
    draft_from_sent,
    find_bounce,
    parse_bounce,
)
from invoicebot.sinks.onedrive import GraphError

BOT_ID = "<fixture-1@example.com>"
OUTLOOK_ID = "<outlook-1@example.com>"
TIMEOUT_S = 5.0
ACCOUNT = "personal"
SINCE = datetime(2026, 11, 27, tzinfo=UTC)
LINK = "https://outlook.live.com/owa/?ItemID=abc&viewmodel=ReadMessageItem"


class FakeTokens:
    """A TokenSource that always hands out the same synthetic token."""

    async def token(self) -> str:
        return "synthetic-bearer-value"


def _report(
    original_id: str = BOT_ID,
    status: str | None = "5.5.0",
    in_reply_to: str | None = OUTLOOK_ID,
    text: str = "sent",
) -> bytes:
    lines = ["From: Microsoft Outlook <postmaster@example.com>", "To: bot@example.com"]
    if in_reply_to is not None:
        lines.append(f"In-Reply-To: {in_reply_to}")
    lines += [
        "Subject: Undeliverable",
        "MIME-Version: 1.0",
        'Content-Type: multipart/report; report-type=delivery-status; boundary="B"',
        "",
        "--B",
        "Content-Type: text/plain",
        "",
        text,
        "--B",
        "Content-Type: message/delivery-status",
        "",
        "Reporting-MTA: dns; example.com",
        "",
        "Final-Recipient: rfc822; nobody@example.com",
        "Action: failed",
    ]
    if status is not None:
        lines.append(f"Status: {status}")
    lines += [
        "",
        "--B",
        "Content-Type: message/rfc822",
        "",
        f"Message-ID: {OUTLOOK_ID}",
        f"X-Microsoft-Original-Message-ID: {original_id}",
        "Subject: invoice",
        "",
        "body",
        "--B--",
        "",
    ]
    return "\r\n".join(lines).encode()


def test_parse_bounce_matches_the_original_id() -> None:
    """D207: a report whose original id is ours gives its status and Outlook's id."""
    assert parse_bounce(_report(), BOT_ID) == Bounce(status="5.5.0", sent_id=OUTLOOK_ID)


def test_parse_bounce_ignores_another_id() -> None:
    """D207: a report of another mail is not ours."""
    assert parse_bounce(_report(original_id="<other@example.com>"), BOT_ID) is None


def test_parse_bounce_ignores_an_id_only_in_the_text_part() -> None:
    """D207: the id quoted in the readable text does not count."""
    raw = _report(original_id="<other@example.com>", text=f"about {BOT_ID}")
    assert parse_bounce(raw, BOT_ID) is None


def test_parse_bounce_without_status_is_a_question_mark() -> None:
    """D207: a report with no Status: gives ?."""
    assert parse_bounce(_report(status=None), BOT_ID) == Bounce("?", OUTLOOK_ID)


def test_parse_bounce_without_in_reply_to_uses_the_part_message_id() -> None:
    """D207: Outlook's id falls back to the rfc822 part's Message-ID."""
    assert parse_bounce(_report(in_reply_to=None), BOT_ID) == Bounce("5.5.0", OUTLOOK_ID)


def _inbox_handler(raw: bytes, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/inbox/messages"):
            assert request.url.params["$top"] == "50"
            assert request.url.params["$filter"] == "receivedDateTime ge 2026-11-27T00:00:00Z"
            return httpx.Response(200, json={"value": [{"id": "AAA=", "subject": "x"}]})
        assert request.url.path == "/v1.0/me/messages/AAA=/$value"
        return httpx.Response(status, content=raw)

    return httpx.MockTransport(handler)


async def _find(transport: httpx.MockTransport) -> Bounce | None:
    async with httpx.AsyncClient(transport=transport) as http:
        return await find_bounce(
            http, FakeTokens(), message_id=BOT_ID, since=SINCE, account=ACCOUNT, timeout_s=TIMEOUT_S
        )


async def test_find_bounce_returns_the_matching_report() -> None:
    """D207: find_bounce lists the inbox, reads each message and returns the match."""
    assert await _find(_inbox_handler(_report())) == Bounce("5.5.0", OUTLOOK_ID)


async def test_find_bounce_skips_an_oversize_report() -> None:
    """D207: a report over the cap is skipped, so nothing is found."""
    big = _report() + b"x" * MAX_REPORT_BYTES
    assert await _find(_inbox_handler(big)) is None


async def test_find_bounce_403_carries_the_status_only() -> None:
    """R-9.16: a 403 raises GraphError with the status and no body."""
    with pytest.raises(GraphError) as caught:
        await _find(_inbox_handler(b"secret nobody@example.com", status=403))
    assert caught.value.status == 403
    assert "example.com" not in str(caught.value)


async def test_find_bounce_401_asks_for_the_given_account() -> None:
    """D207: a 401 raises AuthRequired for the account the caller named."""
    with pytest.raises(AuthRequired) as caught:
        await _find(_inbox_handler(b"", status=401))
    assert caught.value.account == ACCOUNT


async def test_find_bounce_refuses_a_naive_since() -> None:
    """D207: a since without a timezone is refused before any request."""
    async with httpx.AsyncClient(transport=_inbox_handler(_report())) as http:
        with pytest.raises(ValueError, match="timezone"):
            await find_bounce(
                http,
                FakeTokens(),
                message_id=BOT_ID,
                since=datetime(2026, 11, 27),  # noqa: DTZ001  # naive on purpose: the case under test
                account=ACCOUNT,
                timeout_s=TIMEOUT_S,
            )


def _draft_transport(
    sent_copy: bytes = b"MIME", sent_items: int = 1, link: object = LINK
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(201, json={"isDraft": True, "webLink": link})
        if request.url.path.endswith("/sentitems/messages"):
            assert request.url.params["$filter"] == f"internetMessageId eq '{OUTLOOK_ID}'"
            return httpx.Response(200, json={"value": [{"id": "SSS"}] * sent_items})
        assert request.url.path == "/v1.0/me/messages/SSS/$value"
        return httpx.Response(200, content=sent_copy)

    return httpx.MockTransport(handler), posts


async def _draft(transport: httpx.MockTransport, sent_id: str = OUTLOOK_ID) -> str:
    async with httpx.AsyncClient(transport=transport) as http:
        return await draft_from_sent(
            http, FakeTokens(), sent_id=sent_id, account=ACCOUNT, timeout_s=TIMEOUT_S
        )


async def test_draft_round_trip_returns_the_link() -> None:
    """D207: the Sent copy is posted as base64 text/plain and the web link comes back."""
    transport, posts = _draft_transport()
    assert await _draft(transport) == LINK
    assert posts[0].headers["Content-Type"] == "text/plain"
    assert base64.b64decode(posts[0].content) == b"MIME"


async def test_draft_without_a_sent_copy_raises() -> None:
    """D207: no Sent copy means no draft."""
    transport, _ = _draft_transport(sent_items=0)
    with pytest.raises(DraftUnavailable):
        await _draft(transport)


async def test_draft_with_a_too_big_copy_raises() -> None:
    """D207: a Sent copy over the cap means no draft."""
    transport, posts = _draft_transport(sent_copy=b"x" * (MAX_DRAFT_BYTES + 1))
    with pytest.raises(DraftUnavailable):
        await _draft(transport)
    assert posts == []


@pytest.mark.parametrize(
    "link",
    [
        "javascript:alert(1)",
        "https://evil.example.com/owa/",
        "http://outlook.live.com/owa/",
        "https://outlook.live.com.evil.example.com/",
        None,
    ],
)
async def test_draft_with_a_bad_link_raises(link: object) -> None:
    """D207: only an https link on an Outlook host is returned."""
    transport, _ = _draft_transport(link=link)
    with pytest.raises(DraftUnavailable):
        await _draft(transport)


async def test_draft_rejects_a_quote_in_the_sent_id() -> None:
    """D207: an id with a quote never reaches the OData filter."""
    transport, _ = _draft_transport()
    with pytest.raises(ValueError, match="message id"):
        await _draft(transport, sent_id="<a'b@example.com>")
