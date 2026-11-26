"""Integration tests for the Telegram webhook route.

Covers R-1.7, R-1.8, D173 and D177 against real Postgres, with the app built
from ``make_router`` and reached through ``httpx.ASGITransport``. The Bot API
sits behind ``httpx.MockTransport`` so every ``answerCallbackQuery`` call is
recorded.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from psycopg import AsyncConnection

from invoicebot.api.intake_route import IntakeDeps
from invoicebot.api.telegram_webhook import TelegramWebhookDeps, make_router
from invoicebot.channels.profiles import ProfileIndex
from invoicebot.channels.sender_hash import sender_hash
from invoicebot.channels.telegram.client import TelegramClient
from invoicebot.config.models import MediaMaxBytes, Profile
from invoicebot.pipeline.handlers import MEGABYTE
from invoicebot.pipeline.intake import NO_ROLES, IntakeSettings
from invoicebot.states import StateMoved
from invoicebot.store.db import Pool

pytestmark = pytest.mark.asyncio(loop_scope="session")

WEBHOOK_KEY = "synthetic-telegram-key"
WRONG_KEY = "synthetic-wrong-key"
HASH_KEY = "synthetic-hash-key"
# Split so the scanner does not read the synthetic value as a committed credential.
BOT_TOKEN = "123456" + ":ABC-fake-token"
EPOCH = 1700000000
MAX_QUEUED = 10
MEDIA_MAX_BYTES = MediaMaxBytes(image=10 * MEGABYTE, pdf=10 * MEGABYTE, xml=MEGABYTE, zip=10 * MEGABYTE)
#: The owner's timezone, for the dates a command prints (S11.4).
TIMEZONE = "America/Lima"
#: The largest body the route accepts, doubled for the over-cap test.
OVERSIZED_BYTES = 2 * MEGABYTE


@dataclass(frozen=True)
class _Sender:
    """A config profile and its Telegram user id, unique to one test."""

    profile_id: str
    user_id: int


@dataclass
class _Bot:
    """A fake Bot API that records every method call and answers a fixed status."""

    calls: list[tuple[str, dict[str, Any]]]
    status: int
    delay: float = 0.0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        """Record one call and answer as the Bot API would, after ``delay`` seconds."""
        self.calls.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content)))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.status == 200:
            return httpx.Response(200, json={"ok": True, "result": True})
        return httpx.Response(
            self.status,
            json={"ok": False, "error_code": self.status, "description": "boom"},
        )


def _bot(*, status: int = 200, delay: float = 0.0) -> _Bot:
    """Return a bot that records calls and answers ``status`` after ``delay`` seconds."""
    return _Bot(calls=[], status=status, delay=delay)


def _sender() -> _Sender:
    """Return a profile id and Telegram user id no other test uses."""
    suffix = uuid4().hex
    return _Sender(
        profile_id=f"telegram-webhook-{suffix}",
        user_id=1_000_000_000 + int(suffix[:9], 16) % 1_000_000_000,
    )


def _unknown_user_id() -> int:
    """Return a positive user id that is not in the profile index."""
    return 2_000_000_000 + uuid4().int % 1_000_000_000


def _deps(pool: Pool, sender: _Sender, client: TelegramClient) -> TelegramWebhookDeps:
    """Build the webhook dependencies around one allowlisted sender."""
    profile = Profile(
        name="Telegram sender",
        legal_name="Telegram Sender",
        telegram_user_id=sender.user_id,
        preferred_channel="telegram",
        flows=["personal_google"],
        admin=True,
    )
    return TelegramWebhookDeps(
        intake_deps=IntakeDeps(
            pool=pool,
            intake=IntakeSettings(
                max_queued_per_sender=MAX_QUEUED,
                media_max_bytes=MEDIA_MAX_BYTES,
                timezone=TIMEZONE,
                roles=NO_ROLES,
            ),
        ),
        index=ProfileIndex({sender.profile_id: profile}),
        owner_profile=sender.profile_id,
        webhook_secret=WEBHOOK_KEY,
        sender_hash_key=HASH_KEY,
        client=client,
    )


@asynccontextmanager
async def _client(pool: Pool, sender: _Sender, bot: _Bot) -> AsyncIterator[httpx.AsyncClient]:
    """Yield a webhook client whose Bot API calls reach ``bot`` and are recorded."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(bot.handler)) as bot_http:
        app = FastAPI()
        app.include_router(make_router(_deps(pool, sender, TelegramClient(bot_http, token=BOT_TOKEN))))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def _text_update(user_id: int, *, message_id: int = 1) -> dict[str, Any]:
    """Return one private text update from ``user_id``."""
    return {
        "update_id": message_id,
        "message": {
            "message_id": message_id,
            "from": {"id": user_id},
            "chat": {"id": user_id, "type": "private"},
            "date": EPOCH,
            "text": "hola",
        },
    }


