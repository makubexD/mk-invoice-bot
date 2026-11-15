"""Microsoft Graph mail reader for the inbox fetcher (R-5.2, R-6.5).

Plain ``httpx`` over Graph's REST API; tokens come from an injected
:class:`TokenSource` and every request has the configured timeout. Ids Graph
returns are checked against :data:`_GRAPH_ID` before they go into a URL path, so
a hostile or malformed id never changes the request. Errors carry the HTTP
status only, never a URL, a path or a body (R-9.16).
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime

import httpx

from invoicebot.sinks.base import TokenSource
from invoicebot.sinks.onedrive import (
    GRAPH_BASE_URL,
    NOT_SENT_STATUS,
    GraphError,
    auth_headers,
    raise_graph_error,
)

#: The only characters a Graph message or attachment id may contain.
_GRAPH_ID = re.compile(r"^[A-Za-z0-9_=+/-]+$")

#: The only attachment type that carries a file; every other kind is skipped.
_FILE_ATTACHMENT = "#microsoft.graph.fileAttachment"

#: The message fields ``search`` asks Graph for.
_MESSAGE_SELECT = "id,receivedDateTime,hasAttachments"

#: The attachment fields ``attachments`` asks Graph for.
_ATTACHMENT_SELECT = "id,name,size,contentType"


@dataclass(frozen=True)
class MailMessage:
    """One mailbox message that has attachments (R-5.2)."""

    id: str
    received_at: datetime


@dataclass(frozen=True)
class MailAttachment:
    """One file attached to a message (R-5.2)."""

    id: str
    name: str
    size: int


def _encoded_id(value: str) -> str:
    """Return ``value`` percent-encoded for a URL path; raise if it is not a Graph id.

    Raises:
        GraphError: the value is not a plausible Graph id, so no request is sent.
    """
    if _GRAPH_ID.fullmatch(value) is None:
        raise GraphError(NOT_SENT_STATUS)
    return urllib.parse.quote(value, safe="")


def _json_dict(response: httpx.Response) -> dict[str, object]:
    """Return the response's JSON object body.

    Raises:
        GraphError: the body is not JSON or is not an object.
    """
    try:
        body: object = response.json()
    except ValueError as err:
        raise GraphError(NOT_SENT_STATUS) from err
    if not isinstance(body, dict):
        raise GraphError(NOT_SENT_STATUS)
    return body


def _items(body: dict[str, object]) -> list[object]:
    """Return the ``value`` entries of a Graph collection body.

    Raises:
        GraphError: the body carries no ``value`` list.
    """
    value = body.get("value")
    if not isinstance(value, list):
        raise GraphError(NOT_SENT_STATUS)
    return value


def _message(item: object) -> MailMessage | None:
    """Return the message for one search entry, or None when it has no attachments.

    Raises:
        GraphError: the entry is malformed or one of its fields has the wrong type.
    """
    if not isinstance(item, dict):
        raise GraphError(NOT_SENT_STATUS)
    if item.get("hasAttachments") is not True:
        return None
    message_id = item.get("id")
    received = item.get("receivedDateTime")
    if not isinstance(message_id, str) or not isinstance(received, str):
        raise GraphError(NOT_SENT_STATUS)
    _encoded_id(message_id)
    try:
        received_at = datetime.fromisoformat(received)
    except ValueError as err:
        raise GraphError(NOT_SENT_STATUS) from err
    return MailMessage(id=message_id, received_at=received_at)


def _attachment(item: object) -> MailAttachment | None:
    """Return the file attachment for one listing entry, or None for another kind.

    Raises:
        GraphError: the entry is malformed or one of its fields has the wrong type.
    """
    if not isinstance(item, dict):
        raise GraphError(NOT_SENT_STATUS)
    if item.get("@odata.type") != _FILE_ATTACHMENT:
        return None
    attachment_id = item.get("id")
    name = item.get("name")
    size = item.get("size")
    if (
        not isinstance(attachment_id, str)
        or not isinstance(name, str)
        or isinstance(size, bool)
        or not isinstance(size, int)
    ):
        raise GraphError(NOT_SENT_STATUS)
    _encoded_id(attachment_id)
    return MailAttachment(id=attachment_id, name=name, size=size)


class GraphMailbox:
    """One Microsoft account's mailbox, over Graph and an injected HTTP client (R-5.2).

    Long-lived: one instance per account the inbox fetcher reads.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        tokens: TokenSource,
        account: str,
        timeout_s: float,
    ) -> None:
        self._http = http
        self._tokens = tokens
        self._account = account
        self._timeout_s = timeout_s

    async def search(self, term: str, top: int) -> list[MailMessage]:
        """Return the messages with attachments whose contents match ``term`` (R-5.2).

        ``term`` has its ``"`` and ``\\`` removed; Graph receives the rest as one
        quoted ``$search`` phrase. Only the first ``top`` messages are looked at.

        Raises:
            GraphError: Graph answers with an error or a malformed body.
        """
        clean = term.replace('"', "").replace("\\", "")
        params = {
            "$search": f'"{clean}"',
            "$top": str(top),
            "$select": _MESSAGE_SELECT,
        }
        token = await self._tokens.token()
        response = await self._http.get(
            f"{GRAPH_BASE_URL}/me/messages",
            params=params,
            headers={**auth_headers(token), "ConsistencyLevel": "eventual"},
            timeout=self._timeout_s,
        )
        raise_graph_error(response, self._account)
        messages = (_message(item) for item in _items(_json_dict(response)))
        return [message for message in messages if message is not None]

    async def attachments(self, message_id: str) -> list[MailAttachment]:
        """Return the file attachments of ``message_id`` (R-5.2).

        Raises:
            GraphError: ``message_id`` is not a Graph id (before any request), or
                Graph answers with an error or a malformed body.
        """
        message_path = _encoded_id(message_id)
        token = await self._tokens.token()
        response = await self._http.get(
            f"{GRAPH_BASE_URL}/me/messages/{message_path}/attachments",
            params={"$select": _ATTACHMENT_SELECT},
            headers=auth_headers(token),
            timeout=self._timeout_s,
        )
        raise_graph_error(response, self._account)
        attachments = (_attachment(item) for item in _items(_json_dict(response)))
        return [attachment for attachment in attachments if attachment is not None]

    async def download(self, message_id: str, attachment_id: str, max_bytes: int) -> bytes:
        """Return the bytes of one attachment, refusing anything over ``max_bytes`` (R-5.2).

        Raises:
            GraphError: an id is not a Graph id (before any request) or Graph
                answers with an error.
            ValueError: the attachment is larger than ``max_bytes``.
        """
        message_path = _encoded_id(message_id)
        attachment_path = _encoded_id(attachment_id)
        url = f"{GRAPH_BASE_URL}/me/messages/{message_path}/attachments/{attachment_path}/$value"
        token = await self._tokens.token()
        chunks: list[bytes] = []
        total = 0
        async with self._http.stream(
            "GET", url, headers=auth_headers(token), timeout=self._timeout_s
        ) as response:
            raise_graph_error(response, self._account)
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"attachment exceeds {max_bytes} bytes")
                chunks.append(chunk)
        return b"".join(chunks)
