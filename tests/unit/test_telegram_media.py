"""Tests for downloading Telegram media to disk (R-1.4, R-1.11, BR-E07)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest

from invoicebot.channels.base import MediaTransient, MediaUnusable
from invoicebot.channels.media_limits import MediaRejected
from invoicebot.channels.telegram import media as telegram_media
from invoicebot.channels.telegram.client import (
    GETFILE_MAX_BYTES,
    NOT_SENT_STATUS,
    TelegramClient,
    TelegramError,
)
from invoicebot.channels.telegram.media import download_media
from invoicebot.config.models import MediaLimits, MediaMaxBytes

# Split so the scanner does not read the synthetic value as a committed credential.
TOKEN = "123456" + ":ABC-fake-token"
FILE_ID = "AgACAgQAAx0Cfake-file-id"
PDF_PATH = "documents/file_1.pdf"
PHOTO_PATH = "photos/file_1.jpg"

Handler = Callable[[httpx.Request], httpx.Response]


class _ReadErrorAfterFirstChunk(httpx.AsyncByteStream):
    """Yield one chunk, then fail as a dropped connection does mid-body."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"%PDF-1.7\n"
        raise httpx.ReadError("connection reset")


@contextlib.asynccontextmanager
async def _make_client(handler: Handler) -> AsyncIterator[TelegramClient]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        yield TelegramClient(http, token=TOKEN)


def _limits(*, image: int = 5_000_000, pdf: int = 10_000_000) -> MediaLimits:
    """Limits with synthetic byte caps for each attachment kind."""
    return MediaLimits(
        max_megapixels=40,
        max_bytes=MediaMaxBytes(image=image, pdf=pdf, xml=1_000_000, zip=2_000_000),
        zip_max_entries=20,
        zip_max_ratio=50,
    )


def _get_file_response(file_path: str, size: int | None) -> httpx.Response:
    result: dict[str, object] = {"file_path": file_path}
    if size is not None:
        result["file_size"] = size
    return httpx.Response(200, json={"ok": True, "result": result})


def _is_file_request(request: httpx.Request) -> bool:
    return request.url.path.startswith("/file/")


def _entries(root: Path) -> list[Path]:
    """List a directory's entries from a sync helper, so an async test may call it."""
    return list(root.iterdir())


async def test_download_media_writes_a_pdf_with_its_hash_and_size(tmp_path: Path) -> None:
    """R-1.4: a small PDF is written with the SHA-256, size and mime of the bytes received."""
    body = b"%PDF-1.7\n" + b"\x00" * 50
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if _is_file_request(request):
            return httpx.Response(200, content=body)
        return _get_file_response(PDF_PATH, len(body))

    async with _make_client(handler) as client:
        media = await download_media(client, FILE_ID, "pdf", tmp_path, _limits())

    assert media.path.name == "file"
    assert media.path.parent.parent == tmp_path
    assert media.path.parent.name.startswith(f"{FILE_ID}-")
    assert len(media.path.parent.name) == len(FILE_ID) + 1 + 32  # file id, a dash and a uuid4 hex
    assert media.path.read_bytes() == body
    assert media.size == len(body)
    assert media.sha256 == hashlib.sha256(body).hexdigest()
    assert media.mime == "application/pdf"
    assert [_is_file_request(request) for request in requests] == [False, True]


async def test_file_size_over_the_cap_is_rejected_before_downloading(tmp_path: Path) -> None:
    """R-1.11: a 21 MB file_size raises too_large and no file request is made.

    The configured cap is above it, so Telegram's own getFile limit is what binds.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _get_file_response(PDF_PATH, GETFILE_MAX_BYTES + 1024 * 1024)

    async with _make_client(handler) as client:
        with pytest.raises(MediaRejected) as raised:
            await download_media(client, FILE_ID, "pdf", tmp_path, _limits(pdf=50_000_000))

    assert raised.value.reason == "too_large"
    assert not any(_is_file_request(request) for request in requests)
    assert _entries(tmp_path) == []


async def test_body_over_the_cap_is_rejected_while_streaming_and_leaves_nothing(
    tmp_path: Path,
) -> None:
    """R-1.11: without a file_size, a body longer than the cap raises too_large and leaves nothing."""

    async def chunks_to_send() -> AsyncIterator[bytes]:
        yield b"a" * 60
        yield b"b" * 60

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_file_request(request):
            return httpx.Response(200, content=chunks_to_send())
        return _get_file_response(PDF_PATH, None)

    async with _make_client(handler) as client:
        with pytest.raises(MediaRejected) as raised:
            await download_media(client, FILE_ID, "pdf", tmp_path, _limits(pdf=100))

    assert raised.value.reason == "too_large"
    assert _entries(tmp_path) == []


@pytest.mark.parametrize(
    ("status", "neutral_error"),
    [(502, MediaTransient), (400, MediaUnusable)],
)
async def test_get_file_failure_maps_to_the_neutral_media_error(
    tmp_path: Path,
    status: int,
    neutral_error: type[MediaTransient] | type[MediaUnusable],
) -> None:
    """R-1.11: a 502 on getFile raises MediaTransient and a 400 raises MediaUnusable.

    Either neutral error keeps the ``TelegramError`` as its ``__cause__``.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"ok": False, "error_code": status})

    async with _make_client(handler) as client:
        with pytest.raises(neutral_error) as raised:
            await download_media(client, FILE_ID, "pdf", tmp_path, _limits())

    cause = raised.value.__cause__
    assert isinstance(cause, TelegramError)
    assert cause.status == status
    assert _entries(tmp_path) == []