KEPT_ROW = [{"text": "Abrir", "url": "https://example.com/doc"}]


def _callback_update(
    user_id: int, *, query_id: str, keyboard: list[list[dict[str, str]]] | None = None
) -> dict[str, Any]:
    """Return one button press from ``user_id`` on a message carrying ``keyboard``."""
    update: dict[str, Any] = {
        "update_id": 1,
        "callback_query": {
            "id": query_id,
            "from": {"id": user_id},
            "data": "12:confirm",
            "message": {"message_id": 1, "chat": {"id": user_id, "type": "private"}},
        },
    }
    if keyboard is not None:
        update["callback_query"]["message"]["reply_markup"] = {"inline_keyboard": keyboard}
    return update


def _body(payload: dict[str, Any]) -> bytes:
    """Encode an update as the body Telegram would send."""
    return json.dumps(payload).encode("utf-8")


async def _post(
    client: httpx.AsyncClient, body: bytes, *, secret: str | None = WEBHOOK_KEY
) -> httpx.Response:
    """POST body to the webhook, carrying ``secret`` unless it is None."""
    headers = {} if secret is None else {"X-Telegram-Bot-Api-Secret-Token": secret}
    return await client.post("/webhook/telegram", content=body, headers=headers)


async def _message_channels(pool: Pool, profile_id: str) -> list[str]:
    """Return the channels of the messages stored for ``profile_id``."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT channel FROM messages WHERE profile_id = %s", (profile_id,))
        return [row[0] for row in await cur.fetchall()]


async def test_wrong_or_missing_secret_is_rejected_and_stores_nothing(db_pool: Pool) -> None:
    """D173: a wrong or missing secret header gets 401 before anything is stored."""
    sender = _sender()
    bot = _bot()
    body = _body(_text_update(sender.user_id))
    async with _client(db_pool, sender, bot) as client:
        wrong = await _post(client, body, secret=WRONG_KEY)
        missing = await _post(client, body, secret=None)

    assert (wrong.status_code, missing.status_code) == (401, 401)
    assert await _message_channels(db_pool, sender.profile_id) == []
    assert bot.calls == []


async def test_wrong_secret_with_oversize_body_returns_401(db_pool: Pool) -> None:
    """D173: the secret is checked first, so a wrong secret gets 401 without reading the body."""
    sender = _sender()
    body = b"{" + b" " * OVERSIZED_BYTES + b"}"
    async with _client(db_pool, sender, _bot()) as client:
        response = await _post(client, body, secret=WRONG_KEY)

    assert response.status_code == 401


async def test_body_over_the_cap_returns_413(db_pool: Pool) -> None:
    """D173: a body past the read cap answers 413 before it is parsed."""
    sender = _sender()
    bot = _bot()
    body = b"{" + b" " * OVERSIZED_BYTES + b"}"
    async with _client(db_pool, sender, bot) as client:
        response = await _post(client, body)

    assert response.status_code == 413
    assert await _message_channels(db_pool, sender.profile_id) == []


async def test_text_update_from_known_user_stores_one_telegram_message(db_pool: Pool) -> None:
    """R-1.7: a text update from an allowlisted user gets 200 and one messages row."""
    sender = _sender()
    async with _client(db_pool, sender, _bot()) as client:
        response = await _post(client, _body(_text_update(sender.user_id)))

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert await _message_channels(db_pool, sender.profile_id) == ["telegram"]


async def test_redelivered_update_stores_one_message(db_pool: Pool) -> None:
    """R-1.8: the same update twice is acknowledged twice but stored once."""
    sender = _sender()
    body = _body(_text_update(sender.user_id))
    async with _client(db_pool, sender, _bot()) as client:
        first = await _post(client, body)
        second = await _post(client, body)

    assert (first.status_code, second.status_code) == (200, 200)
    assert await _message_channels(db_pool, sender.profile_id) == ["telegram"]


async def test_callback_query_is_answered_once(db_pool: Pool) -> None:
    """D177: a button press gets 200 and exactly one answerCallbackQuery with its id."""
    sender = _sender()
    bot = _bot()
    query_id = f"cbq-{uuid4().hex}"
    async with _client(db_pool, sender, bot) as client:
        response = await _post(client, _body(_callback_update(sender.user_id, query_id=query_id)))

    assert response.status_code == 200
    assert bot.calls == [
        ("answerCallbackQuery", {"callback_query_id": query_id}),
        (
            "editMessageReplyMarkup",
            {"chat_id": sender.user_id, "message_id": 1, "reply_markup": {"inline_keyboard": []}},
        ),
    ]


async def test_stored_press_edits_the_keyboard_keeping_the_url_buttons(db_pool: Pool) -> None:
    """D195: a stored press is acknowledged, then the keyboard is edited to the kept URL row."""
    sender = _sender()
    bot = _bot()
    query_id = f"cbq-{uuid4().hex}"
    keyboard = [[{"text": "Confirmar", "callback_data": "12:confirm"}], KEPT_ROW]
    async with _client(db_pool, sender, bot) as client:
        response = await _post(
            client, _body(_callback_update(sender.user_id, query_id=query_id, keyboard=keyboard))
        )

    assert response.status_code == 200
    assert bot.calls == [
        ("answerCallbackQuery", {"callback_query_id": query_id}),
        (
            "editMessageReplyMarkup",
            {"chat_id": sender.user_id, "message_id": 1, "reply_markup": {"inline_keyboard": [KEPT_ROW]}},
        ),
    ]


async def test_press_is_not_settled_when_intake_answers_503(
    db_pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D195: when intake does not answer 200 neither the acknowledgement nor the keyboard edit is sent."""
    sender = _sender()
    bot = _bot()

    async def moved(conn: AsyncConnection[Any], msg: Any, **kwargs: Any) -> None:
        raise StateMoved(1, "EXTRACT_FAILED", "CANCELLED")

    monkeypatch.setattr("invoicebot.api.intake_route.handle_inbound", moved)
    async with _client(db_pool, sender, bot) as client:
        response = await _post(
            client,
            _body(_callback_update(sender.user_id, query_id=f"cbq-{uuid4().hex}", keyboard=[KEPT_ROW])),
        )

    assert response.status_code == 503
    assert bot.calls == []


