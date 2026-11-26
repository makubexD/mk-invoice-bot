"""Send mail through Microsoft Graph's REST API (R-7.6, R-7.7, D84).

Two shapes: a mail whose attachments total at most
:data:`MAX_INLINE_ATTACHMENT_BYTES` is one ``sendMail`` call; a bigger one
becomes a draft, uploads each attachment through its own upload session (the
chunked ``PUT``s carry no bearer header), then sends the draft. Tokens come
from an injected :class:`TokenSource`; errors carry the HTTP status only, never
a body, an address or a token (R-9.16).

The body is always HTML (D197): a ``Text`` body was routed through Outlook.com's
high-risk pool and bounced with 5.7.501.
"""

from __future__ import annotations

import base64
import html
import re
import urllib.parse
from collections.abc import Sequence

import httpx

from invoicebot.sinks.base import Attachment, OutgoingMail, TokenSource
from invoicebot.sinks.onedrive import (
    GRAPH_BASE_URL,
    NOT_SENT_STATUS,
    GraphError,
    auth_headers,
    raise_graph_error,
    upload_chunks,
)

#: The sum of attachment sizes up to here goes inline in one ``sendMail`` (R-7.6).
MAX_INLINE_ATTACHMENT_BYTES = 3 * 1024 * 1024

#: The attachment kind Graph renders as a regular file.
_FILE_ATTACHMENT_TYPE = "#microsoft.graph.fileAttachment"

#: The status Graph answers a created draft with; any other is an error.
_DRAFT_CREATED_STATUS = 201


#: Inline style of the one wrapper div around the body (D197).
BODY_STYLE = "font-family: Calibri, Arial, sans-serif; font-size: 11pt"

_BLANK_LINES = re.compile(r"\n\s*\n")


def html_body(text: str) -> str:
    """Render plain ``text`` as an HTML body: escaped, one ``<p>`` per paragraph (D197)."""
    escaped = html.escape(text).replace("\r\n", "\n")
    paragraphs = [part.strip("\n") for part in _BLANK_LINES.split(escaped)]
    rendered = "".join(f"<p>{part.replace(chr(10), '<br>')}</p>" for part in paragraphs if part.strip())
    return f'<html><body><div style="{BODY_STYLE}">{rendered}</div></body></html>'


def _recipients(addresses: Sequence[str]) -> list[dict[str, object]]:
    """Render addresses as Graph recipients."""
    return [{"emailAddress": {"address": address}} for address in addresses]


def _message(mail: OutgoingMail) -> dict[str, object]:
    """Render the Graph ``message`` resource for ``mail``, without attachments."""
    return {
        "subject": mail.subject,
        "body": {"contentType": "HTML", "content": html_body(mail.body)},
        "toRecipients": _recipients(mail.to),
        "ccRecipients": _recipients(mail.cc),
    }


def _inline_attachment(attachment: Attachment) -> dict[str, object]:
    """Render ``attachment`` as an inline ``fileAttachment``."""
    return {
        "@odata.type": _FILE_ATTACHMENT_TYPE,
        "name": attachment.name,
        "contentType": attachment.mime,
        "contentBytes": base64.b64encode(attachment.data).decode("ascii"),
    }


def _session_attachment(attachment: Attachment) -> dict[str, object]:
    """Render ``attachment`` as the ``AttachmentItem`` of an upload session."""
    return {
        "attachmentType": "file",
        "name": attachment.name,
        "size": len(attachment.data),
        "contentType": attachment.mime,
    }


def _json_body(response: httpx.Response) -> object:
    """Return the decoded JSON body, or None when the body is not JSON."""
    try:
        body: object = response.json()
    except ValueError:
        return None
    return body


def _message_id(response: httpx.Response) -> str | None:
    """Return the draft's id when the answer holds a non-empty string id."""
    body = _json_body(response)
    if not isinstance(body, dict):
        return None
    message_id = body.get("id")
    return message_id if isinstance(message_id, str) and message_id else None


