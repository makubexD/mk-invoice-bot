"""Tests for the Gmail mail reader (R-5.2, D135)."""

from __future__ import annotations

import base64
import contextlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

import httpx
import pytest

from invoicebot.auth.msal_cache import AuthRequired
from invoicebot.fetchers.base import MailboxError
from invoicebot.fetchers.gmail import GmailMailbox
from invoicebot.fetchers.graph_mail import MailAttachment, MailMessage

ACCOUNT = "bot"
BEARER = "synthetic-bearer-value"
TIMEOUT_S = 5.0
MAX_MESSAGE_BYTES = 1024
MESSAGES_PATH = "/gmail/v1/users/me/messages"
INTERNAL_DATE_MS = "1763200000000"
RECEIVED_AT = datetime.fromtimestamp(int(INTERNAL_DATE_MS) / 1000, tz=UTC)

Handler = Callable[[httpx.Request], httpx.Response]


class FakeTokens:
    """A TokenSource that always hands out the same synthetic token."""

    async def token(self) -> str:
        return BEARER


@contextlib.asynccontextmanager
async def _make_mailbox(handler: Handler) -> AsyncIterator[GmailMailbox]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        yield GmailMailbox(http, FakeTokens(), ACCOUNT, TIMEOUT_S, MAX_MESSAGE_BYTES)


def _part(name: str, attachment_id: str, size: int) -> dict[str, object]:
    """Return a synthetic Gmail MIME part carrying one file."""
    return {"filename": name, "body": {"attachmentId": attachment_id, "size": size}}


async def test_search_sends_the_quoted_term_and_the_attachment_filter() -> None:
    """R-5.2: the list query holds the quoted term and the attachment filter."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == MESSAGES_PATH:
            return httpx.Response(200, json={"messages": [{"id": "msg_1"}]})
        return httpx.Response(200, json={"id": "msg_1", "internalDate": INTERNAL_DATE_MS})

    async with _make_mailbox(handler) as mailbox:
        messages = await mailbox.search("F001-123", 10)

    assert messages == [MailMessage(id="msg_1", received_at=RECEIVED_AT)]
    list_request = requests[0]
    assert list_request.url.params["q"] == ('has:attachment (filename:xml OR filename:pdf) "F001-123"')
    assert list_request.url.params["maxResults"] == "10"
    assert list_request.headers["Authorization"] == f"Bearer {BEARER}"
    metadata_request = requests[1]
    assert metadata_request.url.path == f"{MESSAGES_PATH}/msg_1"
    assert metadata_request.url.params["format"] == "metadata"


async def test_search_removes_a_quote_from_the_term() -> None:
    """R-5.2: a term's ``"`` and ``\\`` are removed before Gmail sees it."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"messages": []})

    async with _make_mailbox(handler) as mailbox:
        assert await mailbox.search('se"rie\\-01', 5) == []

    assert requests[0].url.params["q"] == ('has:attachment (filename:xml OR filename:pdf) "serie-01"')


async def test_search_returns_messages_newest_first() -> None:
    """R-5.2: the messages come back newest first by internalDate."""
    dates = {"old": "1763000000000", "new": INTERNAL_DATE_MS}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == MESSAGES_PATH:
            return httpx.Response(200, json={"messages": [{"id": "old"}, {"id": "new"}]})
        message_id = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"id": message_id, "internalDate": dates[message_id]})

    async with _make_mailbox(handler) as mailbox:
        messages = await mailbox.search("F001-123", 10)

    assert [message.id for message in messages] == ["new", "old"]


