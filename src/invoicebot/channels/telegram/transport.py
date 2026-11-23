"""Deliver Telegram outbox rows as plain text with an inline keyboard (D180, R-1.10).

Telegram has no message window and no templates, so a row goes out as plain
text with no ``parse_mode`` and the row's template is ignored (D180). Buttons
travel as an inline keyboard, one button per row, with the button id as the
``callback_data``. Whatever no retry can fix (a recipient with no Telegram user
id, a text over Telegram's cap, a button id over the ``callback_data`` cap)
raises ``PermanentSendError``, so the outbox kills the row at once. The id the
row gets back is ``"<chat_id>:<message_id>"`` (D178), because Telegram message
ids are unique only per chat.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import Any

from invoicebot.channels.base import Button, OutboundMessage
from invoicebot.channels.telegram.client import TelegramClient, TelegramError
from invoicebot.queue.outbox import OutboxRow, PermanentSendError, SendLater, Transport
from invoicebot.queue.retry import RULE_TOKEN_REJECTED, TOKEN_RETRY_DELAY

#: Telegram's sendMessage text cap in characters (D180).
TEXT_MAX_CHARS = 4096
#: Telegram's inline button callback_data cap in UTF-8 bytes (D180).
CALLBACK_DATA_MAX_BYTES = 64
#: A rejected bot token: Telegram answers 401 (BR-O03).
TOKEN_REJECTED_STATUS = 401
#: Telegram answers these when the message can never be delivered (D180).
PERMANENT_STATUSES = frozenset({400, 403})
#: Why a rate-limited row waits: Telegram's own retry delay.
RATE_LIMITED_REASON = "telegram_rate_limited"


def make_transport(
    client: TelegramClient,
    *,
    user_ids: Mapping[str, int],
    on_token_rejected: Callable[[], Awaitable[None]],
) -> Transport:
    """Build the outbox transport that delivers one Telegram message per row (D180).

    ``row.to`` is a profile id, resolved to that profile's Telegram user id
    through ``user_ids``. A rejected token awaits ``on_token_rejected`` so the
    owner is alerted, then reschedules the row instead of failing it (BR-O03).
    """

    async def transport(row: OutboxRow) -> str:
        msg = OutboundMessage.model_validate(row.payload)
        payload = _payload(msg, _chat_id(row, user_ids))
        try:
            chat_id, message_id = await client.send_message(payload)
        except TelegramError as exc:
            later = await _send_later_for(exc, on_token_rejected)
            if later is not None:
                raise later from exc
            raise
        return f"{chat_id}:{message_id}"

    return transport


async def _send_later_for(
    exc: TelegramError,
    on_token_rejected: Callable[[], Awaitable[None]],
) -> SendLater | None:
    """Map a Telegram error to a reschedule, a permanent failure, or None to re-raise it."""
    if exc.retry_after is not None:
        return SendLater(timedelta(seconds=exc.retry_after), RATE_LIMITED_REASON)
    if exc.status == TOKEN_REJECTED_STATUS:
        await on_token_rejected()
        return SendLater(TOKEN_RETRY_DELAY, RULE_TOKEN_REJECTED)
    if exc.status in PERMANENT_STATUSES:
        raise PermanentSendError(_permanent_message(exc)) from None
    return None


def _permanent_message(exc: TelegramError) -> str:
    """Name only the status and Telegram's error code, never the description or the text."""
    return f"Telegram API error {exc.status}: code {exc.code}"


def _chat_id(row: OutboxRow, user_ids: Mapping[str, int]) -> int:
    """Return the Telegram user id a profile sends to, or fail permanently when it has none."""
    try:
        return user_ids[row.to]
    except KeyError as exc:
        raise PermanentSendError(f"no Telegram user id for {row.to}") from exc


def _payload(msg: OutboundMessage, chat_id: int) -> dict[str, Any]:
    """Build the sendMessage payload for ``msg``, refusing what Telegram would reject."""
    if len(msg.text) > TEXT_MAX_CHARS:
        raise PermanentSendError(f"Telegram text is over {TEXT_MAX_CHARS} characters")
    payload: dict[str, Any] = {"chat_id": chat_id, "text": msg.text}
    if msg.buttons:
        payload["reply_markup"] = {"inline_keyboard": _keyboard(msg.buttons)}
    return payload


def _keyboard(buttons: list[Button]) -> list[list[dict[str, str]]]:
    """Lay the buttons out one per row, with their id as the callback data (D180)."""
    rows: list[list[dict[str, str]]] = []
    for button in buttons:
        if len(button.id.encode("utf-8")) > CALLBACK_DATA_MAX_BYTES:
            raise PermanentSendError(f"Telegram callback data is over {CALLBACK_DATA_MAX_BYTES} bytes")
        rows.append([{"text": button.title, "callback_data": button.id}])
    return rows
