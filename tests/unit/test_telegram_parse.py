"""Tests for parsing Telegram Bot API updates into canonical messages (R-1.7, R-1.8)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from invoicebot.channels.base import Attachment, InboundMessage
from invoicebot.channels.profiles import ProfileIndex
from invoicebot.channels.sender_hash import sender_hash
from invoicebot.channels.telegram.client import GETFILE_MAX_BYTES
from invoicebot.channels.telegram.parse import ParsedUpdate, parse_update
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
OWNER_USER_ID = 100000001
UNKNOWN_USER_ID = 100000999
CHAT_ID = OWNER_USER_ID
HASH_KEY = "synthetic-hash-key"
EPOCH = 1700000000
RECEIVED_AT = datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
EMPTY = ParsedUpdate(messages=[], unknown_senders=[], callback_query_ids=[])


@pytest.fixture(scope="module")
def cfg() -> Config:
    return load_config(EXAMPLE)


@pytest.fixture(scope="module")
def index(cfg: Config) -> ProfileIndex:
    return ProfileIndex.from_config(cfg)


def _message(
    *,
    sender: int = OWNER_USER_ID,
    chat_id: int = CHAT_ID,
    chat_type: str = "private",
    message_id: int = 1,
    date: object = EPOCH,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "from": {"id": sender},
        "chat": {"id": chat_id, "type": chat_type},
        "date": date,
        **fields,
    }


def _callback(
    *,
    sender: int = OWNER_USER_ID,
    chat_id: int = CHAT_ID,
    chat_type: str = "private",
    query_id: str = "cbq1",
    data: str = "12:confirm",
    message_id: int = 1,
) -> dict[str, Any]:
    return {
        "id": query_id,
        "from": {"id": sender},
        "data": data,
        "message": {
            "message_id": message_id,
            "chat": {"id": chat_id, "type": chat_type},
        },
    }


def _update(message: dict[str, Any]) -> dict[str, Any]:
    return {"update_id": 1, "message": message}


def _callback_update(**kwargs: Any) -> dict[str, Any]:
    return {"update_id": 2, "callback_query": _callback(**kwargs)}


def _parse(
    update: dict[str, Any],
    index: ProfileIndex,
    *,
    owner_profile: str = "yo",
    admin_profiles: frozenset[str] = frozenset(),
) -> ParsedUpdate:
    return parse_update(
        update,
        index,
        owner_profile=owner_profile,
        admin_profiles=admin_profiles,
        sender_hash_key=HASH_KEY,
        now=NOW,
    )


def _only(parsed: ParsedUpdate) -> InboundMessage:
    assert len(parsed.messages) == 1
    return parsed.messages[0]


def test_text_message_becomes_canonical_message(index: ProfileIndex) -> None:
    """R-1.7: a private text message becomes one canonical InboundMessage with a UTC timestamp."""
    parsed = _parse(_update(_message(text="hola")), index)

    assert parsed == ParsedUpdate(
        messages=[
            InboundMessage(
                channel="telegram",
                external_id=f"{CHAT_ID}:1",
                profile_id="yo",
                sender_role="owner",
                text="hola",
                received_at=RECEIVED_AT,
            )
        ],
        unknown_senders=[],
        callback_query_ids=[],
    )


def test_command_at_bot_name_is_stripped(index: ProfileIndex) -> None:
    """R-1.7: a leading /cmd@bot name becomes /cmd."""
    message = _only(_parse(_update(_message(text="/estado@mk_facturas_bot")), index))

    assert message.text == "/estado"


def test_photo_keeps_the_largest_size_within_the_cap(index: ProfileIndex) -> None:
    """D181: the attachment is the largest photo size whose file_size fits getFile's 20 MB."""
    photo = [
        {"file_id": "small", "file_size": 1024},
        {"file_id": "middle", "file_size": 5 * 1024 * 1024},
        {"file_id": "large", "file_size": GETFILE_MAX_BYTES + 1},
    ]
    message = _only(_parse(_update(_message(photo=photo)), index))

    assert message.attachments == [
        Attachment(kind="photo", media_ref="middle", mime="image/jpeg", size=5 * 1024 * 1024)
    ]


