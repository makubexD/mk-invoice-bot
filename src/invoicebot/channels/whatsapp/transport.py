"""Deliver WhatsApp outbox rows, using a template outside the 24 h window (R-1.10, PT-4).

Free-form messages are only allowed inside the customer service window. When the
window is closed, or Meta answers that it has just closed, the same text goes
through an approved template instead (BR-S04). A failure that no retry can fix
(no number for the recipient, no configured template, a permanent Meta error
code) raises ``PermanentSendError``, so the outbox kills the row at once. The
window is read on its own pooled connection, never inside a transaction held
across the HTTP call.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from invoicebot.channels.base import OutboundMessage
from invoicebot.channels.whatsapp.client import WhatsAppClient, WhatsAppError, WindowExpired
from invoicebot.channels.whatsapp.payloads import buttons_payload, template_payload, text_payload
from invoicebot.config.models import ChannelTemplates
from invoicebot.queue.outbox import OutboxRow, PermanentSendError, Transport
from invoicebot.store.db import Pool
from invoicebot.store.invoices import window_open

#: Meta error codes no retry can fix: invalid parameter, parameter value invalid,
#: message undeliverable, unsupported message type.
PERMANENT_META_CODES = frozenset({100, 131009, 131026, 131051})

#: Rules whose out-of-window message has an approved template (R-1.10, BR-S04).
TEMPLATE_SLOT_BY_RULE: dict[str, str] = {
    "BR-F07": "manual_required",
    "BR-S08": "pending_user_reminder",
    "BR-S04": "admin_alert",
    "BR-O01": "admin_alert",
}

#: Meta caps one template body parameter at 1024 characters.
TEMPLATE_PARAM_MAX_CHARS = 1024


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
    templates: ChannelTemplates,
    language: str,
) -> Transport:
    """Build the outbox transport that delivers one WhatsApp message per row (R-1.10, PT-4)."""

    async def transport(row: OutboxRow) -> None:
        msg = OutboundMessage.model_validate(row.payload)
        try:
            to = numbers[row.to]
        except KeyError as exc:
            raise PermanentSendError(f"no WhatsApp number for {row.to}") from exc
        if await _recipient_window_open(pool, msg, row.to):
            try:
                await _send(client, _free_form_payload(to, msg))
                return
            except WindowExpired:
                pass
        try:
            name = _template_name(templates, msg)
        except TemplateMissing as exc:
            raise PermanentSendError(str(exc)) from exc
        await _send(client, template_payload(to, name, language, [_template_parameter(msg.text)]))

    return transport


async def _send(client: WhatsAppClient, payload: dict[str, Any]) -> None:
    """Send one payload, making a Meta code that no retry can fix permanent (R-11.5)."""
    try:
        await client.send(payload)
    except WhatsAppError as exc:
        if exc.code in PERMANENT_META_CODES:
            raise PermanentSendError(str(exc)) from exc
        raise


async def _recipient_window_open(pool: Pool, msg: OutboundMessage, recipient: str) -> bool:
    """Read the recipient's 24-hour window without holding a transaction (R-1.10).

    The window belongs to whoever receives the row, not to the profile the
    message is about: an admin alert goes to the admin's window.
    """
    async with pool.connection() as conn:
        return await window_open(conn, msg.channel, recipient)


def _free_form_payload(to: str, msg: OutboundMessage) -> dict[str, Any]:
    """Build the in-window payload: reply buttons when present, else plain text."""
    if msg.buttons:
        return buttons_payload(to, msg.text, msg.buttons)
    return text_payload(to, msg.text)


def _template_name(templates: ChannelTemplates, msg: OutboundMessage) -> str:
    """Return the configured template name, or raise ``TemplateMissing``."""
    slot = template_slot(msg)
    name = getattr(templates, slot, "") if slot is not None else ""
    if not name:
        raise TemplateMissing(slot or msg.rule_id or "?")
    return str(name)


def _template_parameter(text: str) -> str:
    """Fit the message text into one template body parameter on a single line.

    Meta rejects newlines, tabs and runs of more than four spaces, so the text
    is truncated first and every whitespace run is collapsed to one space.
    """
    return " ".join(text[:TEMPLATE_PARAM_MAX_CHARS].split())
