"""Parse Telegram Bot API updates into canonical messages (R-1.7, R-1.8).

The update body is untrusted: every field is checked and a malformed update
yields an empty result rather than raising. Senders outside the profile index
are reported only through their keyed :func:`sender_hash` digest, and a button
press is reported so the ingress can acknowledge it (D177).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from invoicebot.channels.base import Attachment, InboundMessage
from invoicebot.channels.media_limits import document_kind
from invoicebot.channels.profiles import ProfileIndex, sender_role
from invoicebot.channels.sender_hash import sender_hash
from invoicebot.channels.telegram.client import GETFILE_MAX_BYTES
from invoicebot.text import replace_unsafe_chars

_MAX_EPOCH_SECONDS = 253402300799  # 9999-12-31T23:59:59Z
_FILE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,255}")
_COMMAND_AT_BOT_PATTERN = re.compile(r"^/([A-Za-z0-9_]+)@[A-Za-z0-9_]+")


@dataclass(frozen=True)
class ParsedUpdate:
    """Messages accepted from one update, the digests of rejected senders and the presses to ack."""

    messages: list[InboundMessage]
    unknown_senders: list[str]
    callback_query_ids: list[str]


@dataclass(frozen=True)
class _Content:
    """The canonical fields one message contributes."""

    text: str = ""
    attachments: list[Attachment] = field(default_factory=list)


def parse_update(
    payload: dict[str, Any],
    index: ProfileIndex,
    *,
    owner_profile: str,
    admin_profiles: frozenset[str],
    sender_hash_key: str,
    now: datetime,
) -> ParsedUpdate:
    """Return the canonical result of one Telegram update, ignoring a malformed one (R-1.7, R-1.8)."""
    message = payload.get("message")
    if isinstance(message, dict):
        return _parse_message(
            message,
            index,
            owner_profile=owner_profile,
            admin_profiles=admin_profiles,
            sender_hash_key=sender_hash_key,
        )
    callback = payload.get("callback_query")
    if isinstance(callback, dict):
        return _parse_callback(
            callback,
            index,
            owner_profile=owner_profile,
            admin_profiles=admin_profiles,
            sender_hash_key=sender_hash_key,
            now=now,
        )
    return _empty()


def _parse_message(
    message: dict[str, Any],
    index: ProfileIndex,
    *,
    owner_profile: str,
    admin_profiles: frozenset[str],
    sender_hash_key: str,
) -> ParsedUpdate:
    """Return a private message canonicalised, an unknown sender's digest, or nothing (R-1.7)."""
    chat = message.get("chat")
    message_id = _int_field(message, "message_id")
    sender_id = _int_field(message.get("from"), "id")
    chat_id = _int_field(chat, "id")
    received_at = _received_at(message.get("date"))
    if (
        message_id is None
        or sender_id is None
        or chat_id is None
        or received_at is None
        or chat_id != sender_id
        or _str_field(chat, "type") != "private"
    ):
        return _empty()
    profile_id = index.resolve("telegram", sender_id)
    if profile_id is None:
        return _unknown_sender(sender_hash_key, sender_id)
    content = _content(message)
    if content is None:
        return _empty()
    return ParsedUpdate(
        messages=[
            InboundMessage(
                channel="telegram",
                external_id=f"{chat_id}:{message_id}",
                profile_id=profile_id,
                sender_role=sender_role(profile_id, owner_profile, admin_profiles),
                text=_command_text(replace_unsafe_chars(content.text)),
                attachments=content.attachments,
                reply_to_external_id=_reply_to(message, chat_id),
                received_at=received_at,
            )
        ],
        unknown_senders=[],
        callback_query_ids=[],
    )


def _parse_callback(
    callback: dict[str, Any],
    index: ProfileIndex,
    *,
    owner_profile: str,
    admin_profiles: frozenset[str],
    sender_hash_key: str,
    now: datetime,
) -> ParsedUpdate:
    """Return one button press canonicalised, an unknown sender's digest, or nothing (D177, D178)."""
    query_id = _str_field(callback, "id")
    button_id = _str_field(callback, "data")
    sender_id = _int_field(callback.get("from"), "id")
    message = callback.get("message")
    chat = message.get("chat") if isinstance(message, dict) else None
    chat_id = _int_field(chat, "id")
    message_id = _int_field(message, "message_id")
    if (
        query_id is None
        or button_id is None
        or sender_id is None
        or chat_id is None
        or message_id is None
        or chat_id != sender_id
        or _str_field(chat, "type") != "private"
    ):
        return _empty()
    profile_id = index.resolve("telegram", sender_id)
    if profile_id is None:
        return _unknown_sender(sender_hash_key, sender_id)
    return ParsedUpdate(
        messages=[
            InboundMessage(
                channel="telegram",
                external_id=query_id,
                profile_id=profile_id,
                sender_role=sender_role(profile_id, owner_profile, admin_profiles),
                button_id=button_id,
                reply_to_external_id=f"{chat_id}:{message_id}",
                received_at=now,
            )
        ],
        unknown_senders=[],
        callback_query_ids=[query_id],
    )


