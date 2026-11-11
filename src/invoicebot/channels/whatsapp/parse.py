"""Parse Meta WhatsApp Cloud API webhooks into canonical messages (R-1.2, R-1.9).

The webhook body is untrusted: every entry, change and message is checked field
by field and a malformed one is skipped rather than raised. Senders outside the
profile index are reported only through their :func:`sender_hash` digest.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from invoicebot.channels.base import Attachment, AttachmentKind, InboundMessage, SenderRole
from invoicebot.channels.profiles import ProfileIndex
from invoicebot.channels.whatsapp.signature import sender_hash

_MAX_EPOCH_SECONDS = 253402300799  # 9999-12-31T23:59:59Z


@dataclass(frozen=True)
class ParsedWebhook:
    """Messages accepted from one webhook call plus the hashes of rejected senders."""

    messages: list[InboundMessage]
    unknown_senders: list[str]


@dataclass(frozen=True)
class _Content:
    """The canonical fields one message type contributes."""

    text: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    button_id: str | None = None


@dataclass(frozen=True)
class _UnknownSender:
    """A message from a sender outside the allowlist, reduced to its digest."""

    digest: str


type _Builder = Callable[[dict[str, Any]], _Content | None]


def parse_webhook(
    payload: dict[str, Any],
    index: ProfileIndex,
    *,
    owner_profile: str,
    admin_profiles: frozenset[str],
) -> ParsedWebhook:
    """Return the canonical messages of one webhook call, skipping malformed items (R-1.2, R-1.9)."""
    messages: list[InboundMessage] = []
    unknown_senders: list[str] = []
    for entry in _items(payload, "entry"):
        for change in _items(entry, "changes"):
            if _str_field(change, "field") != "messages":
                continue
            value = change.get("value") if isinstance(change, dict) else None
            for raw in _items(value, "messages"):
                if not isinstance(raw, dict):
                    continue
                parsed = _parse_message(
                    raw,
                    index,
                    owner_profile=owner_profile,
                    admin_profiles=admin_profiles,
                )
                if isinstance(parsed, _UnknownSender):
                    unknown_senders.append(parsed.digest)
                elif parsed is not None:
                    messages.append(parsed)
    return ParsedWebhook(messages=messages, unknown_senders=unknown_senders)


def _parse_message(
    raw: dict[str, Any],
    index: ProfileIndex,
    *,
    owner_profile: str,
    admin_profiles: frozenset[str],
) -> InboundMessage | _UnknownSender | None:
    external_id = _str_field(raw, "id")
    sender_id = _str_field(raw, "from")
    message_type = _str_field(raw, "type")
    received_at = _received_at(raw.get("timestamp"))
    if external_id is None or sender_id is None or message_type is None or received_at is None:
        return None
    profile_id = index.resolve("whatsapp", sender_id)
    if profile_id is None:
        return _UnknownSender(digest=sender_hash(sender_id))
    content = _content(message_type, raw)
    if content is None:
        return None
    return InboundMessage(
        channel="whatsapp",
        external_id=external_id,
        profile_id=profile_id,
        sender_role=_sender_role(profile_id, owner_profile, admin_profiles),
        text=content.text,
        attachments=content.attachments,
        reply_to_external_id=_reply_to(raw),
        button_id=content.button_id,
        received_at=received_at,
    )


def _sender_role(profile_id: str, owner_profile: str, admin_profiles: frozenset[str]) -> SenderRole:
    if profile_id == owner_profile:
        return "owner"
    if profile_id in admin_profiles:
        return "admin"
    return "sender"


def _content(message_type: str, message: dict[str, Any]) -> _Content | None:
    """Return the message's content, ``None`` when its type-specific fields are malformed."""
    builder = _BUILDERS.get(message_type)
    if builder is None:
        return _Content()
    return builder(message)


def _text_content(message: dict[str, Any]) -> _Content | None:
    body = _str_field(message.get("text"), "body")
    if body is None:
        return None
    return _Content(text=body)


def _image_content(message: dict[str, Any]) -> _Content | None:
    image = message.get("image")
    fields = _media_fields(image)
    if fields is None:
        return None
    media_ref, mime = fields
    attachment = Attachment(kind="photo", media_ref=media_ref, mime=mime, size=0)
    return _Content(text=_str_field(image, "caption") or "", attachments=[attachment])


def _document_content(message: dict[str, Any]) -> _Content | None:
    fields = _media_fields(message.get("document"))
    if fields is None:
        return None
    media_ref, mime = fields
    kind = _document_kind(mime)
    if kind is None:
        return _Content()
    attachment = Attachment(kind=kind, media_ref=media_ref, mime=mime, size=0)
    return _Content(attachments=[attachment])


def _interactive_content(message: dict[str, Any]) -> _Content | None:
    interactive = message.get("interactive")
    for key in ("button_reply", "list_reply"):
        button_id = _reply_id(interactive, key)
        if button_id is not None:
            return _Content(button_id=button_id)
    return None


def _media_fields(media: object) -> tuple[str, str] | None:
    media_ref = _str_field(media, "id")
    mime = _str_field(media, "mime_type")
    if media_ref is None or mime is None:
        return None
    return media_ref, mime


def _document_kind(mime: str) -> AttachmentKind | None:
    if mime.startswith("image/"):
        return "photo"
    return _DOCUMENT_KINDS.get(mime)


def _reply_id(interactive: object, key: str) -> str | None:
    if not isinstance(interactive, dict):
        return None
    return _str_field(interactive.get(key), "id")


def _reply_to(raw: dict[str, Any]) -> str | None:
    return _str_field(raw.get("context"), "id")


def _received_at(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        seconds = int(value)
    except ValueError:
        return None
    if not 0 <= seconds <= _MAX_EPOCH_SECONDS:
        return None
    return datetime.fromtimestamp(seconds, tz=UTC)


def _items(container: object, key: str) -> list[Any]:
    if not isinstance(container, dict):
        return []
    value = container.get(key)
    return value if isinstance(value, list) else []


def _str_field(data: object, key: str) -> str | None:
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    return value if isinstance(value, str) else None


_DOCUMENT_KINDS: dict[str, AttachmentKind] = {
    "application/pdf": "pdf",
    "text/xml": "xml",
    "application/xml": "xml",
}

_BUILDERS: dict[str, _Builder] = {
    "text": _text_content,
    "image": _image_content,
    "document": _document_content,
    "interactive": _interactive_content,
}
