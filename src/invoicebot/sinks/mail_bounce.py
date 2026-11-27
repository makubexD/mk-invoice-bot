"""Finds one sent mail's delivery report in a Microsoft inbox and drafts its Sent copy (D207).

Plain ``httpx`` over Graph like ``fetchers/graph_mail.py`` (``GRAPH_BASE_URL``,
``auth_headers``, ``raise_graph_error``, the Graph id check before any id goes
into a path). The caller builds the token source with ``Mail.ReadWrite``. Errors
carry the HTTP status only, never a URL, an address or a body (R-9.16).
"""

from __future__ import annotations

import base64
import email.parser
import email.policy
import logging
import re
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message

import httpx

from invoicebot.fetchers.graph_mail import encoded_graph_id
from invoicebot.sinks.base import TokenSource
from invoicebot.sinks.onedrive import (
    GRAPH_BASE_URL,
    NOT_SENT_STATUS,
    GraphError,
    auth_headers,
    raise_graph_error,
)

logger = logging.getLogger(__name__)

#: The only shape an id may have before it goes into an OData filter.
MESSAGE_ID = re.compile(r"^<[^<>'\s]{1,250}>$")

#: A delivery report bigger than this is skipped.
MAX_REPORT_BYTES = 2 * 1024 * 1024

#: A Sent copy bigger than this is not turned into a draft.
MAX_DRAFT_BYTES = 3 * 1024 * 1024

#: How many inbox messages one watch looks at.
_INBOX_TOP = 50

_STATUS_CODE = re.compile(r"^[245]\.\d{1,3}\.\d{1,3}$")
_ORIGINAL_ID_HEADER = "X-Microsoft-Original-Message-ID"
_WEB_LINK_HOSTS = frozenset({"outlook.live.com", "outlook.office.com"})


class DraftUnavailable(Exception):
    """The Sent copy is missing or too big, or the draft's link is not usable (D207)."""


@dataclass(frozen=True)
class Bounce:
    """A delivery report: its status code and the id Outlook gave the mail (D207)."""

    status: str
    sent_id: str


def _sub_messages(part: Message) -> list[Message]:
    """Return the parsed messages a ``message/*`` part carries."""
    payload = part.get_payload()
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, Message)]


def _status_of(report: Message) -> str:
    """Return the first well-formed ``Status:`` of the delivery-status part, else ``?``."""
    for part in report.walk():
        if part.get_content_type() != "message/delivery-status":
            continue
        for block in _sub_messages(part):
            value = str(block.get("Status", "")).strip()
            if _STATUS_CODE.fullmatch(value):
                return value
    return "?"


def _original_part(report: Message, message_id: str) -> Message | None:
    """Return the ``message/rfc822`` part whose original id is ``message_id``."""
    for part in report.walk():
        if part.get_content_type() != "message/rfc822":
            continue
        for inner in _sub_messages(part):
            if str(inner.get(_ORIGINAL_ID_HEADER, "")).strip() == message_id:
                return inner
    return None


def parse_bounce(raw: bytes, message_id: str) -> Bounce | None:
    """Return the bounce when ``raw`` is the report of ``message_id``, else None (D207)."""
    report = email.parser.BytesParser(policy=email.policy.default).parsebytes(raw)
    original = _original_part(report, message_id)
    if original is None:
        return None
    for candidate in (report.get("In-Reply-To"), original.get("Message-ID")):
        sent_id = str(candidate).strip() if candidate is not None else ""
        if MESSAGE_ID.fullmatch(sent_id):
            return Bounce(status=_status_of(report), sent_id=sent_id)
    return None


async def _get_json(
    http: httpx.AsyncClient,
    tokens: TokenSource,
    url: str,
    params: dict[str, str],
    *,
    account: str,
    timeout_s: float,
) -> list[dict[str, object]]:
    """Return the ``value`` entries of one Graph collection call.

    Raises:
        GraphError: Graph answers with an error or a malformed body.
    """
    response = await http.get(
        url,
        params=params,
        headers=auth_headers(await tokens.token()),
        timeout=timeout_s,
    )
    raise_graph_error(response, account)
    try:
        body = response.json()
    except ValueError as err:
        raise GraphError(NOT_SENT_STATUS) from err
    items = body.get("value") if isinstance(body, dict) else None
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise GraphError(NOT_SENT_STATUS)
    return items


def _item_id(item: dict[str, object]) -> str:
    """Return the checked, encoded ``id`` of a listed message.

    Raises:
        GraphError: the id is missing or not a Graph id.
    """
    value = item.get("id")
    if not isinstance(value, str):
        raise GraphError(NOT_SENT_STATUS)
    return encoded_graph_id(value)