def _content(message: dict[str, Any]) -> _Content | None:
    """Return the text and attachment a message carries, or ``None`` when its media is malformed."""
    text = _text(message)
    photo = message.get("photo")
    if photo is not None:
        attachment = _photo_attachment(photo)
        if attachment is None:
            return None
        return _Content(text=text, attachments=[attachment])
    document = message.get("document")
    if document is not None:
        return _document_content(document, text)
    return _Content(text=text)


def _text(message: dict[str, Any]) -> str:
    """Return the message text, falling back to the caption a photo or document carries."""
    text = _str_field(message, "text")
    if text is None:
        text = _str_field(message, "caption")
    return text or ""


def _command_text(text: str) -> str:
    """Drop the bot name from a leading ``/cmd@name`` so the command reads as ``/cmd``."""
    return _COMMAND_AT_BOT_PATTERN.sub(r"/\1", text)


def _photo_attachment(photo: object) -> Attachment | None:
    """Return the photo size D181 keeps, or ``None`` when the photo is malformed."""
    if not isinstance(photo, list):
        return None
    sizes = [entry for entry in photo if isinstance(entry, dict)]
    chosen = _chosen_size(sizes)
    if chosen is None:
        return None
    file_id = _str_field(chosen, "file_id")
    if file_id is None or not _is_file_id(file_id):
        return None
    return Attachment(kind="photo", media_ref=file_id, mime="image/jpeg", size=_file_size(chosen))


def _chosen_size(sizes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the largest size within the getFile cap, else the largest of all (D181)."""
    if not sizes:
        return None
    fitting = [entry for entry in sizes if _file_size(entry) <= GETFILE_MAX_BYTES]
    return max(fitting or sizes, key=_file_size)


def _document_content(document: object, text: str) -> _Content | None:
    """Return the document's attachment or a text-only message, ``None`` when it is malformed (R-1.7)."""
    file_id = _str_field(document, "file_id")
    mime = _str_field(document, "mime_type")
    if file_id is None or mime is None or not _is_file_id(file_id):
        return None
    kind = document_kind(mime)
    if kind is None:
        return _Content(text=text)
    attachment = Attachment(kind=kind, media_ref=file_id, mime=mime, size=_file_size(document))
    return _Content(text=text, attachments=[attachment])


def _reply_to(message: dict[str, Any], chat_id: int) -> str | None:
    """Return ``"<chat>:<message id>"`` when the message replies to another (D178)."""
    replied_id = _int_field(message.get("reply_to_message"), "message_id")
    if replied_id is None:
        return None
    return f"{chat_id}:{replied_id}"


def _received_at(value: object) -> datetime | None:
    """Return the update date as a UTC datetime, or ``None`` when it is out of range."""
    seconds = _int_value(value)
    if seconds is None or not 0 <= seconds <= _MAX_EPOCH_SECONDS:
        return None
    return datetime.fromtimestamp(seconds, tz=UTC)


def _unknown_sender(sender_hash_key: str, sender_id: int) -> ParsedUpdate:
    """Report an allowlist miss only through its keyed digest (R-1.2)."""
    digest = sender_hash(sender_hash_key, str(sender_id))
    return ParsedUpdate(messages=[], unknown_senders=[digest], callback_query_ids=[])


def _empty() -> ParsedUpdate:
    """Return the result for an update that carries nothing canonical."""
    return ParsedUpdate(messages=[], unknown_senders=[], callback_query_ids=[])


def _file_size(data: object) -> int:
    """Return a non-negative ``file_size``, or 0 when it is absent or malformed."""
    value = _int_field(data, "file_size")
    return value if value is not None and value >= 0 else 0


def _is_file_id(value: str) -> bool:
    """Return True when ``value`` is a Bot API file id (the media-id check the roadmap carried)."""
    return _FILE_ID_PATTERN.fullmatch(value) is not None


def _int_value(value: object) -> int | None:
    """Return ``value`` when it is an int that is not a bool."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _int_field(data: object, key: str) -> int | None:
    """Return ``data[key]`` when ``data`` is a dict and the value is an int that is not a bool."""
    if not isinstance(data, dict):
        return None
    return _int_value(data.get(key))


def _str_field(data: object, key: str) -> str | None:
    """Return ``data[key]`` when ``data`` is a dict and the value is a string."""
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    return value if isinstance(value, str) else None