def _upload_url(response: httpx.Response) -> str | None:
    """Return the session URL from a createUploadSession answer."""
    body = _json_body(response)
    if not isinstance(body, dict):
        return None
    url = body.get("uploadUrl")
    return url if isinstance(url, str) else None


class GraphMailer:
    """One Microsoft account's mailbox, over Graph and an injected HTTP client (D84).

    Long-lived: one instance per signed-in account.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        tokens: TokenSource,
        timeout_s: float,
        *,
        account: str,
    ) -> None:
        self._client = client
        self._tokens = tokens
        self._timeout_s = timeout_s
        self._account = account

    async def send(self, mail: OutgoingMail) -> None:
        """Send ``mail`` and return once Graph accepts it for delivery (R-7.6).

        Raises:
            AuthRequired: the account needs an interactive sign-in.
            GraphTransient: Graph answered 429 or 5xx; the queue retries.
            GraphError: any other final failure, or a draft without a usable id.
        """
        total = sum(len(attachment.data) for attachment in mail.attachments)
        if total <= MAX_INLINE_ATTACHMENT_BYTES:
            await self._send_inline(mail)
        else:
            await self._send_through_draft(mail)

    async def _send_inline(self, mail: OutgoingMail) -> None:
        token = await self._tokens.token()
        response = await self._client.post(
            f"{GRAPH_BASE_URL}/me/sendMail",
            json={
                "message": {
                    **_message(mail),
                    "attachments": [_inline_attachment(attachment) for attachment in mail.attachments],
                },
                "saveToSentItems": True,
            },
            headers=auth_headers(token),
            timeout=self._timeout_s,
        )
        raise_graph_error(response, self._account)

    async def _send_through_draft(self, mail: OutgoingMail) -> None:
        message_id = urllib.parse.quote(await self._create_draft(mail), safe="")
        for attachment in mail.attachments:
            await self._upload_attachment(message_id, attachment)
        token = await self._tokens.token()
        response = await self._client.post(
            f"{GRAPH_BASE_URL}/me/messages/{message_id}/send",
            headers=auth_headers(token),
            timeout=self._timeout_s,
        )
        raise_graph_error(response, self._account)

    async def _create_draft(self, mail: OutgoingMail) -> str:
        token = await self._tokens.token()
        response = await self._client.post(
            f"{GRAPH_BASE_URL}/me/messages",
            json=_message(mail),
            headers=auth_headers(token),
            timeout=self._timeout_s,
        )
        if response.status_code != _DRAFT_CREATED_STATUS:
            raise_graph_error(response, self._account)
            raise GraphError(response.status_code)
        message_id = _message_id(response)
        if message_id is None:
            raise GraphError(NOT_SENT_STATUS)
        return message_id

    async def _upload_attachment(self, message_id: str, attachment: Attachment) -> None:
        token = await self._tokens.token()
        # Graph takes files under 3 MB as a plain attachment POST; sessions are for larger ones.
        if len(attachment.data) < MAX_INLINE_ATTACHMENT_BYTES:
            response = await self._client.post(
                f"{GRAPH_BASE_URL}/me/messages/{message_id}/attachments",
                json=_inline_attachment(attachment),
                headers=auth_headers(token),
                timeout=self._timeout_s,
            )
            raise_graph_error(response, self._account)
            return
        response = await self._client.post(
            f"{GRAPH_BASE_URL}/me/messages/{message_id}/attachments/createUploadSession",
            json={"AttachmentItem": _session_attachment(attachment)},
            headers=auth_headers(token),
            timeout=self._timeout_s,
        )
        raise_graph_error(response, self._account)
        upload_url = _upload_url(response)
        if upload_url is None:
            raise GraphError(NOT_SENT_STATUS)
        await upload_chunks(
            self._client,
            upload_url,
            attachment.data,
            timeout_s=self._timeout_s,
            account=self._account,
        )
