"""Telegram Bot API client: call methods, download files (R-1.7, R-1.11, R-9.16).

Every call uses the injected ``httpx.AsyncClient``; the bot token arrives
through the constructor, never from the environment. The token is part of the
request path, so errors carry only the HTTP status, Telegram's error code and
description, and the retry delay, never the URL or the token.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

API_BASE_URL = "https://api.telegram.org"
REQUEST_TIMEOUT_SECONDS = 30.0
GETFILE_MAX_BYTES = 20 * 1024 * 1024  # R-1.11: Telegram's own getFile limit
NOT_SENT_STATUS = 0  # the status of an error raised before any answer
OK_STATUS = 200  # the Bot API answers every ok: true call with 200, so a bad result keeps it
FILE_PATH_MAX_CHARS = 256
#: A file path Telegram may name: plain segments only, no character a shell or a URL parser treats specially.
FILE_PATH_PATTERN = r"[A-Za-z0-9_-]+(/[A-Za-z0-9_.-]+)*"
UNSAFE_PATH_SEGMENTS = frozenset({".", ".."})


class TelegramError(Exception):
    """A Bot API failure, with Telegram's error code, description and retry delay when present (R-9.16)."""

    def __init__(
        self,
        *,
        status: int,
        code: int | None = None,
        message: str | None = None,
        retry_after: int | None = None,
        description: str | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.retry_after = retry_after
        self.description = description
        super().__init__(_error_message(status, code, message))


@dataclass(frozen=True)
class FileInfo:
    """A Telegram file's downloadable path and size (R-1.11)."""

    file_path: str
    size: int | None


def _error_message(status: int, code: int | None, message: str | None) -> str:
    """Build the exception text from the status, Telegram's code and description only."""
    parts = [f"Telegram API error {status}"]
    if code is not None:
        parts.append(f"code {code}")
    if message:
        parts.append(message)
    return ": ".join(parts)


def _json_body(response: httpx.Response) -> object:
    """Return the decoded JSON body, or None when the body is not JSON."""
    try:
        return response.json()
    except ValueError:
        return None


def _positive_int(value: object) -> int | None:
    """Return ``value`` when it is a positive int that is not a bool."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _int_field(value: object) -> int | None:
    """Return ``value`` when it is an int that is not a bool."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _retry_after(body: dict[str, Any]) -> int | None:
    """Read ``parameters.retry_after`` from an error body, when it is a positive int."""
    parameters = body.get("parameters")
    if not isinstance(parameters, dict):
        return None
    return _positive_int(parameters.get("retry_after"))


def _error_for(response: httpx.Response, body: object) -> TelegramError:
    """Turn an unsuccessful or ``ok: false`` answer into ``TelegramError``."""
    code = None
    message = None
    description = None
    retry_after = None
    if isinstance(body, dict):
        code = _int_field(body.get("error_code"))
        raw_description = body.get("description")
        description = raw_description if isinstance(raw_description, str) else None
        message = description
        retry_after = _retry_after(body)
    return TelegramError(
        status=response.status_code,
        code=code,
        message=message,
        retry_after=retry_after,
        description=description,
    )


def _result_of(response: httpx.Response) -> object:
    """Return the ``result`` of an ``ok: true`` answer, or raise ``TelegramError``."""
    body = _json_body(response)
    if response.is_success and isinstance(body, dict) and body.get("ok") is True:
        return body.get("result")
    raise _error_for(response, body)


def _is_safe_file_path(file_path: str) -> bool:
    """True when ``file_path`` matches the allow-list, fits the cap and holds no dot segment."""
    if len(file_path) > FILE_PATH_MAX_CHARS:
        return False
    if re.fullmatch(FILE_PATH_PATTERN, file_path) is None:
        return False
    return all(segment not in UNSAFE_PATH_SEGMENTS for segment in file_path.split("/"))


def _file_info(result: object) -> FileInfo:
    """Map a ``getFile`` result to ``FileInfo``, rejecting an unusable ``file_path`` (R-1.11)."""
    if not isinstance(result, dict):
        raise TelegramError(status=OK_STATUS, message="getFile result is not an object")
    file_path = result.get("file_path")
    if not isinstance(file_path, str) or not _is_safe_file_path(file_path):
        raise TelegramError(status=OK_STATUS, message="getFile result has an unusable file_path")
    return FileInfo(file_path=file_path, size=_int_field(result.get("file_size")))


class TelegramClient:
    """The Bot API calls the Telegram channel needs (R-1.7, R-1.11)."""

    def __init__(self, http: httpx.AsyncClient, *, token: str) -> None:
        self._http = http
        self._token = token

    def __repr__(self) -> str:
        """Never show the token (R-9.16)."""
        return "TelegramClient()"

    async def call(self, method: str, payload: dict[str, Any]) -> object:
        """POST ``payload`` to a Bot API method and return its ``result``.

        Raises ``TelegramError`` when the answer is not ``ok: true``. A transport
        failure becomes ``TelegramError(status=NOT_SENT_STATUS)`` naming only the
        exception's class, with no cause or context that could carry the URL (R-9.16).
        """
        url = f"{API_BASE_URL}/bot{self._token}/{method}"
        answer: httpx.Response | TelegramError
        try:
            answer = await self._http.post(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:
            answer = TelegramError(status=NOT_SENT_STATUS, message=type(exc).__name__)
        if isinstance(answer, TelegramError):
            raise answer
        return _result_of(answer)

    async def send_message(self, payload: dict[str, Any]) -> tuple[int, int]:
        """Send a message and return its ``(chat id, message id)`` (R-1.7)."""
        result = await self.call("sendMessage", payload)
        if isinstance(result, dict):
            chat = result.get("chat")
            message_id = _int_field(result.get("message_id"))
            chat_id = _int_field(chat.get("id")) if isinstance(chat, dict) else None
            if chat_id is not None and message_id is not None:
                return chat_id, message_id
        # The message went out, so this must not read as NOT_SENT and invite a duplicate retry.
        raise TelegramError(status=OK_STATUS, message="sendMessage result has no chat id or message id")

    async def answer_callback_query(self, callback_query_id: str) -> None:
        """Acknowledge a button press so the client stops showing it as pending (D177)."""
        await self.call("answerCallbackQuery", {"callback_query_id": callback_query_id})

    async def get_file(self, file_id: str) -> FileInfo:
        """Resolve a media ``file_id`` to its download path and size (R-1.11)."""
        return _file_info(await self.call("getFile", {"file_id": file_id}))

    @contextlib.asynccontextmanager
    async def stream_file(self, file_path: str) -> AsyncIterator[AsyncIterator[bytes]]:
        """Stream a file's bytes from the bot file endpoint (R-1.11).

        Raises ``TelegramError`` when the download answers with a non-2xx status,
        or with ``NOT_SENT_STATUS`` and no chain when the request cannot be made
        or the body fails mid-stream, since the URL holds the token (R-9.16).
        """
        url = f"{API_BASE_URL}/file/bot{self._token}/{file_path}"
        async with contextlib.AsyncExitStack() as stack:
            response: httpx.Response | TelegramError
            try:
                response = await stack.enter_async_context(
                    self._http.stream("GET", url, timeout=REQUEST_TIMEOUT_SECONDS)
                )
            except httpx.HTTPError as exc:
                response = TelegramError(status=NOT_SENT_STATUS, message=type(exc).__name__)
            if isinstance(response, TelegramError):
                raise response
            if not response.is_success:
                raise TelegramError(status=response.status_code)
            yield _stream_chunks(response)


async def _stream_chunks(response: httpx.Response) -> AsyncIterator[bytes]:
    """Yield ``response`` bytes, mapping a mid-stream transport failure to a chainless error (R-9.16).

    The error is raised outside the ``except`` block so neither ``__cause__`` nor
    ``__context__`` can carry the request URL, which holds the token.
    """
    error: TelegramError | None = None
    try:
        async for chunk in response.aiter_bytes():
            yield chunk
    except httpx.HTTPError as exc:
        error = TelegramError(status=NOT_SENT_STATUS, message=type(exc).__name__)
    if error is not None:
        raise error
