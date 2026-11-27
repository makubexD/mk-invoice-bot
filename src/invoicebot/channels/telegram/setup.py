"""The one-off Bot API calls the owner's setup runs: sender ids and the webhook (D186).

Functions take a ``TelegramClient`` and use its ``call``; nothing here is
imported by the running bot.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from invoicebot.channels.telegram.client import (
    OK_STATUS,
    TelegramClient,
    TelegramError,
)
from invoicebot.config.models import Profile

WEBHOOK_PATH = "/webhook/telegram"  # D186: the path the funnel and the API route share
WEBHOOK_ACTIVE_STATUS = 409  # getUpdates is refused while a webhook is set


class WebhookActive(Exception):
    """``getUpdates`` is refused because a webhook is set (D186)."""


@dataclass(frozen=True)
class WebhookInfo:
    """What ``getWebhookInfo`` reports: URL, pending count and last error (D186)."""

    url: str
    pending: int
    last_error_date: int | None
    last_error_message: str | None


def _int_field(value: object) -> int | None:
    """Return ``value`` when it is an int that is not a bool."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _private_sender(update: object) -> tuple[int, str | None] | None:
    """Return the sender of a private update, or None when it is not one (D175)."""
    if not isinstance(update, dict):
        return None
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    sender = message.get("from")
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        return None
    if chat.get("type") != "private":
        return None
    chat_id = _int_field(chat.get("id"))
    sender_id = _int_field(sender.get("id"))
    if chat_id is None or sender_id is None or chat_id != sender_id:
        return None
    username = sender.get("username")
    return sender_id, username if isinstance(username, str) else None


async def sender_ids(client: TelegramClient) -> list[tuple[int, str | None]]:
    """List the pending updates' private senders, first seen, each id once (D186, D175).

    Message text is never read. Raises ``WebhookActive`` when Telegram answers 409.
    """
    # shortcut: one getUpdates returns at most 100 updates, so senders beyond those are missed;
    # fine for a handful of owner profiles.
    try:
        result = await client.call("getUpdates", {"allowed_updates": ["message"], "timeout": 0})
    except TelegramError as error:
        if error.status == WEBHOOK_ACTIVE_STATUS:
            raise WebhookActive from error
        raise
    if not isinstance(result, list):
        return []
    senders: dict[int, str | None] = {}
    for update in result:
        sender = _private_sender(update)
        if sender is not None and sender[0] not in senders:
            senders[sender[0]] = sender[1]
    return list(senders.items())


async def set_webhook(client: TelegramClient, url: str, secret: str) -> None:
    """Register the bot's webhook with the stored secret (D186).

    Re-registering keeps Telegram's pending updates, so re-pointing the webhook
    to a new tunnel URL loses nothing (D201).

    Raises ``ValueError`` for a URL that is not ``https://`` or does not end in
    ``/webhook/telegram``.
    """
    if not url.startswith("https://") or not url.endswith(WEBHOOK_PATH):
        raise ValueError(f"the webhook URL must be https:// and end in {WEBHOOK_PATH}")
    await client.call(
        "setWebhook",
        {
            "url": url,
            "secret_token": secret,
            "allowed_updates": ["message", "callback_query"],
            "drop_pending_updates": False,
        },
    )


async def webhook_info(client: TelegramClient) -> WebhookInfo:
    """Return the webhook's URL, pending update count and last error (D186)."""
    result = await client.call("getWebhookInfo", {})
    if not isinstance(result, dict):
        raise TelegramError(status=OK_STATUS, message="getWebhookInfo result is not an object")
    url = result.get("url")
    last_error_message = result.get("last_error_message")
    return WebhookInfo(
        url=url if isinstance(url, str) else "",
        pending=_int_field(result.get("pending_update_count")) or 0,
        last_error_date=_int_field(result.get("last_error_date")),
        last_error_message=last_error_message if isinstance(last_error_message, str) else None,
    )


async def delete_webhook(client: TelegramClient) -> None:
    """Delete the bot's webhook without dropping the pending updates (D186)."""
    await client.call("deleteWebhook", {"drop_pending_updates": False})


async def set_commands(
    client: TelegramClient,
    entries: Sequence[tuple[str, str]],
    chat_id: int | None = None,
) -> None:
    """Publish the command menu for every chat, or only for ``chat_id`` (D187).

    ``entries`` are the ``(command, description)`` pairs the caller took from
    the registry; the scope is the bot's default menu unless a chat is given.
    """
    if chat_id is None:
        scope: dict[str, object] = {"type": "default"}
    else:
        scope = {"type": "chat", "chat_id": chat_id}
    commands = [{"command": command, "description": description} for command, description in entries]
    await client.call("setMyCommands", {"commands": commands, "scope": scope})


async def publish_menus(
    client: TelegramClient,
    profiles: Mapping[str, Profile],
    user_entries: Sequence[tuple[str, str]],
    admin_entries: Sequence[tuple[str, str]],
) -> list[str]:
    """Set the default menu, then each admin's chat-scoped one; return what was set (D187).

    The returned names are ``"default"`` first, then the profile ids of the admins
    that have a ``telegram_user_id``. An admin without one gets no chat menu.
    """
    await set_commands(client, user_entries)
    names = ["default"]
    for profile_id, profile in profiles.items():
        if profile.admin and profile.telegram_user_id is not None:
            await set_commands(client, admin_entries, chat_id=profile.telegram_user_id)
            names.append(profile_id)
    return names
