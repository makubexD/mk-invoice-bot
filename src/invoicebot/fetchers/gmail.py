"""Gmail mail reader for the inbox fetcher (R-5.2, R-6.5, D135).

Plain ``httpx`` over Gmail's REST API; tokens come from an injected
:class:`TokenSource` and every request has the configured timeout. Ids are
checked against :data:`_GMAIL_ID` before they go into a URL path, so a hostile
or malformed id never changes the request. Errors carry the HTTP status only,
never a URL, a path, a body or the search term (R-9.16).
"""

from __future__ import annotations

import base64
import contextlib
import json
import re
import urllib.parse
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx

from invoicebot.fetchers.base import MailboxError
from invoicebot.fetchers.graph_mail import MailAttachment, MailMessage
from invoicebot.sinks.base import TokenSource
from invoicebot.sinks.google_api import GoogleError, auth_headers, raise_google_error

#: The Gmail endpoint; the only host this client talks to (D135).
GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me"

#: The only characters a Gmail message or attachment id may contain; real
#: attachment ids are often several hundred characters long (D135).
_GMAIL_ID = re.compile(r"^[A-Za-z0-9_-]{1,2048}$")

#: How deep a message's nested MIME parts are walked (D135).
_MAX_PART_DEPTH = 10

#: Room for the JSON envelope around one attachment's base64url body (D135).
_JSON_OVERHEAD_BYTES = 4096

#: The query Gmail receives: only mails carrying an invoice's files (D135).
_QUERY = 'has:attachment (filename:xml OR filename:pdf) "{term}"'

#: The fields ``search`` needs: the message id and when it arrived (D135).
_METADATA_FORMAT = "metadata"

#: The format ``attachments`` needs: the message's full MIME tree (D135).
_FULL_FORMAT = "full"


def _encoded_id(value: str) -> str:
    """Return ``value`` percent-encoded for a URL path; raise if it is not a Gmail id.

    Raises:
        GoogleError: the value is not a plausible Gmail id, so no request is sent.
    """
    if _GMAIL_ID.fullmatch(value) is None:
        raise GoogleError()
    return urllib.parse.quote(value, safe="")


def _json_object(data: bytes) -> dict[str, object]:
    """Return ``data`` parsed as a JSON object.

    Raises:
        GoogleError: the bytes are not JSON or are not an object.
    """
    try:
        body: object = json.loads(data)
    except ValueError as err:
        raise GoogleError() from err
    if not isinstance(body, dict):
        raise GoogleError()
    return body


def _json_dict(response: httpx.Response) -> dict[str, object]:
    """Return the response's JSON object body.

    Raises:
        GoogleError: the body is not JSON or is not an object.
    """
    return _json_object(response.content)


def _string(value: object) -> str:
    """Return ``value`` when it is a string.

    Raises:
        GoogleError: the field is missing or has the wrong type.
    """
    if not isinstance(value, str):
        raise GoogleError()
    return value


def _integer(value: object) -> int:
    """Return ``value`` when it is an integer that is not a bool.

    Raises:
        GoogleError: the field is missing or has the wrong type.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise GoogleError()
    return value


def _hits(body: dict[str, object]) -> list[object]:
    """Return the ``messages`` entries of a Gmail list body.

    Raises:
        GoogleError: the body carries something other than a list of messages.
    """
    value = body.get("messages")
    if value is None:
        return []
    if not isinstance(value, list):
        raise GoogleError()
    return value


def _received_at(body: dict[str, object]) -> datetime:
    """Return the message's ``internalDate`` (ms since the epoch) as aware UTC.

    Raises:
        GoogleError: the field is missing or cannot be read as a timestamp.
    """
    value = _string(body.get("internalDate"))
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (ValueError, OverflowError, OSError) as err:
        raise GoogleError() from err


def _attachment(part: dict[str, object]) -> MailAttachment | None:
    """Return the file attachment a MIME part carries, or None for another part.

    Raises:
        GoogleError: the part is malformed or its id is not a Gmail id.
    """
    if "filename" not in part:
        return None
    name = part.get("filename")
    body = part.get("body")
    if not isinstance(name, str) or not isinstance(body, dict):
        raise GoogleError()
    if not name:
        return None
    attachment_id = body.get("attachmentId")
    if attachment_id is None:
        return None
    if not isinstance(attachment_id, str):
        raise GoogleError()
    _encoded_id(attachment_id)
    return MailAttachment(id=attachment_id, name=name, size=_integer(body.get("size")))


def _parts(payload: dict[str, object], depth: int) -> list[MailAttachment]:
    """Return the file attachments of ``payload`` and its nested parts (D135).

    Raises:
        GoogleError: a part is malformed or the tree is deeper than the cap.
    """
    if depth > _MAX_PART_DEPTH:
        return []
    found: list[MailAttachment] = []
    attachment = _attachment(payload)
    if attachment is not None:
        found.append(attachment)
    children = payload.get("parts")
    if children is None:
        return found
    if not isinstance(children, list):
        raise GoogleError()
    for child in children:
        if not isinstance(child, dict):
            raise GoogleError()
        found.extend(_parts(child, depth + 1))
    return found


def _decode(body: dict[str, object]) -> bytes:
    """Return the attachment's bytes from its unpadded base64url ``data``.

    Raises:
        GoogleError: the field is missing, is not a string or is not base64url.
    """
    data = _string(body.get("data"))
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except ValueError as err:
        raise GoogleError() from err


@contextlib.asynccontextmanager
async def _mailbox_errors() -> AsyncIterator[None]:
    """Re-raise Google's failures as MailboxError so callers see no provider (R-5.2)."""
    try:
        yield
    except GoogleError as err:
        raise MailboxError(str(err)) from err


