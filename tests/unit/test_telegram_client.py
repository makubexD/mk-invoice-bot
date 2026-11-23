"""Tests for the Telegram Bot API client (R-1.7, R-1.11, R-9.16)."""

from __future__ import annotations

import contextlib
import io
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator

import httpx
import pytest

from invoicebot.channels.telegram.client import (
    NOT_SENT_STATUS,
    FileInfo,
    TelegramClient,
    TelegramError,
)
from invoicebot.logging import PROPAGATING_LOGGERS, configure_logging

# Split so the scanner does not read the synthetic value as a committed credential.
TOKEN = "123456" + ":ABC-fake-token"
FILE_ID = "AgACAgQAAx0Cfake-file-id"

Handler = Callable[[httpx.Request], httpx.Response]


@contextlib.asynccontextmanager
async def _make_client(handler: Handler) -> AsyncIterator[TelegramClient]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        yield TelegramClient(http, token=TOKEN)


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    """Route the root logger into a string stream and restore it afterwards."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_named = {
        name: (
            logging.getLogger(name).handlers[:],
            logging.getLogger(name).propagate,
            logging.getLogger(name).level,
        )
        for name in PROPAGATING_LOGGERS
    }
    stream = io.StringIO()
    try:
        yield stream
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        for name, (handlers, propagate, level) in saved_named.items():
            child = logging.getLogger(name)
            child.handlers[:] = handlers
            child.propagate = propagate
            child.setLevel(level)


async def test_send_message_posts_to_the_token_path_and_returns_ids() -> None:
    """R-1.7: send_message POSTs sendMessage and returns the chat id and message id."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42, "chat": {"id": -100}}})

    async with _make_client(handler) as client:
        ids = await client.send_message({"chat_id": -100, "text": "hola"})

    assert ids == (-100, 42)
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == f"/bot{TOKEN}/sendMessage"
    assert json.loads(requests[0].content) == {"chat_id": -100, "text": "hola"}


async def test_send_message_raises_with_the_retry_delay_on_429() -> None:
    """R-1.7: a 429 answer raises TelegramError with its status, code and retry_after."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry after 7",
                "parameters": {"retry_after": 7},
            },
        )

    async with _make_client(handler) as client:
        with pytest.raises(TelegramError) as raised:
            await client.send_message({"chat_id": 1, "text": "hola"})

    error = raised.value
    assert error.status == 429
    assert error.code == 429
    assert error.retry_after == 7


async def test_unauthorized_error_hides_the_token_from_text_and_repr() -> None:
    """R-9.16: a 401 error and the client repr hold no part of the bot token."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})

    async with _make_client(handler) as client:
        shown = repr(client)
        with pytest.raises(TelegramError) as raised:
            await client.send_message({"chat_id": 1, "text": "hola"})

    assert raised.value.status == 401
    assert TOKEN not in str(raised.value)
    assert "ABC-fake-token" not in str(raised.value)
    assert shown == "TelegramClient()"
    assert TOKEN not in shown


async def test_ok_false_with_status_200_raises() -> None:
    """R-1.7: an ok: false body is an error even when the HTTP status is 200."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
        )

    async with _make_client(handler) as client:
        with pytest.raises(TelegramError, match="chat not found") as raised:
            await client.send_message({"chat_id": 1, "text": "hola"})

    assert raised.value.code == 400


async def test_non_json_502_raises_without_a_code() -> None:
    """R-1.7: a non-JSON error body still raises TelegramError with the status and code None."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    async with _make_client(handler) as client:
        with pytest.raises(TelegramError) as raised:
            await client.send_message({"chat_id": 1, "text": "hola"})

    assert raised.value.status == 502
    assert raised.value.code is None


async def test_connect_error_becomes_a_status_0_error_without_a_chain() -> None:
    """R-9.16: a transport failure carries no cause or context that could hold the token."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _make_client(handler) as client:
        with pytest.raises(TelegramError, match="ConnectError") as raised:
            await client.send_message({"chat_id": 1, "text": "hola"})

    error = raised.value
    assert error.status == NOT_SENT_STATUS
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "connection refused" not in str(error)


async def test_get_file_returns_the_path_and_size() -> None:
    """R-1.11: get_file returns the download path and byte size of the getFile result."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"ok": True, "result": {"file_path": "photos/file_1.jpg", "file_size": 2048}}
        )

    async with _make_client(handler) as client:
        info = await client.get_file(FILE_ID)

    assert info == FileInfo(file_path="photos/file_1.jpg", size=2048)
    assert requests[0].url.path == f"/bot{TOKEN}/getFile"
    assert json.loads(requests[0].content) == {"file_id": FILE_ID}


@pytest.mark.parametrize("file_path", ["../x", "/x", ""])
async def test_get_file_rejects_an_unusable_file_path(file_path: str) -> None:
    """R-1.11: a returned file_path that is empty, absolute or holds .. raises TelegramError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"file_path": file_path}})

    async with _make_client(handler) as client:
        with pytest.raises(TelegramError):
            await client.get_file(FILE_ID)


async def test_unauthorized_error_logged_writes_no_part_of_the_token(
    log_stream: io.StringIO,
) -> None:
    """R-9.16: logging the 401 error writes no part of the bot token."""
    token_parts = TOKEN.split(":")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})

    configure_logging(stream=log_stream)
    async with _make_client(handler) as client:
        with pytest.raises(TelegramError):
            try:
                await client.send_message({"chat_id": 1, "text": "hola"})
            except TelegramError:
                logging.getLogger("invoicebot.telegram").exception("send failed")
                raise

    output = log_stream.getvalue()
    assert "send failed" in output
    assert TOKEN not in output
    for part in token_parts:
        assert part not in output


async def test_send_message_with_a_bad_result_is_not_reported_as_unsent() -> None:
    """R-1.7: an ok answer without ids raises with status 200, never NOT_SENT, so no retry resends it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    async with _make_client(handler) as client:
        with pytest.raises(TelegramError, match="no chat id") as raised:
            await client.send_message({"chat_id": 1, "text": "hola"})

    assert raised.value.status != NOT_SENT_STATUS


async def test_stream_file_yields_the_bytes_from_the_file_path() -> None:
    """R-1.11: stream_file downloads from /file/bot<token>/<file_path>."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"jpeg-bytes")

    async with _make_client(handler) as client, client.stream_file("photos/file_1.jpg") as chunks:
        body = b"".join([chunk async for chunk in chunks])

    assert body == b"jpeg-bytes"
    assert requests[0].url.path == f"/file/bot{TOKEN}/photos/file_1.jpg"


async def test_stream_file_raises_on_a_non_2xx_answer() -> None:
    """R-1.11: a failed download raises TelegramError with its status before any byte."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    async with _make_client(handler) as client:
        with pytest.raises(TelegramError) as raised:
            async with client.stream_file("photos/file_1.jpg"):
                pytest.fail("stream_file yielded on a 404")

    assert raised.value.status == 404


async def test_stream_file_connect_error_has_no_chain() -> None:
    """R-9.16: a download transport failure carries no cause or context that could hold the token."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _make_client(handler) as client:
        with pytest.raises(TelegramError, match="ConnectError") as raised:
            async with client.stream_file("photos/file_1.jpg"):
                pytest.fail("stream_file yielded on a connect error")

    assert raised.value.status == NOT_SENT_STATUS
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