async def _read_raw(
    http: httpx.AsyncClient,
    tokens: TokenSource,
    encoded_id: str,
    max_bytes: int,
    *,
    account: str,
    timeout_s: float,
) -> bytes | None:
    """Return a message's MIME, or None when it is over ``max_bytes``."""
    chunks: list[bytes] = []
    total = 0
    async with http.stream(
        "GET",
        f"{GRAPH_BASE_URL}/me/messages/{encoded_id}/$value",
        headers=auth_headers(await tokens.token()),
        timeout=timeout_s,
    ) as response:
        raise_graph_error(response, account)
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                return None
            chunks.append(chunk)
    return b"".join(chunks)


async def find_bounce(
    http: httpx.AsyncClient,
    tokens: TokenSource,
    *,
    message_id: str,
    since: datetime,
    account: str,
    timeout_s: float,
) -> Bounce | None:
    """Return the delivery report of ``message_id`` received since ``since``, or None (D207).

    ``since`` must be timezone-aware; ``account`` names the sign-in an ``AuthRequired`` asks for.

    Raises:
        GraphError: Graph answers with a final error or a malformed body.
        GraphTransient: Graph answers with a retryable error.
        AuthRequired: the account needs a sign-in.
    """
    items = await _get_json(
        http,
        tokens,
        f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages",
        {
            "$filter": f"receivedDateTime ge {_odata_utc(since)}",
            "$select": "id,subject",
            "$top": str(_INBOX_TOP),
        },
        account=account,
        timeout_s=timeout_s,
    )
    for item in items:
        raw = await _read_raw(
            http, tokens, _item_id(item), MAX_REPORT_BYTES, account=account, timeout_s=timeout_s
        )
        if raw is None:
            logger.warning("bounce watch skipped an oversize inbox message")
            continue
        bounce = parse_bounce(raw, message_id)
        if bounce is not None:
            return bounce
    return None


def _odata_utc(moment: datetime) -> str:
    """Return ``moment`` as an OData UTC literal, whole seconds (``2026-11-27T10:00:00Z``).

    Raises:
        ValueError: ``moment`` has no timezone.
    """
    if moment.tzinfo is None:
        raise ValueError("since must be timezone-aware")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _checked_web_link(value: object) -> str:
    """Return ``value`` when it is an https link on an Outlook host.

    Raises:
        DraftUnavailable: it is anything else.
    """
    if not isinstance(value, str):
        raise DraftUnavailable("no link")
    try:
        parts = urllib.parse.urlsplit(value)
        host = parts.hostname
    except ValueError as err:
        raise DraftUnavailable("bad link") from err
    if parts.scheme != "https" or host not in _WEB_LINK_HOSTS:
        raise DraftUnavailable("bad link")
    return value


async def draft_from_sent(
    http: httpx.AsyncClient,
    tokens: TokenSource,
    *,
    sent_id: str,
    account: str,
    timeout_s: float,
) -> str:
    """Make a draft from the Sent copy of ``sent_id`` and return its web link (D207).

    ``account`` names the sign-in an ``AuthRequired`` asks for.

    Raises:
        ValueError: ``sent_id`` is not a message id.
        DraftUnavailable: the copy is missing or too big, or the link is not Outlook's.
        GraphError: Graph answers with a final error or a malformed body.
        GraphTransient: Graph answers with a retryable error.
        AuthRequired: the account needs a sign-in.
    """
    if MESSAGE_ID.fullmatch(sent_id) is None:
        raise ValueError("sent_id is not a message id")
    items = await _get_json(
        http,
        tokens,
        f"{GRAPH_BASE_URL}/me/mailFolders/sentitems/messages",
        {"$filter": f"internetMessageId eq '{sent_id}'", "$select": "id", "$top": "1"},
        account=account,
        timeout_s=timeout_s,
    )
    if not items:
        raise DraftUnavailable("no Sent copy")
    raw = await _read_raw(
        http, tokens, _item_id(items[0]), MAX_DRAFT_BYTES, account=account, timeout_s=timeout_s
    )
    if raw is None:
        raise DraftUnavailable("Sent copy too big")
    response = await http.post(
        f"{GRAPH_BASE_URL}/me/messages",
        content=base64.b64encode(raw),
        headers={**auth_headers(await tokens.token()), "Content-Type": "text/plain"},
        timeout=timeout_s,
    )
    raise_graph_error(response, account)
    try:
        body = response.json()
    except ValueError as err:
        raise GraphError(NOT_SENT_STATUS) from err
    return _checked_web_link(body.get("webLink") if isinstance(body, dict) else None)
