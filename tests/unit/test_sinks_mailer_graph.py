"""Tests for the Graph mail client (R-7.6, R-7.7)."""

from __future__ import annotations

import base64
import contextlib
import json
import urllib.parse
from collections.abc import AsyncIterator, Callable
from dataclasses import replace

import httpx
import pytest

from invoicebot.auth.msal_cache import AuthRequired
from invoicebot.sinks.base import Attachment, OutgoingMail
from invoicebot.sinks.mailer_graph import MAX_INLINE_ATTACHMENT_BYTES, GraphMailer
from invoicebot.sinks.onedrive import (
    GRAPH_BASE_URL,
    NOT_SENT_STATUS,
    UPLOAD_CHUNK_BYTES,
    GraphError,
    GraphTransient,
)

ACCOUNT = "personal"
BEARER = "synthetic-bearer-value"
TIMEOUT_S = 5.0

PDF_BYTES = b"%PDF-1.4 synthetic"
XLSX_BYTES = b"PK\x03\x04 synthetic"
XML_BYTES = b"<Invoice/>"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

SMALL_MAIL = OutgoingMail(
    to=("owner@example.com",),
    cc=("accounting@example.com",),
    subject="Support release 05/11/2026",
    body="Here is the invoice.",
    attachments=(
        Attachment("invoice.pdf", PDF_BYTES, "application/pdf"),
        Attachment("invoice.xlsx", XLSX_BYTES, XLSX_MIME),
        Attachment("invoice.xml", XML_BYTES, "application/xml"),
    ),
)

LARGE_BYTES = b"x" * (4 * 1024 * 1024)

LARGE_MAIL = OutgoingMail(
    to=("owner@example.com",),
    cc=(),
    subject="Support release 05/11/2026",
    body="Here is the invoice.",
    attachments=(Attachment("invoice.pdf", LARGE_BYTES, "application/pdf"),),
)

Handler = Callable[[httpx.Request], httpx.Response]


class FakeTokens:
    """A TokenSource that always hands out the same synthetic token."""

    async def token(self) -> str:
        return BEARER


@contextlib.asynccontextmanager
async def _make_mailer(handler: Handler) -> AsyncIterator[GraphMailer]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        yield GraphMailer(http, FakeTokens(), TIMEOUT_S, account=ACCOUNT)


def _large_path_handler(requests: list[httpx.Request], *, message_id: str = "message-id") -> Handler:
    """Answer the draft, the upload sessions, the chunks and the send of a large mail."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/me/messages"):
            return httpx.Response(201, json={"id": message_id})
        if request.method == "POST" and path.endswith("/attachments/createUploadSession"):
            return httpx.Response(201, json={"uploadUrl": "https://outlook.office.com/api/v2.0/session"})
        if request.method == "PUT":
            end, total = request.headers["Content-Range"].split(" ")[1].split("/")
            return httpx.Response(200 if int(end.split("-")[1]) + 1 == int(total) else 202)
        if request.method == "POST" and path.endswith("/attachments"):
            return httpx.Response(201, json={"id": "attachment-id"})
        if request.method == "POST" and path.endswith("/send"):
            return httpx.Response(202)
        return httpx.Response(500)

    return handler


def _inline_attachment(name: str, data: bytes, mime: str) -> dict[str, object]:
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": name,
        "contentType": mime,
        "contentBytes": base64.b64encode(data).decode("ascii"),
    }


async def test_a_small_mail_is_one_send_mail_call_with_the_attachments() -> None:
    """R-7.6: a mail within 3 MB is one sendMail call with the exact subject, recipients and files."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202)

    async with _make_mailer(handler) as mailer:
        await mailer.send(SMALL_MAIL)

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{GRAPH_BASE_URL}/me/sendMail"
    assert request.headers["Authorization"] == f"Bearer {BEARER}"
    assert json.loads(request.content) == {
        "message": {
            "subject": "Support release 05/11/2026",
            "body": {"contentType": "Text", "content": "Here is the invoice."},
            "toRecipients": [{"emailAddress": {"address": "owner@example.com"}}],
            "ccRecipients": [{"emailAddress": {"address": "accounting@example.com"}}],
            "attachments": [
                _inline_attachment("invoice.pdf", PDF_BYTES, "application/pdf"),
                _inline_attachment("invoice.xlsx", XLSX_BYTES, XLSX_MIME),
                _inline_attachment("invoice.xml", XML_BYTES, "application/xml"),
            ],
        },
        "saveToSentItems": True,
    }


