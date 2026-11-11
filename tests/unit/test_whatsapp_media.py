"""Tests for downloading WhatsApp media to disk (R-1.4, R-1.11, BR-E07)."""

from __future__ import annotations

import contextlib
import hashlib
import struct
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest

from invoicebot.channels.media_limits import MediaRejected
from invoicebot.channels.whatsapp.client import WhatsAppClient
from invoicebot.channels.whatsapp.media import download_media
from invoicebot.config.models import MediaLimits, MediaMaxBytes

PHONE_NUMBER_ID = "123456789"
GRAPH_VERSION = "v23.0"
BEARER = "synthetic-bearer-value"
MEDIA_ID = "MID"
MEDIA_URL = "https://mmg.whatsapp.net/v/t62/media"
SECOND_MEDIA_URL = "https://mmg.whatsapp.net/v/t62/media-second"
GRAPH_HOST = "graph.facebook.com"
PNG_MIME = "image/png"

Handler = Callable[[httpx.Request], httpx.Response]

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@contextlib.asynccontextmanager
async def _make_client(handler: Handler) -> AsyncIterator[WhatsAppClient]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        yield WhatsAppClient(
            http,
            phone_number_id=PHONE_NUMBER_ID,
            graph_api_version=GRAPH_VERSION,
            token=BEARER,
        )


def _png_header(width: int, height: int) -> bytes:
    """Build a 33-byte PNG header declaring the given pixel size."""
    ihdr_data = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    ihdr_chunk = struct.pack(">I", 13) + b"IHDR" + ihdr_data
    return _PNG_SIGNATURE + ihdr_chunk + b"\x00\x00\x00\x00"


def _limits(*, image: int = 5_000_000, pdf: int = 10_000_000) -> MediaLimits:
    """Limits with synthetic byte caps for each attachment kind."""
    return MediaLimits(
        max_megapixels=40,
        max_bytes=MediaMaxBytes(image=image, pdf=pdf, xml=1_000_000, zip=2_000_000),
        zip_max_entries=20,
        zip_max_ratio=50,
    )


def _info_response(url: str, size: int, mime: str = PNG_MIME) -> httpx.Response:
    return httpx.Response(200, json={"url": url, "mime_type": mime, "file_size": size})


def _entries(root: Path) -> list[Path]:
    """List a directory's entries from a sync helper, so an async test may call it."""
    return list(root.iterdir())


async def test_download_media_writes_a_small_png_with_its_hash_and_size(tmp_path: Path) -> None:
    """R-1.4: a small PNG is written with the SHA-256 and size of the bytes received."""
    body = _png_header(2000, 1500) + b"\x00" * 50
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == GRAPH_HOST:
            return _info_response(MEDIA_URL, len(body))
        return httpx.Response(200, content=body)

    async with _make_client(handler) as client:
        media = await download_media(client, MEDIA_ID, "photo", tmp_path, _limits())

    assert media.path == tmp_path / MEDIA_ID / "file"
    assert media.path.read_bytes() == body
    assert media.size == len(body)
    assert media.sha256 == hashlib.sha256(body).hexdigest()
    assert media.mime == PNG_MIME
    assert [request.url.host for request in requests] == [GRAPH_HOST, "mmg.whatsapp.net"]


async def test_each_call_requests_fresh_media_info_and_uses_its_url(tmp_path: Path) -> None:
    """R-1.4: every call fetches media info again and downloads from the URL it returns."""
    urls = iter([MEDIA_URL, SECOND_MEDIA_URL])
    bodies = {MEDIA_URL: b"first-body", SECOND_MEDIA_URL: b"second-body"}
    info_requests = 0
    download_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal info_requests
        if request.url.host == GRAPH_HOST:
            info_requests += 1
            return _info_response(next(urls), len(b"second-body"), mime="application/pdf")
        download_hosts.append(str(request.url))
        return httpx.Response(200, content=bodies[str(request.url)])

    async with _make_client(handler) as client:
        first = await download_media(client, MEDIA_ID, "pdf", tmp_path, _limits())
        first_size = first.size
        second = await download_media(client, MEDIA_ID, "pdf", tmp_path, _limits())

    assert info_requests == 2
    assert download_hosts == [MEDIA_URL, SECOND_MEDIA_URL]
    assert first_size == len(b"first-body")
    assert second.path.read_bytes() == b"second-body"


async def test_body_over_the_cap_is_rejected_while_streaming_and_leaves_nothing(
    tmp_path: Path,
) -> None:
    """R-1.11: a body longer than the cap raises too_large and leaves no file or directory."""

    async def chunks_to_send() -> AsyncIterator[bytes]:
        yield b"a" * 60
        yield b"b" * 60

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == GRAPH_HOST:
            return _info_response(MEDIA_URL, 60, mime="application/pdf")
        return httpx.Response(200, content=chunks_to_send())

    async with _make_client(handler) as client:
        with pytest.raises(MediaRejected) as raised:
            await download_media(client, MEDIA_ID, "pdf", tmp_path, _limits(pdf=100))

    assert raised.value.reason == "too_large"
    assert _entries(tmp_path) == []


async def test_media_info_size_over_the_cap_is_rejected_before_downloading(tmp_path: Path) -> None:
    """R-1.11: a file_size above the cap raises too_large and no download request is made."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _info_response(MEDIA_URL, 6_000_000)

    async with _make_client(handler) as client:
        with pytest.raises(MediaRejected) as raised:
            await download_media(client, MEDIA_ID, "photo", tmp_path, _limits())

    assert raised.value.reason == "too_large"
    assert len(requests) == 1
    assert requests[0].url.host == GRAPH_HOST
    assert _entries(tmp_path) == []


async def test_photo_over_the_pixel_limit_is_rejected_after_the_first_chunk(tmp_path: Path) -> None:
    """BR-E07: a 48 MP PNG header raises too_many_pixels and the rest of the body is not read."""
    header = _png_header(8000, 6000)
    chunks = [header + b"\x00" * (64 * 1024 - len(header)), b"more-1", b"more-2", b"more-3"]
    pulled = 0

    async def chunks_to_send() -> AsyncIterator[bytes]:
        nonlocal pulled
        for chunk in chunks:
            pulled += 1
            yield chunk

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == GRAPH_HOST:
            return _info_response(MEDIA_URL, 100_000)
        return httpx.Response(200, content=chunks_to_send())

    async with _make_client(handler) as client:
        with pytest.raises(MediaRejected) as raised:
            await download_media(client, MEDIA_ID, "photo", tmp_path, _limits())

    assert raised.value.reason == "too_many_pixels"
    assert pulled == 1
    assert pulled < len(chunks)
    assert _entries(tmp_path) == []


async def test_media_id_outside_the_plain_identifier_pattern_is_rejected(tmp_path: Path) -> None:
    """R-1.4: a media id that could escape dest_root raises ValueError before any request."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    async with _make_client(handler) as client:
        with pytest.raises(ValueError, match="media_id"):
            await download_media(client, "../x", "photo", tmp_path, _limits())

    assert requests == []
    assert _entries(tmp_path) == []


async def test_unsupported_kind_is_rejected_without_a_request(tmp_path: Path) -> None:
    """R-1.11: a captcha has no byte cap, so it raises unsupported_type before any request."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    async with _make_client(handler) as client:
        with pytest.raises(MediaRejected) as raised:
            await download_media(client, MEDIA_ID, "captcha", tmp_path, _limits())

    assert raised.value.reason == "unsupported_type"
    assert requests == []
    assert _entries(tmp_path) == []
