"""Conversation parity over the fake, WhatsApp and Telegram inbound paths (S14.1).

The same four conversations run on each inbound path against real Postgres: the fake
path builds the canonical message directly, the WhatsApp path parses a Meta webhook and
the Telegram path parses a Bot API update. Each outbox reply goes through
:class:`FakeChannel`, so every path must produce the same rule id on its own channel.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import Attachment, InboundMessage
from invoicebot.channels.fake import FakeChannel
from invoicebot.channels.profiles import ProfileIndex
from invoicebot.channels.telegram.parse import parse_update
from invoicebot.channels.whatsapp.parse import parse_webhook
from invoicebot.config.loader import load_config
from invoicebot.config.models import MediaMaxBytes
from invoicebot.pipeline.handlers import MEGABYTE
from invoicebot.pipeline.intake import IntakeResult, handle_inbound
from invoicebot.queue.outbox import OutboxSender
from invoicebot.store.invoices import create_invoice, set_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

type InboundPath = Literal["fake", "whatsapp", "telegram"]

EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
#: The example config's full profile, copied under a profile id no other test uses.
FULL_PROFILE = "yo"
MAX_QUEUED = 10
MEDIA_MAX_BYTES = MediaMaxBytes(image=10 * MEGABYTE, pdf=10 * MEGABYTE, xml=MEGABYTE, zip=10 * MEGABYTE)
#: The owner timezone, for the dates a command prints (S11.4).
TIMEZONE = "America/Lima"
#: Keyed digest of a sender outside the allowlist (R-1.2); these tests never send one.
HASH_KEY = "synthetic-hash-key"
PATHS: tuple[InboundPath, ...] = ("fake", "whatsapp", "telegram")
#: One id per Telegram update, so no two updates of a chat share one (R-1.8).
_NEXT_MESSAGE_ID = itertools.count(1)


@dataclass(frozen=True)
class _Identity:
    """A synthetic profile and the channel ids that resolve to it (R-1.9)."""

    profile_id: str
    whatsapp: str
    telegram: int

    def index(self) -> ProfileIndex:
        """Return the profile index that maps both synthetic channel ids to this profile."""
        cfg = load_config(EXAMPLE_CONFIG)
        profile = cfg.profiles[FULL_PROFILE].model_copy(
            update={"whatsapp": self.whatsapp, "telegram_user_id": self.telegram}
        )
        return ProfileIndex.from_config(cfg.model_copy(update={"profiles": {self.profile_id: profile}}))


def _identity() -> _Identity:
    """Return a profile id and channel ids no other test uses."""
    return _Identity(
        profile_id=f"profile-{uuid4().hex}",
        whatsapp=f"51{uuid4().int % 10**9:09d}",
        telegram=10**9 + uuid4().int % 10**9,
    )


def _only(messages: list[InboundMessage]) -> InboundMessage:
    """Return the one canonical message a parser accepted."""
    assert len(messages) == 1
    return messages[0]


def _fake_message(
    identity: _Identity,
    received_at: datetime,
    *,
    text: str,
    photo: bool,
    button_id: str | None,
) -> InboundMessage:
    """Build the canonical message the parsers would produce for the fake path (S14.1)."""
    return InboundMessage(
        channel="whatsapp",
        external_id=f"ext-{uuid4().hex}",
        profile_id=identity.profile_id,
        sender_role="owner",
        text=text,
        attachments=(
            [Attachment(kind="photo", media_ref=f"ref-{uuid4().hex}", mime="image/jpeg", size=1024)]
            if photo
            else []
        ),
        button_id=button_id,
        received_at=received_at,
    )


def _whatsapp_payload(
    identity: _Identity,
    received_at: datetime,
    *,
    text: str,
    photo: bool,
    button_id: str | None,
) -> dict[str, Any]:
    """Return a Meta webhook body carrying one photo, text or button press (R-1.2)."""
    message: dict[str, Any] = {
        "from": identity.whatsapp,
        "id": f"wamid.{uuid4().hex}",
        "timestamp": str(int(received_at.timestamp())),
    }
    if photo:
        message.update(type="image", image={"id": f"ref-{uuid4().hex}", "mime_type": "image/jpeg"})
    elif button_id is not None:
        message.update(
            type="interactive",
            interactive={"type": "button_reply", "button_reply": {"id": button_id, "title": "Confirmar"}},
        )
    else:
        message.update(type="text", text={"body": text})
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {"messaging_product": "whatsapp", "messages": [message]},
                    }
                ],
            }
        ],
    }


def _telegram_update(
    identity: _Identity,
    received_at: datetime,
    *,
    text: str,
    photo: bool,
    button_id: str | None,
) -> dict[str, Any]:
    """Return a Bot API update carrying one photo, text or button press (R-1.7)."""
    message_id = next(_NEXT_MESSAGE_ID)
    if button_id is not None:
        return {
            "update_id": message_id,
            "callback_query": {
                "id": f"cbq-{uuid4().hex}",
                "from": {"id": identity.telegram},
                "data": button_id,
                "message": {
                    "message_id": message_id,
                    "chat": {"id": identity.telegram, "type": "private"},
                },
            },
        }
    message: dict[str, Any] = {
        "message_id": message_id,
        "from": {"id": identity.telegram},
        "chat": {"id": identity.telegram, "type": "private"},
        "date": int(received_at.timestamp()),
    }
    if photo:
        message["photo"] = [{"file_id": f"AgAC-{uuid4().hex}", "file_size": 1024}]
    else:
        message["text"] = text
    return {"update_id": message_id, "message": message}


def _inbound(
    path: InboundPath,
    identity: _Identity,
    *,
    text: str = "",
    photo: bool = False,
    button_id: str | None = None,
) -> InboundMessage:
    """Return the one canonical message ``path`` builds from the same content (S14.1)."""
    received_at = datetime.now(UTC)
    if path == "fake":
        return _fake_message(identity, received_at, text=text, photo=photo, button_id=button_id)
    if path == "whatsapp":
        payload = _whatsapp_payload(identity, received_at, text=text, photo=photo, button_id=button_id)
        parsed = parse_webhook(
            payload,
            identity.index(),
            owner_profile=identity.profile_id,
            admin_profiles=frozenset(),
            sender_hash_key=HASH_KEY,
        )
        return _only(parsed.messages)
    update = _telegram_update(identity, received_at, text=text, photo=photo, button_id=button_id)
    parsed = parse_update(
        update,
        identity.index(),
        owner_profile=identity.profile_id,
        admin_profiles=frozenset(),
        sender_hash_key=HASH_KEY,
        now=received_at,
    )
    return _only(parsed.messages)


async def _clear_outbox(conn: AsyncConnection[Any]) -> None:
    """Remove every outbox row so a test only sends what it queued."""
    await conn.execute("DELETE FROM outbox")


async def _intake(pool: AsyncConnectionPool[AsyncConnection[Any]], msg: InboundMessage) -> IntakeResult:
    """Deliver msg once through the pipeline in its own transaction."""
    async with pool.connection() as conn:
        return await handle_inbound(
            conn,
            msg,
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
        )


def _sender(pool: AsyncConnectionPool[AsyncConnection[Any]], fake: FakeChannel) -> OutboxSender:
    """Build an outbox sender that delivers both chat channels to fake (S14.1)."""
    transport = fake.transport()
    return OutboxSender(pool, {"whatsapp": transport, "telegram": transport})


def _assert_replies(fake: FakeChannel, channel: str, rule_ids: list[str | None]) -> None:
    """Assert fake saw exactly rule_ids, each addressed on the inbound channel (S14.1)."""
    assert fake.rule_ids() == rule_ids
    assert [message.channel for message in fake.sent] == [channel] * len(rule_ids)


@pytest.mark.parametrize("path", PATHS)
async def test_one_photo_acknowledges_on_the_inbound_channel(
    path: InboundPath, db_pool: AsyncConnectionPool[AsyncConnection[Any]]
) -> None:
    """S14.1, R-1.1, BR-S09: one photo is acknowledged once on the inbound channel."""
    identity = _identity()
    fake = FakeChannel()
    msg = _inbound(path, identity, photo=True)

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
    result = await _intake(db_pool, msg)

    assert result.status == "queued"
    assert await _sender(db_pool, fake).send_pending() == 1
    _assert_replies(fake, msg.channel, ["BR-S09"])
    assert result.invoice_id is not None
    assert str(result.invoice_id) in fake.texts()[0]


@pytest.mark.parametrize("path", PATHS)
async def test_redelivered_message_is_stored_once(
    path: InboundPath, db_pool: AsyncConnectionPool[AsyncConnection[Any]]
) -> None:
    """S14.1, R-1.8: the same delivery twice is a duplicate and sends no second message."""
    identity = _identity()
    fake = FakeChannel()
    msg = _inbound(path, identity, photo=True)

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
    first = await _intake(db_pool, msg)
    assert await _sender(db_pool, fake).send_pending() == 1
    second = await _intake(db_pool, msg)

    assert first.status == "queued"
    assert second.status == "duplicate"
    assert await _sender(db_pool, fake).send_pending() == 0
    _assert_replies(fake, msg.channel, ["BR-S09"])


@pytest.mark.parametrize("path", PATHS)
async def test_stale_button_press_gets_br_s02(
    path: InboundPath, db_pool: AsyncConnectionPool[AsyncConnection[Any]]
) -> None:
    """S14.1, BR-S02: a press naming an invoice not waiting for it gets BR-S02."""
    identity = _identity()
    fake = FakeChannel()

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        invoice_id = await create_invoice(conn, identity.profile_id, "whatsapp")
        await set_state(conn, invoice_id, "DONE")
    msg = _inbound(path, identity, button_id=f"{invoice_id}:confirm")
    await _intake(db_pool, msg)

    assert await _sender(db_pool, fake).send_pending() == 1
    _assert_replies(fake, msg.channel, ["BR-S02"])


@pytest.mark.parametrize("path", PATHS)
async def test_estado_from_the_owner_replies_on_the_inbound_channel(
    path: InboundPath, db_pool: AsyncConnectionPool[AsyncConnection[Any]]
) -> None:
    """S14.1, S11.4: /estado from the owner answers once on the inbound channel."""
    identity = _identity()
    fake = FakeChannel()

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        invoice_id = await create_invoice(conn, identity.profile_id, "whatsapp")
        await set_state(conn, invoice_id, "DONE")
    msg = _inbound(path, identity, text=f"/estado {invoice_id}")
    await _intake(db_pool, msg)

    assert await _sender(db_pool, fake).send_pending() == 1
    _assert_replies(fake, msg.channel, [None])
    assert str(invoice_id) in fake.texts()[0]
