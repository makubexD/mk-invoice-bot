"""Tests for parsing Meta WhatsApp webhooks into canonical messages (R-1.2, R-1.9)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from invoicebot.channels.base import Attachment, AttachmentKind, InboundMessage
from invoicebot.channels.profiles import ProfileIndex
from invoicebot.channels.whatsapp.parse import ParsedWebhook, parse_webhook
from invoicebot.channels.whatsapp.signature import sender_hash
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
OWNER_NUMBER = "51999999999"
SECOND_NUMBER = "51988888888"
UNKNOWN_NUMBER = "51900000000"
HASH_KEY = "synthetic-hash-key"
EPOCH = "1700000000"
RECEIVED_AT = datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


@pytest.fixture(scope="module")
def cfg() -> Config:
    return load_config(EXAMPLE)


@pytest.fixture(scope="module")
def index(cfg: Config) -> ProfileIndex:
    return ProfileIndex.from_config(cfg)


def _message(
    *,
    sender: str = OWNER_NUMBER,
    message_id: str = "wamid.1",
    message_type: str = "text",
    timestamp: str = EPOCH,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "from": sender,
        "id": message_id,
        "timestamp": timestamp,
        "type": message_type,
        **fields,
    }


def _payload(*messages: Any) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "messages": list(messages),
                        },
                    }
                ],
            }
        ],
    }


def _parse(
    payload: dict[str, Any],
    index: ProfileIndex,
    *,
    owner_profile: str = "yo",
    admin_profiles: frozenset[str] = frozenset(),
) -> ParsedWebhook:
    return parse_webhook(
        payload,
        index,
        owner_profile=owner_profile,
        admin_profiles=admin_profiles,
        sender_hash_key=HASH_KEY,
    )


def _only(parsed: ParsedWebhook) -> InboundMessage:
    assert len(parsed.messages) == 1
    return parsed.messages[0]


def test_text_message_becomes_canonical_message(index: ProfileIndex) -> None:
    """R-1.9: a text message becomes one canonical InboundMessage with a UTC timestamp."""
    parsed = _parse(_payload(_message(text={"body": "hola"})), index)

    assert parsed == ParsedWebhook(
        messages=[
            InboundMessage(
                channel="whatsapp",
                external_id="wamid.1",
                profile_id="yo",
                sender_role="owner",
                text="hola",
                received_at=RECEIVED_AT,
            )
        ],
        unknown_senders=[],
    )


def test_image_with_caption_is_a_photo(index: ProfileIndex) -> None:
    """R-1.9: an image maps to a photo attachment referenced by media id, caption as text."""
    image = {"id": "MEDIA1", "mime_type": "image/jpeg", "caption": "mi boleta"}
    message = _only(_parse(_payload(_message(message_type="image", image=image)), index))

    assert message.attachments == [Attachment(kind="photo", media_ref="MEDIA1", mime="image/jpeg", size=0)]
    assert message.text == "mi boleta"


@pytest.mark.parametrize(
    ("mime", "kind"),
    [
        ("application/pdf", "pdf"),
        ("text/xml", "xml"),
        ("application/xml", "xml"),
    ],
)
def test_document_kind_follows_mime(index: ProfileIndex, mime: str, kind: AttachmentKind) -> None:
    """R-1.9: a PDF or XML document maps to its attachment kind."""
    document = {"id": "MEDIA2", "mime_type": mime}
    message = _only(_parse(_payload(_message(message_type="document", document=document)), index))

    assert message.attachments == [Attachment(kind=kind, media_ref="MEDIA2", mime=mime, size=0)]


def test_image_document_is_a_photo(index: ProfileIndex) -> None:
    """R-1.9: an image sent as a document still maps to the photo kind."""
    document = {"id": "MEDIA3", "mime_type": "image/png"}
    message = _only(_parse(_payload(_message(message_type="document", document=document)), index))

    assert message.attachments == [Attachment(kind="photo", media_ref="MEDIA3", mime="image/png", size=0)]


@pytest.mark.parametrize(
    ("interactive", "button_id"),
    [
        ({"button_reply": {"id": "12:confirm", "title": "Sí"}}, "12:confirm"),
        ({"list_reply": {"id": "12:cancel", "title": "No"}}, "12:cancel"),
    ],
)
def test_interactive_reply_sets_button_id(
    index: ProfileIndex, interactive: dict[str, Any], button_id: str
) -> None:
    """R-1.9: a pressed button or list row sets button_id and carries no attachment."""
    message = _only(_parse(_payload(_message(message_type="interactive", interactive=interactive)), index))

    assert message.button_id == button_id
    assert message.attachments == []
    assert message.text == ""


def test_quoted_message_sets_reply_to_external_id(index: ProfileIndex) -> None:
    """R-1.9: context.id from a quoted message becomes reply_to_external_id."""
    message = _only(_parse(_payload(_message(context={"id": "wamid.0"}, text={"body": "respondo"})), index))

    assert message.reply_to_external_id == "wamid.0"


def test_unknown_sender_is_skipped_and_only_hashed(index: ProfileIndex) -> None:
    """R-1.2: a non-allowlisted sender yields no message and is kept only as a hash."""
    parsed = _parse(
        _payload(_message(sender=UNKNOWN_NUMBER, text={"body": "hola"})),
        index,
    )

    assert parsed.messages == []
    assert parsed.unknown_senders == [sender_hash(HASH_KEY, UNKNOWN_NUMBER)]
    assert UNKNOWN_NUMBER not in repr(parsed)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {
            "entry": [
                {
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "statuses": [{"id": "wamid.X", "status": "delivered"}],
                            },
                        }
                    ]
                }
            ]
        },
        {
            "entry": [
                {
                    "changes": [
                        {
                            "field": "message_template_status_update",
                            "value": {"messages": [_message(text={"body": "hola"})]},
                        }
                    ]
                }
            ]
        },
    ],
)
def test_payload_without_messages_is_empty(index: ProfileIndex, payload: dict[str, Any]) -> None:
    """R-1.9: a status-only, empty or non-message change yields nothing and does not raise."""
    assert _parse(payload, index) == ParsedWebhook(messages=[], unknown_senders=[])


@pytest.mark.parametrize(
    ("sender", "owner_profile", "admin_profiles", "role"),
    [
        (OWNER_NUMBER, "yo", frozenset(), "owner"),
        (SECOND_NUMBER, "yo", frozenset({"asistente"}), "admin"),
        (SECOND_NUMBER, "yo", frozenset(), "sender"),
    ],
)
def test_sender_role_follows_the_profile(
    index: ProfileIndex,
    sender: str,
    owner_profile: str,
    admin_profiles: frozenset[str],
    role: str,
) -> None:
    """R-1.2: the sender role comes from the owner and admin profile sets."""
    parsed = _parse(
        _payload(_message(sender=sender, text={"body": "hola"})),
        index,
        owner_profile=owner_profile,
        admin_profiles=admin_profiles,
    )

    assert _only(parsed).sender_role == role


def test_two_messages_keep_payload_order(index: ProfileIndex) -> None:
    """R-1.9: several messages in one payload are returned in payload order."""
    payload = _payload(
        _message(message_id="wamid.1", text={"body": "uno"}),
        _message(message_id="wamid.2", text={"body": "dos"}),
    )
    parsed = _parse(payload, index)

    assert [message.external_id for message in parsed.messages] == ["wamid.1", "wamid.2"]
    assert [message.text for message in parsed.messages] == ["uno", "dos"]


def test_unsupported_message_type_is_empty_but_kept(index: ProfileIndex) -> None:
    """R-1.9: an unsupported message type stays a message with no attachment and empty text."""
    message = _only(_parse(_payload(_message(message_type="audio", audio={"id": "AUDIO1"})), index))

    assert message.attachments == []
    assert message.text == ""


def test_document_with_unsupported_mime_has_no_attachment(index: ProfileIndex) -> None:
    """R-1.9: a document with an unknown mime type keeps the message but no attachment."""
    document = {"id": "MEDIA4", "mime_type": "application/zip"}
    message = _only(_parse(_payload(_message(message_type="document", document=document)), index))

    assert message.attachments == []
    assert message.text == ""


def test_malformed_messages_are_skipped(index: ProfileIndex) -> None:
    """R-1.9: a non-numeric timestamp and a non-dict message are skipped without raising."""
    payload = _payload(
        _message(timestamp="yesterday", text={"body": "hola"}),
        "not a message",
    )

    assert _parse(payload, index) == ParsedWebhook(messages=[], unknown_senders=[])
