"""WhatsApp Graph API client: send messages and fetch media (R-3.2, R-1.4, R-1.10).

Every call uses the injected ``httpx.AsyncClient``; the token, the phone number
id and the API version arrive through the constructor, never from the
environment. Errors carry Meta's status and error code but never the request
URL, headers or token, so they are safe to log.
"""

from __future__ import annotations

import contextlib
import re
import urllib.parse
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

GRAPH_API_BASE_URL = "https://graph.facebook.com"
REQUEST_TIMEOUT_SECONDS = 30.0
WINDOW_EXPIRED_CODE = 131047
ALLOWED_MEDIA_HOST_SUFFIXES = (".facebook.com", ".fbcdn.net", ".whatsapp.net", ".fbsbx.com")
NOT_SENT_STATUS = 0  # the status of an error raised before any request
_MEDIA_ID_PATTERN = r"[0-9A-Za-z_-]{1,128}"


class WhatsAppError(Exception):
    """A Graph API failure, with Meta's error code when the body carries one."""

    def __init__(self, *, status: int, code: int | None = None, message: str | None = None) -> None:
        self.status = status
        self.code = code
        super().__init__(_error_message(status, code, message))


class WindowExpired(WhatsAppError):
    """The 24-hour customer service window is closed (Meta code 131047, R-1.10)."""


@dataclass(frozen=True)
class MediaInfo:
    """A media item's download URL and integrity metadata (R-1.4)."""

    url: str
    mime: str
    size: int
    sha256: str | None


def _error_message(status: int, code: int | None, message: str | None) -> str:
    """Build the exception text from the status, the Meta code and the Meta message only."""
    parts = [f"WhatsApp API error {status}"]
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


def _meta_error(response: httpx.Response) -> tuple[int | None, str | None]:
    """Read ``error.code`` and ``error.message`` from a JSON body, when both are usable."""
    body = _json_body(response)
    if not isinstance(body, dict):
        return None, None
    error = body.get("error")
    if not isinstance(error, dict):
        return None, None
    code = error.get("code")
    if not isinstance(code, int) or isinstance(code, bool):
        code = None
    message = error.get("message")
    if not isinstance(message, str):
        message = None
    return code, message


def _error_for(response: httpx.Response) -> WhatsAppError:
    """Turn a non-2xx response into the matching error class."""
    code, message = _meta_error(response)
    error_class = WindowExpired if code == WINDOW_EXPIRED_CODE else WhatsAppError
    return error_class(status=response.status_code, code=code, message=message)


def _sent_message_id(body: object) -> str | None:
    """Return ``messages[0].id`` from a send answer, or None when it is absent."""
    if not isinstance(body, dict):
        return None
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    first = messages[0]
    if not isinstance(first, dict):
        return None
    message_id = first.get("id")
    return message_id if isinstance(message_id, str) and message_id else None


def _media_info(body: object) -> MediaInfo | None:
    """Map a media-info answer to ``MediaInfo``, or None when a required field is missing."""
    if not isinstance(body, dict):
        return None
    url = body.get("url")
    mime = body.get("mime_type")
    raw_size = body.get("file_size")
    sha256 = body.get("sha256")
    if not isinstance(url, str) or not isinstance(mime, str):
        return None
    if sha256 is not None and not isinstance(sha256, str):
        return None
    if not isinstance(raw_size, (int, str)):
        return None
    try:
        size = int(raw_size)
    except ValueError:
        return None
    return MediaInfo(url=url, mime=mime, size=size, sha256=sha256)


def _check_media_url(url: str) -> None:
    """Reject a media URL that is not HTTPS on one of Meta's hosts, before any request."""
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host.endswith(ALLOWED_MEDIA_HOST_SUFFIXES):
        raise WhatsAppError(status=NOT_SENT_STATUS)


class WhatsAppClient:
    """The Graph API calls the WhatsApp channel needs (R-3.2, R-1.4)."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        phone_number_id: str,
        graph_api_version: str,
        token: str,
    ) -> None:
        self._http = http
        self._phone_number_id = phone_number_id
        self._base_url = f"{GRAPH_API_BASE_URL}/{graph_api_version}"
        self._token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def send(self, payload: dict[str, Any]) -> str:
        """POST a message payload and return Meta's message id (R-3.2).

        Raises ``WindowExpired`` when Meta reports the closed 24-hour window and
        ``WhatsAppError`` for any other failure or a missing message id.
        """
        response = await self._http.post(
            f"{self._base_url}/{self._phone_number_id}/messages",
            json=payload,
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if not response.is_success:
            raise _error_for(response)
        message_id = _sent_message_id(_json_body(response))
        if message_id is None:
            raise WhatsAppError(status=response.status_code)
        return message_id

    async def media_info(self, media_id: str) -> MediaInfo:
        """GET a media item's URL and integrity metadata (R-1.4).

        The id comes from an untrusted webhook, so one that could change the
        request path is rejected with ``WhatsAppError`` before any request.
        """
        if re.fullmatch(_MEDIA_ID_PATTERN, media_id) is None:
            raise WhatsAppError(status=NOT_SENT_STATUS)
        response = await self._http.get(
            f"{self._base_url}/{media_id}",
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if not response.is_success:
            raise _error_for(response)
        info = _media_info(_json_body(response))
        if info is None:
            raise WhatsAppError(status=response.status_code)
        return info

    @contextlib.asynccontextmanager
    async def stream_media(self, url: str) -> AsyncIterator[AsyncIterator[bytes]]:
        """Stream a media URL's bytes, rejecting a non-Meta host before any request (R-1.4).

        Raises ``WhatsAppError`` when the URL is not HTTPS on an allowed host or
        when the download answers with a status other than 200.
        """
        _check_media_url(url)
        async with self._http.stream(
            "GET",
            url,
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as response:
            if response.status_code != 200:
                raise WhatsAppError(status=response.status_code)
            yield response.aiter_bytes()
