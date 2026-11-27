"""Tests for the bounce report reader and the draft maker (D207, R-9.16)."""

from __future__ import annotations

import base64
import logging
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime

import httpx
import pytest

from invoicebot.auth.msal_cache import AuthRequired
from invoicebot.sinks.mail_bounce import (
    MAX_DRAFT_BYTES,
    MAX_HEADER_BYTES,
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


REPORT_TYPE = 'multipart/report; report-type=delivery-status; boundary="B"'
NOT_A_REPORT = b"From: someone@example.com\r\nSubject: hello\r\nContent-Type: text/plain\r\n\r\nhi\r\n"


def _report(
    original_id: str = BOT_ID,
    status: str | None = "5.5.0",
    in_reply_to: str | None = OUTLOOK_ID,
    text: str = "sent",
    content_type: str = REPORT_TYPE,
) -> bytes:
    lines = ["From: Microsoft Outlook <postmaster@example.com>", "To: bot@example.com"]
    if in_reply_to is not None:
        lines.append(f"In-Reply-To: {in_reply_to}")
    lines += [
        "Subject: Undeliverable",
        "MIME-Version: 1.0",
        f"Content-Type: {content_type}",
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


def _forwarded(report: bytes) -> bytes:
    """Return ``report`` forwarded inside a multipart/mixed mail."""
    head = b'Subject: Fw: Undeliverable\r\nContent-Type: multipart/mixed; boundary="F"\r\n\r\n'
    return head + b"--F\r\nContent-Type: message/rfc822\r\n\r\n" + report + b"\r\n--F--\r\n"


@pytest.mark.parametrize(
    "raw",
    [
        _forwarded(_report()),
        _report(content_type='multipart/mixed; boundary="B"'),
        _report(content_type='multipart/report; report-type=disposition-notification; boundary="B"'),
        _report(content_type='multipart/report; boundary="B"'),
    ],
    ids=["forwarded", "mixed", "other-report-type", "no-report-type"],
)
def test_parse_bounce_needs_a_top_level_delivery_report(raw: bytes) -> None:
    """D207: only a top-level multipart/report of report-type delivery-status counts."""
    assert parse_bounce(raw, BOT_ID) is None


def test_parse_bounce_reads_the_report_type_case_insensitively() -> None:
    """D207: the content type and its report-type match in any case."""
    raw = _report(content_type='Multipart/Report; Report-Type="Delivery-Status"; boundary="B"')
    assert parse_bounce(raw, BOT_ID) == Bounce("5.5.0", OUTLOOK_ID)


def test_parse_bounce_survives_a_deeply_nested_rfc822() -> None:
    """D207: 2000 nested message/rfc822 parts give None, never an error that stops the watch."""
    nested = b"Content-Type: message/rfc822\r\n\r\n" * 2000
    raw = f"Content-Type: {REPORT_TYPE}\r\n\r\n--B\r\n".encode() + nested + b"x\r\n--B--\r\n"
    assert parse_bounce(raw, BOT_ID) is None


@pytest.mark.parametrize(
    "header",
    [
        b"Content-Type: multipart/report; report-type*0*=''%ff; boundary=\"B\"",
        b"Content-Type: multipart/report; report-type=delivery-status; boundary=",
        b'In-Reply-To: <<"\\(\x00',
        b"Content-Type",
    ],
    ids=["rfc2231-garbage", "empty-boundary", "broken-in-reply-to", "no-colon"],
)
def test_parse_bounce_survives_a_malformed_header(header: bytes) -> None:
    """D207: a malformed top-level header gives None and never raises."""
    assert parse_bounce(header + b"\r\n\r\nbody\r\n", BOT_ID) is None


class ChunkStream(httpx.AsyncByteStream):
    """A response body served in the given chunks, counting how many were read."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.served = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            self.served += 1
            yield chunk


type Body = bytes | ChunkStream

INBOX_PARAMS = {
    "$filter": "receivedDateTime ge 2026-11-27T00:00:00Z",
    "$orderby": "receivedDateTime asc",
    "$select": "id",
    "$top": "50",
}


def _inbox(messages: Mapping[str, Body], status: int = 200) -> httpx.MockTransport:
    """Serve an inbox listing ``messages`` in order and each one's MIME."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/inbox/messages"):
            assert dict(request.url.params) == INBOX_PARAMS
            return httpx.Response(200, json={"value": [{"id": item_id} for item_id in messages]})
        item_id = request.url.path.removeprefix("/v1.0/me/messages/").removesuffix("/$value")
        body = messages[item_id]
        if isinstance(body, ChunkStream):
            return httpx.Response(status, stream=body)
        return httpx.Response(status, content=body)

    return httpx.MockTransport(handler)


def _inbox_handler(raw: bytes, status: int = 200) -> httpx.MockTransport:
    return _inbox({"AAA=": raw}, status)


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


async def test_find_bounce_goes_past_a_non_report_and_an_oversize_report(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D207: the loop goes on past a non-report and an oversize report to the match."""
    caplog.set_level(logging.WARNING, logger="invoicebot.sinks.mail_bounce")
    inbox = {"N": NOT_A_REPORT, "O": _report() + b"x" * MAX_REPORT_BYTES, "M": _report()}
    assert await _find(_inbox(inbox)) == Bounce("5.5.0", OUTLOOK_ID)
    [record] = caplog.records
    assert record.__dict__["count"] == 1


async def test_find_bounce_logs_one_warning_for_all_oversize_items(caplog: pytest.LogCaptureFixture) -> None:
    """D207: oversize items log one WARNING per run carrying only their count."""
    caplog.set_level(logging.WARNING, logger="invoicebot.sinks.mail_bounce")
    big = _report() + b"x" * MAX_REPORT_BYTES
    assert await _find(_inbox({"A": big, "B": big})) is None
    [record] = caplog.records
    assert record.getMessage() == "bounce watch skipped oversize inbox messages"
    assert record.__dict__["count"] == 2


async def test_find_bounce_reads_only_the_headers_of_a_non_report() -> None:
    """D207: a message whose top-level headers are not a report is left after its headers."""
    stream = ChunkStream([b"Content-Type: text/plain\r\n\r\n", b"body " * 1000, b"more"])
    assert await _find(_inbox({"A": stream})) is None
    assert stream.served == 1


async def test_find_bounce_skips_headers_over_the_cap() -> None:
    """D207: top-level headers with no end inside MAX_HEADER_BYTES are not a report."""
    stream = ChunkStream([b"X-Pad: " + b"a" * MAX_HEADER_BYTES, b"\r\n\r\n", _report()])
    assert await _find(_inbox({"A": stream})) is None
    assert stream.served == 1


async def test_find_bounce_reads_a_report_split_across_chunks() -> None:
    """D207: a report whose headers end in a later chunk is read whole and matched."""
    raw = _report()
    stream = ChunkStream([raw[:40], raw[40:300], raw[300:]])
    assert await _find(_inbox({"A": stream})) == Bounce("5.5.0", OUTLOOK_ID)


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


SENT_COPY = f"X-Microsoft-Original-Message-ID:  {BOT_ID} \r\nSubject: invoice\r\n\r\nbody\r\n".encode()


def _draft_transport(
    sent_copy: bytes = SENT_COPY, sent_items: int = 1, link: object = LINK
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
            http, FakeTokens(), sent_id=sent_id, message_id=BOT_ID, account=ACCOUNT, timeout_s=TIMEOUT_S
        )


async def test_draft_round_trip_returns_the_link() -> None:
    """D207: the Sent copy is posted as base64 text/plain and the web link comes back."""
    transport, posts = _draft_transport()
    assert await _draft(transport) == LINK
    assert posts[0].headers["Content-Type"] == "text/plain"
    assert base64.b64decode(posts[0].content) == SENT_COPY


@pytest.mark.parametrize(
    "sent_copy",
    [
        b"X-Microsoft-Original-Message-ID: <other@example.com>\r\n\r\nbody\r\n",
        b"Subject: invoice\r\n\r\nbody\r\n",
    ],
    ids=["another-id", "no-id"],
)
async def test_draft_of_a_copy_that_is_not_the_bots_mail_raises(sent_copy: bytes) -> None:
    """D207: a Sent copy without the bot's original id is never drafted, so a forged In-Reply-To fails."""
    transport, posts = _draft_transport(sent_copy=sent_copy)
    with pytest.raises(DraftUnavailable, match="another mail"):
        await _draft(transport)
    assert posts == []


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
        "https://user@outlook.live.com/owa/",
        "https://user:pw@outlook.live.com/owa/",
        "https://outlook.live.com:443/owa/",
        "https://outlook.live.com:99999/owa/",
        "https://outlook.live.com\\@evil.example.com/",
        "https://outlook.live.com/owa/?a=b c",
        "https://outlook.live.com/owa/\t",
        "https://outlook.live.com/owa/\x00",
        "https://outlook.live.com/owa/\x7f",
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
