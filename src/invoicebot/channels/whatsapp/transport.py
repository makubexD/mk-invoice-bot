"""Deliver WhatsApp outbox rows, using a template outside the 24 h window (R-1.10, PT-4).

Free-form messages are only allowed inside the customer service window. When the
window is closed, or Meta answers that it has just closed, the same text goes
through an approved template instead (BR-S04). A message that carries a document
is uploaded and sent as a document inside the window only: outside it, the row
fails at once (D194). A failure that no retry can fix (no number for the
recipient, no configured template, a missing or oversized document, a permanent
Meta error code) raises ``PermanentSendError``, so the outbox kills the row at
once. The window is read on its own pooled connection, never inside a
transaction held across the HTTP call.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from invoicebot.channels.base import Document, OutboundMessage
from invoicebot.channels.documents import read_document
from invoicebot.channels.whatsapp.client import WhatsAppClient, WhatsAppError, WindowExpired
from invoicebot.channels.whatsapp.payloads import (
    BUTTONS_MAX_COUNT,
    buttons_payload,
    document_payload,
    list_payload,
    template_payload,
    text_payload,
)
from invoicebot.config.models import ChannelTemplates, WhatsAppChannel
from invoicebot.queue.outbox import OutboxRow, PermanentSendError, SendLater, Transport
from invoicebot.queue.retry import RULE_TOKEN_REJECTED, TOKEN_RETRY_DELAY
from invoicebot.rules.messages_es import LIST_BUTTON
from invoicebot.store.db import Pool
from invoicebot.store.invoices import window_open

logger = logging.getLogger(__name__)

#: Meta error codes no retry can fix: invalid parameter, parameter value invalid,
#: message undeliverable, unsupported message type.
PERMANENT_META_CODES = frozenset({100, 131009, 131026, 131051})

#: Rules whose out-of-window message has an approved template (R-1.10, BR-S04).
TEMPLATE_SLOT_BY_RULE: dict[str, str] = {
    "BR-F07": "manual_required",
    "BR-F12": "resume_notice",
    "BR-D16": "completion",
    "BR-D17": "completion",
    "BR-D18": "completion",
    "BR-S08": "pending_user_reminder",
    "BR-S04": "admin_alert",
    "BR-O01": "admin_alert",
    "BR-D01_admin": "admin_alert",
    "BR-D03_admin": "admin_alert",
    "BR-E08_admin": "admin_alert",
    "BR-S03_admin": "admin_alert",
    "BR-V02_admin": "admin_alert",
    "BR-V06_admin": "admin_alert",
    "GAP_admin": "admin_alert",
    "GAP_DIGEST_admin": "admin_alert",
    "BR-O02": "admin_alert",
    "BR-O03": "admin_alert",
    "BR-O04": "admin_alert",
    "BR-O05": "admin_alert",
    "BR-D05": "admin_alert",
}

#: Meta caps one template body parameter at 1024 characters.
TEMPLATE_PARAM_MAX_CHARS = 1024

#: A rejected channel token: Meta's status and error code for an expired or invalid token (BR-O03).
TOKEN_REJECTED_STATUS = 401
TOKEN_REJECTED_CODE = 190

#: Meta's document cap in bytes (D194).
DOCUMENT_MAX_BYTES = 100 * 1024 * 1024


class TemplateMissing(Exception):
    """The message needs a template, but no configured template covers it (R-1.10)."""


def template_slot(msg: OutboundMessage) -> str | None:
    """Return the configured slot for ``msg``: its own template, else its rule's."""
    if msg.template is not None:
        return msg.template
    if msg.rule_id is None:
        return None
    return TEMPLATE_SLOT_BY_RULE.get(msg.rule_id)


def make_transport(
    client: WhatsAppClient,
    pool: Pool,
    *,
    numbers: Mapping[str, str],
    data_dir: Path,
    channel: WhatsAppChannel,
    on_token_rejected: Callable[[], Awaitable[None]],
) -> Transport:
    """Build the outbox transport that delivers one WhatsApp message per row (R-1.10, PT-4).

    The returned id is Meta's message id, or None when Meta's answer carried
    none, which the outbox stores as the row's ``channel_message_id`` (D102).
    A rejected token awaits ``on_token_rejected`` so the owner is alerted, then
    reschedules the row instead of failing it (BR-O03). Templates and their
    language come from ``channel``; a document is read from under ``data_dir`` (D194).
    """

    async def transport(row: OutboxRow) -> str | None:
        msg = OutboundMessage.model_validate(row.payload)
        try:
            to = numbers[row.to]
        except KeyError as exc:
            raise PermanentSendError(f"no WhatsApp number for {row.to}") from exc
        document = msg.document
        if document is not None:
            if not await _recipient_window_open(pool, msg, row.to):
                raise PermanentSendError("document outside the 24-hour window")
            message_id = await _document_message_id(client, msg, document, to, data_dir, on_token_rejected)
            return _channel_message_id(row, message_id)
        if await _recipient_window_open(pool, msg, row.to):
            try:
                message_id = await _send(client, _free_form_payload(to, msg), on_token_rejected)
                return _channel_message_id(row, message_id)
            except WindowExpired:
                pass
        try:
            name = _template_name(channel.templates, msg)
        except TemplateMissing as exc:
            raise PermanentSendError(str(exc)) from exc
        message_id = await _send(
            client,
            template_payload(to, name, channel.template_language, [_template_parameter(msg)]),
            on_token_rejected,
        )
        return _channel_message_id(row, message_id)

    return transport