class _BodyTooLarge(Exception):
    """A streamed response passed the caller's byte cap."""


class GmailMailbox:
    """One Google account's mailbox, over Gmail and an injected client (R-5.2, D135).

    Long-lived: one instance per account the inbox fetcher reads.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        tokens: TokenSource,
        account: str,
        timeout_s: float,
        max_message_bytes: int,
    ) -> None:
        self._http = http
        self._tokens = tokens
        self._account = account
        self._timeout_s = timeout_s
        self._max_message_bytes = max_message_bytes

    async def search(self, term: str, top: int) -> list[MailMessage]:
        """Return the ``top`` newest messages with attachments matching ``term`` (R-5.2).

        ``term`` has its ``"`` and ``\\`` removed; Gmail receives the rest inside
        one quoted phrase next to the attachment filter. The messages come back
        newest first by ``internalDate``.

        Raises:
            MailboxError: Gmail answers with an error or a malformed body.
        """
        async with _mailbox_errors():
            clean = term.replace('"', "").replace("\\", "")
            headers = auth_headers(await self._tokens.token())
            response = await self._http.get(
                f"{GMAIL_URL}/messages",
                params={"q": _QUERY.format(term=clean), "maxResults": str(top)},
                headers=headers,
                timeout=self._timeout_s,
            )
            raise_google_error(response, self._account)
            messages = [await self._message(hit, headers) for hit in _hits(_json_dict(response))]
            return sorted(messages, key=lambda message: message.received_at, reverse=True)

    async def attachments(self, message_id: str) -> list[MailAttachment]:
        """Return the file attachments of ``message_id`` (R-5.2).

        The full-message response is streamed and refused once its body passes
        :attr:`_max_message_bytes`, before any JSON is decoded.

        Raises:
            MailboxError: ``message_id`` is not a Gmail id (before any request),
                the message is larger than the configured cap, or Gmail answers
                with an error or a malformed body.
        """
        async with _mailbox_errors():
            message_path = _encoded_id(message_id)
            headers = auth_headers(await self._tokens.token())
            try:
                body = await self._read_capped(
                    f"{GMAIL_URL}/messages/{message_path}",
                    {"format": _FULL_FORMAT},
                    headers,
                    self._max_message_bytes,
                )
            except _BodyTooLarge as err:
                raise MailboxError(f"message exceeds {self._max_message_bytes} bytes") from err
            payload = _json_object(body).get("payload")
            if not isinstance(payload, dict):
                raise GoogleError()
            return _parts(payload, 0)

    async def download(self, message_id: str, attachment_id: str, max_bytes: int) -> bytes:
        """Return the bytes of one attachment, refusing anything over ``max_bytes`` (R-5.2).

        The response is streamed and refused once its raw body passes the room
        ``max_bytes`` needs as base64url plus the JSON envelope. The listed size
        is not looked up: the fetcher checks it before calling here, and Gmail
        may return a different ``attachmentId`` on each ``messages.get`` (D135).

        Raises:
            MailboxError: an id is not a Gmail id (before any request) or Gmail
                answers with an error or a malformed body.
            ValueError: the attachment is larger than ``max_bytes``.
        """
        async with _mailbox_errors():
            message_path = _encoded_id(message_id)
            attachment_path = _encoded_id(attachment_id)
            url = f"{GMAIL_URL}/messages/{message_path}/attachments/{attachment_path}"
            headers = auth_headers(await self._tokens.token())
            raw_cap = max_bytes * 4 // 3 + _JSON_OVERHEAD_BYTES
            try:
                body = await self._read_capped(url, None, headers, raw_cap)
            except _BodyTooLarge as err:
                raise ValueError(f"attachment exceeds {max_bytes} bytes") from err
            data = _decode(_json_object(body))
            if len(data) > max_bytes:
                raise ValueError(f"attachment exceeds {max_bytes} bytes")
            return data

    async def _read_capped(
        self,
        url: str,
        params: dict[str, str] | None,
        headers: dict[str, str],
        cap: int,
    ) -> bytes:
        """Stream the GET response and return its body, refusing one over ``cap`` bytes.

        Raises:
            GoogleError: Gmail answers with an error.
            _BodyTooLarge: the received body passed ``cap`` bytes.
        """
        chunks: list[bytes] = []
        total = 0
        async with self._http.stream(
            "GET", url, params=params, headers=headers, timeout=self._timeout_s
        ) as response:
            raise_google_error(response, self._account)
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > cap:
                    raise _BodyTooLarge()
                chunks.append(chunk)
        return b"".join(chunks)

    async def _message(self, item: object, headers: dict[str, str]) -> MailMessage:
        """Return one list hit with its arrival time, asking for metadata only.

        Raises:
            MailboxError: the hit is malformed, or Gmail answers with an error.
        """
        if not isinstance(item, dict):
            raise GoogleError()
        message_id = _string(item.get("id"))
        message_path = _encoded_id(message_id)
        response = await self._http.get(
            f"{GMAIL_URL}/messages/{message_path}",
            params={"format": _METADATA_FORMAT},
            headers=headers,
            timeout=self._timeout_s,
        )
        raise_google_error(response, self._account)
        return MailMessage(id=message_id, received_at=_received_at(_json_dict(response)))
