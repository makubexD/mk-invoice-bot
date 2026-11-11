"""Tests for the WhatsApp Graph API client (R-3.2, R-1.4, R-1.10)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from invoicebot.channels.whatsapp.client import (
    MediaInfo,
    WhatsAppClient,
    WhatsAppError,
    WindowExpired,
)

PHONE_NUMBER_ID = "123456789"
GRAPH_VERSION = "v23.0"
BEARER = "synthetic-bearer-value"
SEND_URL = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
MEDIA_ID = "MID"
MEDIA_URL = "https://mmg.whatsapp.net/v/t62/media"

Handler = Callable[[httpx.Request], httpx.Response]


@contextlib.asynccontextmanager
async def _make_client(handler: Handler) -> AsyncIterator[WhatsAppClient]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        yield WhatsAppClient(
            http,
            phone_number_id=PHONE_NUMBER_ID,
            graph_api_version=GRAPH_VERSION,
            token=BEARER,
        )


async def test_send_posts_with_bearer_and_returns_message_id() -> None:
    """R-3.2: send POSTs to the phone number's messages endpoint and returns Meta's id."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})

    async with _make_client(handler) as client:
        message_id = await client.send({"messaging_product": "whatsapp"})

    assert message_id == "wamid.X"
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == SEND_URL
    assert requests[0].headers["Authorization"] == f"Bearer {BEARER}"


async def test_send_raises_window_expired_for_meta_code_131047() -> None:
    """R-1.10: Meta error code 131047 means the 24-hour window is closed."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": 131047, "message": "Re-engagement"}})

    async with _make_client(handler) as client:
        with pytest.raises(WindowExpired, match="131047") as raised:
            await client.send({})

    assert raised.value.code == 131047
    assert raised.value.status == 400


async def test_send_error_hides_the_token_and_the_url() -> None:
    """R-3.2: a Graph API failure exposes Meta's code but never the token or the URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": 190, "message": "Invalid OAuth"}})

    async with _make_client(handler) as client:
        with pytest.raises(WhatsAppError) as raised:
            await client.send({})

    error = raised.value
    assert error.code == 190
    assert error.status == 401
    assert BEARER not in str(error)
    assert SEND_URL not in str(error)


async def test_media_info_requests_the_media_id_and_maps_its_fields() -> None:
    """R-1.4: media_info GETs the media id and maps URL, MIME, size and SHA-256."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "url": MEDIA_URL,
                "mime_type": "image/jpeg",
                "file_size": "1234",
                "sha256": "abc123",
            },
        )

    async with _make_client(handler) as client:
        info = await client.media_info(MEDIA_ID)

    assert info == MediaInfo(url=MEDIA_URL, mime="image/jpeg", size=1234, sha256="abc123")
    assert len(requests) == 1
    assert str(requests[0].url) == f"https://graph.facebook.com/{GRAPH_VERSION}/{MEDIA_ID}"
    assert requests[0].headers["Authorization"] == f"Bearer {BEARER}"


async def test_stream_media_yields_the_body_in_chunks() -> None:
    """R-1.4: stream_media GETs the URL with the bearer header and yields byte chunks."""
    requests: list[httpx.Request] = []

    async def chunks_to_send() -> AsyncIterator[bytes]:
        for chunk in (b"abc", b"def", b"ghi"):
            yield chunk

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=chunks_to_send())

    async with _make_client(handler) as client, client.stream_media(MEDIA_URL) as body:
        chunks = [chunk async for chunk in body]

    assert chunks == [b"abc", b"def", b"ghi"]
    assert requests[0].method == "GET"
    assert str(requests[0].url) == MEDIA_URL
    assert requests[0].headers["Authorization"] == f"Bearer {BEARER}"


async def test_stream_media_raises_on_a_non_200_answer() -> None:
    """R-1.4: a media download answering 404 raises WhatsAppError."""
    url = "https://mmg.whatsapp.net/v/missing"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": 100, "message": "Not found"}})

    async with _make_client(handler) as client:
        with pytest.raises(WhatsAppError, match="404"):
            async with client.stream_media(url):
                pass


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/file",
        "http://graph.facebook.com/file",
        "https://evilfacebook.com/file",
        "https://facebook.com.evil.example/file",
    ],
)
async def test_stream_media_rejects_a_non_meta_url_without_a_request(url: str) -> None:
    """R-1.4: a media URL that is not HTTPS on a Meta host is rejected before any request."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"unexpected")

    async with _make_client(handler) as client:
        with pytest.raises(WhatsAppError):
            async with client.stream_media(url):
                pass

    assert requests == []


async def test_send_non_json_error_body_raises_without_a_meta_code() -> None:
    """R-3.2: a non-JSON error body still raises WhatsAppError, with code None and the status."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="<html>internal error</html>")

    async with _make_client(handler) as client:
        with pytest.raises(WhatsAppError, match="500") as raised:
            await client.send({})

    assert raised.value.status == 500
    assert raised.value.code is None


@pytest.mark.parametrize("media_id", ["", "../me", "MID/messages", "MID?fields=url", "a b"])
async def test_media_info_rejects_an_id_that_could_change_the_path(media_id: str) -> None:
    """R-1.4: a media id from the webhook that is not a plain token is rejected before any request."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    async with _make_client(handler) as client:
        with pytest.raises(WhatsAppError):
            await client.media_info(media_id)
    assert requests == []