async def _document_message_id(
    client: WhatsAppClient,
    msg: OutboundMessage,
    document: Document,
    to: str,
    data_dir: Path,
    on_token_rejected: Callable[[], Awaitable[None]],
) -> str | None:
    """Upload ``document`` and send it with the message text as its caption (D194).

    A closed window answered by Meta on the document send is permanent: a
    document has no template to fall back to.
    """
    data = await read_document(document, data_dir, DOCUMENT_MAX_BYTES)
    media_id = await _deliver(
        lambda: client.upload_media(data, document.filename, document.mime), on_token_rejected
    )
    try:
        return await _send(
            client,
            document_payload(to, media_id, document.filename, _caption(msg)),
            on_token_rejected,
        )
    except WindowExpired as exc:
        raise PermanentSendError("document outside the 24-hour window") from exc


async def _send(
    client: WhatsAppClient,
    payload: dict[str, Any],
    on_token_rejected: Callable[[], Awaitable[None]],
) -> str | None:
    """Send one payload and return Meta's message id (R-11.5, BR-O03).

    A permanent Meta code fails at once; a rejected token alerts and waits for
    the token to be renewed instead.
    """
    return await _deliver(lambda: client.send(payload), on_token_rejected)


async def _deliver[T](
    call: Callable[[], Awaitable[T]],
    on_token_rejected: Callable[[], Awaitable[None]],
) -> T:
    """Run one Graph call and map its failure to a reschedule or a permanent error (BR-O03, R-11.5).

    Both the upload and the send go through here, so a rejected token alerts and
    waits in exactly the same way for either call.
    """
    try:
        return await call()
    except WhatsAppError as exc:
        if exc.status == TOKEN_REJECTED_STATUS or exc.code == TOKEN_REJECTED_CODE:
            await on_token_rejected()
            raise SendLater(TOKEN_RETRY_DELAY, RULE_TOKEN_REJECTED) from exc
        if exc.code in PERMANENT_META_CODES:
            raise PermanentSendError(f"WhatsApp API error {exc.status}: code {exc.code}") from None
        raise


def _channel_message_id(row: OutboxRow, message_id: str | None) -> str | None:
    """Return the delivered row's message id, warning with the row id only when there is none."""
    if message_id is None:
        logger.warning("outbox row %d was delivered without a channel message id", row.id)
    return message_id


async def _recipient_window_open(pool: Pool, msg: OutboundMessage, recipient: str) -> bool:
    """Read the recipient's 24-hour window without holding a transaction (R-1.10).

    The window belongs to whoever receives the row, not to the profile the
    message is about: an admin alert goes to the admin's window.
    """
    async with pool.connection() as conn:
        return await window_open(conn, msg.channel, recipient)


def _free_form_payload(to: str, msg: OutboundMessage) -> dict[str, Any]:
    """Build the in-window payload: a list past Meta's button cap, reply buttons, else plain text."""
    text = _caption(msg)
    if len(msg.buttons) > BUTTONS_MAX_COUNT:
        return list_payload(to, text, LIST_BUTTON, msg.buttons)
    if msg.buttons:
        return buttons_payload(to, text, msg.buttons)
    return text_payload(to, text)


def _caption(msg: OutboundMessage) -> str:
    """Return the text, then one line per link, for a text body or a document caption (D194)."""
    links = _link_lines(msg)
    return f"{msg.text}\n{links}" if links else msg.text


def _link_lines(msg: OutboundMessage) -> str:
    """Return one ``title: url`` line per link, or an empty string when there are none (D194)."""
    return "\n".join(f"{link.title}: {link.url}" for link in msg.links)


def _template_name(templates: ChannelTemplates, msg: OutboundMessage) -> str:
    """Return the configured template name, or raise ``TemplateMissing``."""
    slot = template_slot(msg)
    name = getattr(templates, slot, "") if slot is not None else ""
    if not name:
        raise TemplateMissing(slot or msg.rule_id or "?")
    return str(name)


def _template_parameter(msg: OutboundMessage) -> str:
    """Fit the message text and its link lines into one template body parameter (D194).

    Meta rejects newlines, tabs and runs of more than four spaces, so every
    whitespace run is collapsed to one space. The link lines are kept whole and
    only the text is truncated, to the room the links leave.
    """
    links = " ".join(_link_lines(msg).split())
    if not links:
        return " ".join(msg.text[:TEMPLATE_PARAM_MAX_CHARS].split())
    room = max(TEMPLATE_PARAM_MAX_CHARS - len(links) - 1, 0)
    text = " ".join(msg.text[:room].split())
    return f"{text} {links}" if text else links