async def test_photo_with_a_bad_header_is_rejected_and_leaves_nothing(tmp_path: Path) -> None:
    """BR-E07: a photo whose header is not PNG or JPEG is rejected and its directory removed."""
    body = b"not an image at all"

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_file_request(request):
            return httpx.Response(200, content=body)
        return _get_file_response(PHOTO_PATH, len(body))

    async with _make_client(handler) as client:
        with pytest.raises(MediaRejected) as raised:
            await download_media(client, FILE_ID, "photo", tmp_path, _limits())

    assert raised.value.reason == "unsupported_type"
    assert _entries(tmp_path) == []


async def test_file_id_outside_the_plain_identifier_pattern_is_rejected(tmp_path: Path) -> None:
    """R-1.4: a file id that could escape dest_root raises ValueError before any request."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {}})

    async with _make_client(handler) as client:
        with pytest.raises(ValueError, match="file_id"):
            await download_media(client, "a/b", "pdf", tmp_path, _limits())

    assert requests == []
    assert _entries(tmp_path) == []


async def test_read_error_after_the_first_chunk_maps_to_media_transient(
    tmp_path: Path,
) -> None:
    """R-9.16: a body failure after the first chunk becomes MediaTransient whose cause has no chain."""

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_file_request(request):
            return httpx.Response(200, stream=_ReadErrorAfterFirstChunk())
        return _get_file_response(PDF_PATH, None)

    async with _make_client(handler) as client:
        with pytest.raises(MediaTransient) as raised:
            await download_media(client, FILE_ID, "pdf", tmp_path, _limits())

    cause = raised.value.__cause__
    assert isinstance(cause, TelegramError)
    assert cause.status == NOT_SENT_STATUS
    assert cause.__cause__ is None
    assert cause.__context__ is None
    assert TOKEN not in str(raised.value)
    assert TOKEN not in str(cause)
    assert _entries(tmp_path) == []


async def test_file_too_big_answer_is_rejected_as_too_large(tmp_path: Path) -> None:
    """R-1.11: a 400 "file is too big" getFile answer is MediaRejected, not MediaUnusable."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            400,
            json={"ok": False, "error_code": 400, "description": "Bad Request: file is too big"},
        )

    async with _make_client(handler) as client:
        with pytest.raises(MediaRejected) as raised:
            await download_media(client, FILE_ID, "pdf", tmp_path, _limits())

    assert raised.value.reason == "too_large"
    assert not any(_is_file_request(request) for request in requests)
    assert _entries(tmp_path) == []


async def test_two_downloads_of_the_same_file_id_get_their_own_directory(
    tmp_path: Path,
) -> None:
    """R-1.11: Telegram reuses a file_id for a re-sent file, so each download keeps its own folder."""
    body = b"%PDF-1.7\n" + b"\x00" * 20

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_file_request(request):
            return httpx.Response(200, content=body)
        return _get_file_response(PDF_PATH, len(body))

    async with _make_client(handler) as client:
        first = await download_media(client, FILE_ID, "pdf", tmp_path, _limits())
        second = await download_media(client, FILE_ID, "pdf", tmp_path, _limits())

    assert first.path.parent != second.path.parent
    assert first.path.parent.parent == tmp_path
    assert second.path.parent.parent == tmp_path
    assert first.path.read_bytes() == body
    assert second.path.read_bytes() == body


async def test_get_file_past_the_download_deadline_is_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-1.11: a getFile that outlives the download deadline raises MediaTransient and leaves nothing."""
    monkeypatch.setattr(telegram_media, "DOWNLOAD_TIMEOUT_S", 0.01)

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return _get_file_response(PDF_PATH, 0)

    async with _make_client(handler) as client:
        with pytest.raises(MediaTransient):
            await download_media(client, FILE_ID, "pdf", tmp_path, _limits())

    assert _entries(tmp_path) == []
