"""Tests for the Microsoft Graph mail reader (R-5.2, R-6.5)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

import httpx
import pytest

from invoicebot.auth.msal_cache import AuthRequired
from invoicebot.fetchers.base import MailboxError
from invoicebot.fetchers.graph_mail import GraphMailbox, MailAttachment, MailMessage
from invoicebot.sinks.onedrive import GRAPH_BASE_URL, NOT_SENT_STATUS

ACCOUNT = "bot"
BEARER = "synthetic-bearer-value"
TIMEOUT_S = 5.0
MESSAGES_URL = f"{GRAPH_BASE_URL}/me/messages"

Handler = Callable[[httpx.Request], httpx.Response]


class FakeTokens:
    """A TokenSource that always hands out the same synthetic token."""

    async def token(self) -> str:
        return BEARER


@contextlib.asynccontextmanager
async def _make_mailbox(handler: Handler) -> AsyncIterator[GraphMailbox]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        yield GraphMailbox(http, FakeTokens(), ACCOUNT, TIMEOUT_S)


async def test_search_sends_the_cleaned_term_and_the_select_headers() -> None:
    """R-5.2, R-6.5: search asks Graph for the quoted, cleaned term with eventual consistency."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"value": []})

    async with _make_mailbox(handler) as mailbox:
        messages = await mailbox.search('se"rie\\-01', 20)

    assert messages == []
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == "/v1.0/me/messages"
    assert request.url.params["$search"] == '"serie-01"'
    assert request.url.params["$top"] == "20"
    assert request.url.params["$select"] == "id,receivedDateTime,hasAttachments"
    assert request.headers["ConsistencyLevel"] == "eventual"
    assert request.headers["Authorization"] == f"Bearer {BEARER}"


async def test_search_drops_a_message_without_attachments() -> None:
    """R-5.2, R-6.5: only messages Graph marks as having attachments are returned."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "AAA=",
                        "receivedDateTime": "2026-11-14T10:00:00Z",
                        "hasAttachments": False,
                    },
                    {
                        "id": "BBB=",
                        "receivedDateTime": "2026-11-14T11:00:00Z",
                        "hasAttachments": True,
                    },
                ]
            },
        )

    async with _make_mailbox(handler) as mailbox:
        messages = await mailbox.search("serie", 10)

    expected = MailMessage(id="BBB=", received_at=datetime(2026, 11, 14, 11, 0, tzinfo=UTC))
    assert messages == [expected]


async def test_search_refuses_an_id_graph_returns_that_is_not_a_graph_id() -> None:
    """R-5.2, R-6.5: an id Graph returns that fails the pattern raises MailboxError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "../x",
                        "receivedDateTime": "2026-11-14T11:00:00Z",
                        "hasAttachments": True,
                    }
                ]
            },
        )

    async with _make_mailbox(handler) as mailbox:
        with pytest.raises(MailboxError) as raised:
            await mailbox.search("serie", 10)

    assert str(raised.value) == f"Microsoft Graph request failed with status {NOT_SENT_STATUS}"


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"value": "not-a-list"},
        {"value": [42]},
    ],
)
async def test_search_maps_a_malformed_body_to_mailbox_error(body: object) -> None:
    """R-5.2, R-6.5: a body that is not a collection of message objects raises MailboxError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    async with _make_mailbox(handler) as mailbox:
        with pytest.raises(MailboxError) as raised:
            await mailbox.search("serie", 10)

    assert str(raised.value) == f"Microsoft Graph request failed with status {NOT_SENT_STATUS}"


async def test_attachments_lists_only_file_attachments() -> None:
    """R-5.2, R-6.5: the listing keeps file attachments and skips every other kind."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "id": "att1",
                        "name": "factura.pdf",
                        "size": 123,
                        "contentType": "application/pdf",
                    },
                    {
                        "@odata.type": "#microsoft.graph.itemAttachment",
                        "id": "att2",
                        "name": "reenviado.eml",
                        "size": 456,
                    },
                ]
            },
        )

    async with _make_mailbox(handler) as mailbox:
        attachments = await mailbox.attachments("msg1")

    assert attachments == [MailAttachment(id="att1", name="factura.pdf", size=123)]
    assert requests[0].url.path == "/v1.0/me/messages/msg1/attachments"
    assert requests[0].url.params["$select"] == "id,name,size,contentType"
    assert requests[0].headers["Authorization"] == f"Bearer {BEARER}"


async def test_download_returns_the_bytes_within_the_cap() -> None:
    """R-5.2, R-6.5: an attachment under the cap streams back unchanged."""
    requests: list[httpx.Request] = []
    content = b"%PDF-1.4 synthetic invoice"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=content)

    async with _make_mailbox(handler) as mailbox:
        data = await mailbox.download("msg1", "att1", max_bytes=len(content))

    assert data == content
    assert str(requests[0].url) == f"{GRAPH_BASE_URL}/me/messages/msg1/attachments/att1/$value"
    assert requests[0].headers["Authorization"] == f"Bearer {BEARER}"


async def test_download_refuses_an_attachment_over_the_cap() -> None:
    """R-5.2, R-6.5: a body that grows past max_bytes raises ValueError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 11)

    async with _make_mailbox(handler) as mailbox:
        with pytest.raises(ValueError, match="exceeds 10 bytes"):
            await mailbox.download("msg1", "att1", max_bytes=10)


@pytest.mark.parametrize(("status", "error"), [(401, AuthRequired), (403, MailboxError)])
async def test_search_maps_a_failed_response_to_the_right_error(status: int, error: type[Exception]) -> None:
    """R-5.2, R-6.5: a 401 asks for sign-in and any other failure is final, reported as MailboxError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    async with _make_mailbox(handler) as mailbox:
        with pytest.raises(error):
            await mailbox.search("serie", 10)


async def test_search_with_a_server_error_is_a_mailbox_error() -> None:
    """R-5.2: a Graph 500 surfaces from search as MailboxError, not a Graph-specific error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with _make_mailbox(handler) as mailbox:
        with pytest.raises(MailboxError):
            await mailbox.search("serie", 10)


async def test_a_bad_id_is_refused_before_any_request() -> None:
    """R-5.2, R-6.5: an id that fails the pattern raises MailboxError and no request is sent."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"value": []})

    async with _make_mailbox(handler) as mailbox:
        with pytest.raises(MailboxError) as raised:
            await mailbox.attachments("../x")

    assert str(raised.value) == f"Microsoft Graph request failed with status {NOT_SENT_STATUS}"
    assert requests == []