async def test_the_threshold_is_inclusive_and_counts_the_sum_of_attachment_sizes() -> None:
    """R-7.6: exactly 3 MB in total still goes inline, so the threshold is the sum, inclusive."""
    requests: list[httpx.Request] = []
    mail = replace(
        SMALL_MAIL,
        attachments=(
            Attachment("a.pdf", b"x" * (MAX_INLINE_ATTACHMENT_BYTES // 2), "application/pdf"),
            Attachment("b.pdf", b"x" * (MAX_INLINE_ATTACHMENT_BYTES // 2), "application/pdf"),
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202)

    async with _make_mailer(handler) as mailer:
        await mailer.send(mail)

    assert len(requests) == 1
    assert requests[0].url.path.endswith("/me/sendMail")


async def test_a_large_mail_goes_through_draft_upload_session_and_send() -> None:
    """R-7.6: above 3 MB the mail becomes a draft, its attachment an upload session, then a send."""
    requests: list[httpx.Request] = []

    async with _make_mailer(_large_path_handler(requests)) as mailer:
        await mailer.send(LARGE_MAIL)

    assert [request.method for request in requests] == ["POST", "POST", "PUT", "PUT", "POST"]
    assert [request.url.path for request in requests] == [
        "/v1.0/me/messages",
        "/v1.0/me/messages/message-id/attachments/createUploadSession",
        "/api/v2.0/session",
        "/api/v2.0/session",
        "/v1.0/me/messages/message-id/send",
    ]
    assert requests[0].headers["Authorization"] == f"Bearer {BEARER}"
    assert requests[-1].headers["Authorization"] == f"Bearer {BEARER}"

    draft = json.loads(requests[0].content)
    assert "attachments" not in draft
    assert draft["subject"] == "Support release 05/11/2026"
    assert draft["toRecipients"] == [{"emailAddress": {"address": "owner@example.com"}}]
    assert json.loads(requests[1].content) == {
        "AttachmentItem": {
            "attachmentType": "file",
            "name": "invoice.pdf",
            "size": len(LARGE_BYTES),
            "contentType": "application/pdf",
        }
    }

    chunks = [request for request in requests if request.method == "PUT"]
    assert len(chunks) == 2
    assert chunks[0].headers["Content-Range"] == f"bytes 0-{UPLOAD_CHUNK_BYTES - 1}/{len(LARGE_BYTES)}"
    assert chunks[1].headers["Content-Range"] == (
        f"bytes {UPLOAD_CHUNK_BYTES}-{len(LARGE_BYTES) - 1}/{len(LARGE_BYTES)}"
    )


async def test_a_large_mail_sends_small_attachments_plainly_and_large_ones_by_session() -> None:
    """R-7.6: in a large mail a file under 3 MB is a plain attachment POST; a larger one a session."""
    requests: list[httpx.Request] = []
    mail = replace(
        LARGE_MAIL,
        attachments=(
            Attachment("a.pdf", LARGE_BYTES, "application/pdf"),
            Attachment("b.xml", XML_BYTES, "application/xml"),
        ),
    )

    async with _make_mailer(_large_path_handler(requests)) as mailer:
        await mailer.send(mail)

    sessions = [request for request in requests if request.url.path.endswith("/createUploadSession")]
    assert [json.loads(request.content)["AttachmentItem"]["name"] for request in sessions] == ["a.pdf"]
    plain = [request for request in requests if request.url.path.endswith("/message-id/attachments")]
    assert [json.loads(request.content) for request in plain] == [
        _inline_attachment("b.xml", XML_BYTES, "application/xml")
    ]
    assert plain[0].headers["Authorization"] == f"Bearer {BEARER}"
    assert requests[-1].url.path.endswith("/message-id/send")


async def test_the_upload_session_chunks_carry_no_authorization_header() -> None:
    """R-7.6: the chunk PUTs carry no bearer header, so the token never leaves Graph."""
    requests: list[httpx.Request] = []

    async with _make_mailer(_large_path_handler(requests)) as mailer:
        await mailer.send(LARGE_MAIL)

    chunks = [request for request in requests if request.method == "PUT"]
    assert chunks
    assert all("Authorization" not in chunk.headers for chunk in chunks)


async def test_the_draft_id_is_percent_encoded_in_every_later_url() -> None:
    """R-7.6: a draft id with reserved characters is quoted in the session and send URLs."""
    requests: list[httpx.Request] = []
    message_id = "AAMk/id+value="
    encoded = urllib.parse.quote(message_id, safe="")

    async with _make_mailer(_large_path_handler(requests, message_id=message_id)) as mailer:
        await mailer.send(LARGE_MAIL)

    assert encoded != message_id
    assert str(requests[1].url) == (f"{GRAPH_BASE_URL}/me/messages/{encoded}/attachments/createUploadSession")
    assert str(requests[-1].url) == f"{GRAPH_BASE_URL}/me/messages/{encoded}/send"
    assert all(message_id not in str(request.url) for request in requests)


@pytest.mark.parametrize("body", [{}, {"id": ""}, {"id": 7}, {"id": None}])
async def test_a_draft_without_a_usable_id_raises_before_any_upload(body: dict[str, object]) -> None:
    """R-7.6: a draft answer without a non-empty string id is not sent and no chunk goes out."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json=body)

    async with _make_mailer(handler) as mailer:
        with pytest.raises(GraphError) as raised:
            await mailer.send(LARGE_MAIL)

    assert raised.value.status == NOT_SENT_STATUS
    assert len(requests) == 1


async def test_a_draft_answering_200_is_a_failed_send() -> None:
    """R-7.6: /me/messages must answer 201; a non-error 200 is still not a created draft."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "message-id"})

    async with _make_mailer(handler) as mailer:
        with pytest.raises(GraphError) as raised:
            await mailer.send(LARGE_MAIL)

    assert raised.value.status == 200
    assert len(requests) == 1


async def test_a_non_https_upload_session_url_sends_no_chunk() -> None:
    """R-7.6: an upload session URL that is not HTTPS is refused before any chunk."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/me/messages"):
            return httpx.Response(201, json={"id": "message-id"})
        return httpx.Response(201, json={"uploadUrl": "http://outlook.office.com/api/v2.0/session"})

    async with _make_mailer(handler) as mailer:
        with pytest.raises(GraphError) as raised:
            await mailer.send(LARGE_MAIL)

    assert raised.value.status == NOT_SENT_STATUS
    assert [request.method for request in requests] == ["POST", "POST"]


async def test_an_upload_session_outside_microsoft_sends_no_chunk_and_no_send() -> None:
    """R-7.6: an attachment upload session on a non-Microsoft host is refused, so no PUT and no send."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/me/messages"):
            return httpx.Response(201, json={"id": "message-id"})
        return httpx.Response(201, json={"uploadUrl": "https://evil.example/u"})

    async with _make_mailer(handler) as mailer:
        with pytest.raises(GraphError) as raised:
            await mailer.send(LARGE_MAIL)

    assert raised.value.status == NOT_SENT_STATUS
    assert [request.method for request in requests] == ["POST", "POST"]


async def test_a_missing_upload_session_url_sends_no_chunk() -> None:
    """R-7.6: a createUploadSession answer without an uploadUrl fails as not sent."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/me/messages"):
            return httpx.Response(201, json={"id": "message-id"})
        return httpx.Response(201, json={})

    async with _make_mailer(handler) as mailer:
        with pytest.raises(GraphError) as raised:
            await mailer.send(LARGE_MAIL)

    assert raised.value.status == NOT_SENT_STATUS


async def test_a_401_maps_to_auth_required_for_the_account() -> None:
    """R-7.6: a 401 means the account needs a sign-in and the message names the account only."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    async with _make_mailer(handler) as mailer:
        with pytest.raises(AuthRequired) as raised:
            await mailer.send(SMALL_MAIL)

    assert raised.value.account == ACCOUNT
    assert "owner@example.com" not in str(raised.value)
    assert SMALL_MAIL.subject not in str(raised.value)
    assert BEARER not in str(raised.value)


async def test_a_503_maps_to_graph_transient() -> None:
    """R-7.6: a 5xx from sendMail is a transient failure the queue retries, with the status only."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with _make_mailer(handler) as mailer:
        with pytest.raises(GraphTransient) as raised:
            await mailer.send(SMALL_MAIL)

    assert raised.value.status == 503


async def test_a_400_maps_to_graph_error_without_the_mail_contents() -> None:
    """R-7.6: any other 4xx is final and its message carries the status only."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400)

    async with _make_mailer(handler) as mailer:
        with pytest.raises(GraphError) as raised:
            await mailer.send(SMALL_MAIL)

    assert raised.value.status == 400
    assert "owner@example.com" not in str(raised.value)
    assert "Here is the invoice." not in str(raised.value)


async def test_send_mail_accepts_a_non_error_status_other_than_202() -> None:
    """R-7.6: a 200 from sendMail is still accepted; only an error status fails the send."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    async with _make_mailer(handler) as mailer:
        await mailer.send(SMALL_MAIL)

    assert len(requests) == 1


async def test_a_failed_send_of_the_draft_maps_the_error_status() -> None:
    """R-7.6: an error on the draft's send fails with the mapped exception, after the chunks."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if request.method == "PUT":
            return httpx.Response(200)
        if path.endswith("/me/messages"):
            return httpx.Response(201, json={"id": "message-id"})
        if path.endswith("/attachments/createUploadSession"):
            return httpx.Response(201, json={"uploadUrl": "https://outlook.office.com/api/v2.0/session"})
        return httpx.Response(400)

    async with _make_mailer(handler) as mailer:
        with pytest.raises(GraphError) as raised:
            await mailer.send(LARGE_MAIL)

    assert raised.value.status == 400
    assert requests[-1].url.path.endswith("/message-id/send")