async def test_failed_keyboard_edit_still_returns_200_and_logs_one_warning(
    db_pool: Pool, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D195: an editMessageReplyMarkup answering 400 is logged once and the update still gets 200."""
    sender = _sender()
    bot = _bot()
    handler = bot.handler

    async def edit_fails(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/editMessageReplyMarkup"):
            bot.status = 400
        return await handler(request)

    monkeypatch.setattr(bot, "handler", edit_fails)
    with caplog.at_level(logging.WARNING, logger="invoicebot.api.telegram_webhook"):
        async with _client(db_pool, sender, bot) as client:
            response = await _post(
                client,
                _body(_callback_update(sender.user_id, query_id=f"cbq-{uuid4().hex}", keyboard=[KEPT_ROW])),
            )

    assert response.status_code == 200
    assert [call[0] for call in bot.calls] == ["answerCallbackQuery", "editMessageReplyMarkup"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "editMessageReplyMarkup failed" in warnings[0].getMessage()


async def test_text_message_edits_no_keyboard(db_pool: Pool) -> None:
    """D195: a plain text message never calls editMessageReplyMarkup."""
    sender = _sender()
    bot = _bot()
    async with _client(db_pool, sender, bot) as client:
        response = await _post(client, _body(_text_update(sender.user_id)))

    assert response.status_code == 200
    assert all(name != "editMessageReplyMarkup" for name, _ in bot.calls)


async def test_failed_acknowledgement_still_returns_200(
    db_pool: Pool, caplog: pytest.LogCaptureFixture
) -> None:
    """D177: a failed answerCallbackQuery is logged by class only and the update still gets 200."""
    sender = _sender()
    query_id = f"cbq-{uuid4().hex}"
    with caplog.at_level(logging.WARNING, logger="invoicebot.api.telegram_webhook"):
        async with _client(db_pool, sender, _bot(status=500)) as client:
            response = await _post(client, _body(_callback_update(sender.user_id, query_id=query_id)))

    assert response.status_code == 200
    assert "answerCallbackQuery failed" in caplog.text
    assert "boom" not in caplog.text  # the error's description is never logged


async def test_callback_query_is_not_answered_when_intake_answers_503(
    db_pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D177: when intake answers 503 the button press is not acknowledged, so Telegram redelivers."""
    sender = _sender()
    bot = _bot()
    query_id = f"cbq-{uuid4().hex}"

    async def moved(conn: AsyncConnection[Any], msg: Any, **kwargs: Any) -> None:
        raise StateMoved(1, "EXTRACT_FAILED", "CANCELLED")

    monkeypatch.setattr("invoicebot.api.intake_route.handle_inbound", moved)
    async with _client(db_pool, sender, bot) as client:
        response = await _post(client, _body(_callback_update(sender.user_id, query_id=query_id)))

    assert response.status_code == 503
    assert bot.calls == []


async def test_slow_acknowledgement_still_returns_200(
    db_pool: Pool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """D177: a button press whose acknowledgement times out is logged and the update still gets 200."""
    monkeypatch.setattr("invoicebot.api.telegram_webhook.ANSWER_TIMEOUT_SECONDS", 0.05)
    sender = _sender()
    query_id = f"cbq-{uuid4().hex}"
    with caplog.at_level(logging.WARNING, logger="invoicebot.api.telegram_webhook"):
        async with _client(db_pool, sender, _bot(delay=1.0)) as client:
            response = await _post(client, _body(_callback_update(sender.user_id, query_id=query_id)))

    assert response.status_code == 200
    assert any(
        record.levelno == logging.WARNING and getattr(record, "error_type", None) == "TimeoutError"
        for record in caplog.records
    )


async def test_unknown_sender_is_ignored_without_storing_or_answering(
    db_pool: Pool, caplog: pytest.LogCaptureFixture
) -> None:
    """R-1.7: an unknown sender gets 200, stores nothing, is logged only by digest and is not answered."""
    sender = _sender()
    unknown_id = _unknown_user_id()
    bot = _bot()
    query_id = f"cbq-{uuid4().hex}"
    with caplog.at_level(logging.INFO, logger="invoicebot.api.telegram_webhook"):
        async with _client(db_pool, sender, bot) as client:
            response = await _post(client, _body(_callback_update(unknown_id, query_id=query_id)))

    assert response.status_code == 200
    assert await _message_channels(db_pool, sender.profile_id) == []
    assert bot.calls == []
    assert f"ignored sender {sender_hash(HASH_KEY, str(unknown_id))}" in caplog.text
    assert str(unknown_id) not in caplog.text