def test_document_kind_follows_mime(index: ProfileIndex) -> None:
    """R-1.7: a PDF document maps to the pdf attachment kind."""
    document = {"file_id": "document1", "mime_type": "application/pdf", "file_size": 2048}
    message = _only(_parse(_update(_message(document=document)), index))

    assert message.attachments == [
        Attachment(kind="pdf", media_ref="document1", mime="application/pdf", size=2048)
    ]


def test_document_with_unsupported_mime_has_no_attachment(index: ProfileIndex) -> None:
    """R-1.7: a document with an unknown mime type keeps the message but no attachment."""
    document = {"file_id": "document2", "mime_type": "application/zip", "file_size": 2048}
    message = _only(_parse(_update(_message(document=document)), index))

    assert message.attachments == []
    assert message.text == ""


def test_photo_caption_is_the_text(index: ProfileIndex) -> None:
    """R-1.7: a photo's caption becomes the message text."""
    photo = [{"file_id": "photo1", "file_size": 1024}]
    message = _only(_parse(_update(_message(photo=photo, caption="mi boleta")), index))

    assert message.text == "mi boleta"


def test_quoted_message_sets_reply_to_external_id(index: ProfileIndex) -> None:
    """D178: a reply carries the replied message id as <chat>:<message id>."""
    message = _only(_parse(_update(_message(text="respondo", reply_to_message={"message_id": 7})), index))

    assert message.reply_to_external_id == f"{CHAT_ID}:7"


def test_callback_query_becomes_a_button_press(index: ProfileIndex) -> None:
    """D177, D178: a callback query is a dedupable button press queued for acknowledgement."""
    parsed = _parse(_callback_update(), index)

    assert parsed.callback_query_ids == ["cbq1"]
    message = _only(parsed)
    assert message.external_id == "cbq1"
    assert message.button_id == "12:confirm"
    assert message.reply_to_external_id == f"{CHAT_ID}:1"
    assert message.received_at == NOW


@pytest.mark.parametrize(
    ("chat_id", "chat_type"),
    [(UNKNOWN_USER_ID, "group"), (UNKNOWN_USER_ID + 1, "private")],
)
def test_non_private_or_foreign_chat_is_dropped_without_a_hash(
    index: ProfileIndex, chat_id: int, chat_type: str
) -> None:
    """D175: a group chat or a chat whose id is not the sender's is dropped without a hash."""
    update = _update(_message(sender=UNKNOWN_USER_ID, chat_id=chat_id, chat_type=chat_type, text="hola"))

    assert _parse(update, index) == EMPTY


def test_unknown_sender_is_skipped_and_only_hashed(index: ProfileIndex) -> None:
    """R-1.7: a non-allowlisted Telegram id yields no message and is kept only as a hash."""
    parsed = _parse(
        _update(_message(sender=UNKNOWN_USER_ID, chat_id=UNKNOWN_USER_ID, text="hola")),
        index,
    )

    assert parsed.messages == []
    assert parsed.unknown_senders == [sender_hash(HASH_KEY, str(UNKNOWN_USER_ID))]
    assert str(UNKNOWN_USER_ID) not in repr(parsed)


def test_media_with_invalid_file_id_is_dropped(index: ProfileIndex) -> None:
    """D181: a file_id outside [A-Za-z0-9_-] drops the whole update."""
    photo = [{"file_id": "bad/id", "file_size": 1024}]

    assert _parse(_update(_message(photo=photo)), index) == EMPTY


def test_string_date_is_dropped(index: ProfileIndex) -> None:
    """R-1.8: a message whose date is not an integer is dropped."""
    assert _parse(_update(_message(date="1700000000", text="hola")), index) == EMPTY


def test_malformed_update_shape_is_empty(index: ProfileIndex) -> None:
    """R-1.7: an update whose message is not an object yields nothing and does not raise."""
    assert _parse({"update_id": 1, "message": []}, index) == EMPTY
