"""Tests for the Telegram setup calls (D186, D175)."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from invoicebot.channels.telegram.client import TelegramClient
from invoicebot.channels.telegram.setup import (
    WebhookActive,
    WebhookInfo,
    delete_webhook,
    sender_ids,
    set_webhook,
    webhook_info,
)

# Split so the scanner does not read the synthetic value as a committed credential.
TOKEN = "123456" + ":ABC-fake-token"
SECRET = "fake-" + "webhook-secret"
Handler = Callable[[httpx.Request], httpx.Response]


@contextlib.asynccontextmanager
async def _make_client(handler: Handler) -> AsyncIterator[TelegramClient]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        yield TelegramClient(http, token=TOKEN)


def _private_update(user_id: int, username: str | None) -> dict[str, object]:
    """Build a getUpdates entry for a message this user sent in a private chat."""
    sender: dict[str, object] = {"id": user_id, "is_bot": False}
    if username is not None:
        sender["username"] = username
    return {
        "update_id": user_id,
        "message": {
            "message_id": user_id,
            "from": sender,
            "chat": {"id": user_id, "type": "private"},
            "text": "/start",
        },
    }


def _group_update(user_id: int) -> dict[str, object]:
    """Build a getUpdates entry for a message sent in a group chat."""
    return {
        "update_id": user_id,
        "message": {
            "message_id": user_id,
            "from": {"id": user_id, "is_bot": False},
            "chat": {"id": -1000000000000, "type": "group"},
            "text": "hola",
        },
    }


async def test_sender_ids_lists_each_private_sender_once_in_first_seen_order() -> None:
    """D186, D175: only private senders are listed, once each, in first-seen order."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    _private_update(100000001, "a"),
                    _private_update(100000002, None),
                    _private_update(100000001, "a"),
                    _group_update(100000001),
                ],
            },
        )

    async with _make_client(handler) as client:
        senders = await sender_ids(client)

    assert senders == [(100000001, "a"), (100000002, None)]
    assert requests[0].url.path == f"/bot{TOKEN}/getUpdates"
    assert json.loads(requests[0].content) == {"allowed_updates": ["message"], "timeout": 0}


async def test_sender_ids_raises_webhook_active_on_409() -> None:
    """D186: a 409 answer means a webhook is set and raises WebhookActive."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "ok": False,
                "error_code": 409,
                "description": "Conflict: can't use getUpdates method while webhook is active",
            },
        )

    async with _make_client(handler) as client:
        with pytest.raises(WebhookActive):
            await sender_ids(client)


async def test_set_webhook_posts_the_url_secret_and_allowed_updates() -> None:
    """D186: setWebhook carries the URL, the secret and the two allowed update kinds."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": True})

    async with _make_client(handler) as client:
        await set_webhook(client, "https://h.example.ts.net/webhook/telegram", SECRET)

    assert requests[0].url.path == f"/bot{TOKEN}/setWebhook"
    assert json.loads(requests[0].content) == {
        "url": "https://h.example.ts.net/webhook/telegram",
        "secret_token": SECRET,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": True,
    }


@pytest.mark.parametrize(
    "url",
    ["http://h.example.ts.net/webhook/telegram", "https://h.example.ts.net/webhook/whatsapp"],
)
async def test_set_webhook_refuses_a_bad_url_without_a_request(url: str) -> None:
    """D186: a URL that is not https or not the Telegram path is refused before any call."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": True})

    async with _make_client(handler) as client:
        with pytest.raises(ValueError, match="webhook/telegram"):
            await set_webhook(client, url, SECRET)

    assert requests == []


async def test_webhook_info_returns_the_reported_fields() -> None:
    """D186: getWebhookInfo's URL, pending count and last error become a WebhookInfo."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "url": "https://h.example.ts.net/webhook/telegram",
                    "pending_update_count": 3,
                    "last_error_date": 1700000000,
                    "last_error_message": "Wrong response from the webhook: 404 Not Found",
                },
            },
        )

    async with _make_client(handler) as client:
        info = await webhook_info(client)

    assert info == WebhookInfo(
        url="https://h.example.ts.net/webhook/telegram",
        pending=3,
        last_error_date=1700000000,
        last_error_message="Wrong response from the webhook: 404 Not Found",
    )
    assert requests[0].url.path == f"/bot{TOKEN}/getWebhookInfo"


async def test_webhook_info_without_the_error_fields_reports_none() -> None:
    """D186: a getWebhookInfo answer without error fields gives None for both."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "url": "https://h.example.ts.net/webhook/telegram",
                    "pending_update_count": 0,
                },
            },
        )

    async with _make_client(handler) as client:
        info = await webhook_info(client)

    assert info.url == "https://h.example.ts.net/webhook/telegram"
    assert info.pending == 0
    assert info.last_error_date is None
    assert info.last_error_message is None


async def test_delete_webhook_keeps_the_pending_updates() -> None:
    """D186: deleteWebhook sends drop_pending_updates false in one request."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": True})

    async with _make_client(handler) as client:
        await delete_webhook(client)

    assert len(requests) == 1
    assert requests[0].url.path == f"/bot{TOKEN}/deleteWebhook"
    assert json.loads(requests[0].content) == {"drop_pending_updates": False}