async def test_attachments_finds_a_nested_part() -> None:
    """R-5.2: a file part nested inside another part is listed."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"payload": {"parts": [{"parts": [_part("factura.xml", "att_1", 123)]}]}},
        )

    async with _make_mailbox(handler) as mailbox:
        attachments = await mailbox.attachments("msg_1")

    assert attachments == [MailAttachment(id="att_1", name="factura.xml", size=123)]
    assert requests[0].url.path == f"{MESSAGES_PATH}/msg_1"
    assert requests[0].url.params["format"] == "full"


async def test_attachments_refuses_a_full_message_over_the_cap() -> None:
    """R-5.2: a full-message body over max_message_bytes raises MailboxError before decoding."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_MESSAGE_BYTES + 1))

    async with _make_mailbox(handler) as mailbox:
        with pytest.raises(MailboxError, match=f"exceeds {MAX_MESSAGE_BYTES} bytes"):
            await mailbox.attachments("msg_1")


async def test_download_accepts_a_600_character_attachment_id() -> None:
    """R-5.2: a long Gmail attachment id (several hundred characters) is accepted."""
    attachment_id = "a" * 600
    content = b"%PDF-1.7 synthetic"
    encoded = base64.urlsafe_b64encode(content).decode().rstrip("=")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": encoded, "size": len(content)})

    async with _make_mailbox(handler) as mailbox:
        data = await mailbox.download("msg_1", attachment_id, max_bytes=100)

    assert data == content
    assert requests[0].url.path == f"{MESSAGES_PATH}/msg_1/attachments/{attachment_id}"


async def test_download_refuses_a_streamed_body_over_the_raw_cap() -> None:
    """R-5.2: a streamed body larger than the base64 raw cap raises ValueError."""
    max_bytes = 10
    raw_cap = max_bytes * 4 // 3 + 4096

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (raw_cap + 1))

    async with _make_mailbox(handler) as mailbox:
        with pytest.raises(ValueError, match="exceeds 10 bytes"):
            await mailbox.download("msg_1", "att_1", max_bytes=max_bytes)


async def test_download_decodes_a_base64url_body() -> None:
    """R-5.2: the attachment's unpadded base64url body decodes to the file bytes."""
    content = b"\xfb\xff\x10"
    encoded = base64.urlsafe_b64encode(content).decode().rstrip("=")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/attachments/att_1"):
            return httpx.Response(200, json={"data": encoded, "size": len(content)})
        return httpx.Response(
            200,
            json={"payload": {"parts": [_part("factura.pdf", "att_1", len(content))]}},
        )

    async with _make_mailbox(handler) as mailbox:
        data = await mailbox.download("msg_1", "att_1", max_bytes=100)

    assert data == content
    assert requests[-1].url.path == f"{MESSAGES_PATH}/msg_1/attachments/att_1"


async def test_download_refuses_a_decoded_body_over_the_cap() -> None:
    """R-5.2: a body under the raw cap but decoding past max_bytes raises ValueError."""
    content = b"x" * 11
    encoded = base64.urlsafe_b64encode(content).decode().rstrip("=")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/attachments/att_1"):
            return httpx.Response(200, json={"data": encoded, "size": len(content)})
        return httpx.Response(
            200,
            json={"payload": {"parts": [_part("factura.pdf", "att_1", 1)]}},
        )

    async with _make_mailbox(handler) as mailbox:
        with pytest.raises(ValueError, match="exceeds 10 bytes"):
            await mailbox.download("msg_1", "att_1", max_bytes=10)


async def test_search_maps_a_401_to_auth_required() -> None:
    """R-5.2: a 401 means the account needs a sign-in; AuthRequired passes through."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    async with _make_mailbox(handler) as mailbox:
        with pytest.raises(AuthRequired):
            await mailbox.search("F001-123", 10)


async def test_search_maps_a_500_to_a_mailbox_error() -> None:
    """R-5.2: a server error surfaces as MailboxError, not a Google-specific error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with _make_mailbox(handler) as mailbox:
        with pytest.raises(MailboxError):
            await mailbox.search("F001-123", 10)


async def test_a_bad_id_is_refused_before_any_request() -> None:
    """R-5.2: an id that fails the pattern raises MailboxError and nothing is requested."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"payload": {}})

    async with _make_mailbox(handler) as mailbox:
        with pytest.raises(MailboxError):
            await mailbox.attachments("../x")

    assert requests == []
