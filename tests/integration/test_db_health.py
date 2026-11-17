"""Integration tests for database health (R-9.14) and the closed-pool webhook (R-9.15)."""

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.api.webhook import WebhookDeps, make_router
from invoicebot.app import create_app
from invoicebot.channels.profiles import ProfileIndex
from invoicebot.config.models import MediaMaxBytes, Profile
from invoicebot.pipeline.handlers import MEGABYTE
from invoicebot.store.db import Pool, create_pool, ping

pytestmark = pytest.mark.asyncio(loop_scope="session")

CLOSED_PORT_DSN = "postgresql://invoicebot:invoicebot@127.0.0.1:1/invoicebot_test"

APP_KEY = "synthetic-app-key"
HASH_KEY = "synthetic-hash-key"
HANDSHAKE = "synthetic-verify-token"
EPOCH = "1700000000"
MAX_QUEUED = 10
MEDIA_MAX_BYTES = MediaMaxBytes(image=10 * MEGABYTE, pdf=10 * MEGABYTE, xml=MEGABYTE, zip=10 * MEGABYTE)
TIMEZONE = "America/Lima"


async def _get_health(app: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/health")


def _webhook_app(pool: Pool, profile_id: str, number: str) -> FastAPI:
    """Build the webhook router around one allowlisted sender."""
    profile = Profile(
        name="Webhook sender",
        legal_name="Webhook Sender",
        whatsapp=number,
        preferred_channel="whatsapp",
        flows=["personal_google"],
        admin=True,
    )
    deps = WebhookDeps(
        pool=pool,
        index=ProfileIndex({profile_id: profile}),
        owner_profile=profile_id,
        admin_profiles=frozenset(),
        laptop_flows=frozenset(),
        app_secret=APP_KEY,
        verify_token=HANDSHAKE,
        sender_hash_key=HASH_KEY,
        max_queued_per_sender=MAX_QUEUED,
        media_max_bytes=MEDIA_MAX_BYTES,
        timezone=TIMEZONE,
    )
    app = FastAPI()
    app.include_router(make_router(deps))
    return app


@asynccontextmanager
async def _webhook_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Yield a client that reaches ``app`` through ASGITransport."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _sender() -> tuple[str, str]:
    """Return a profile id and WhatsApp number no other test uses."""
    suffix = uuid4().hex
    return f"db-health-{suffix}", f"51{int(suffix[:9], 16) % 10**9:09d}"


def _photo_body(number: str) -> bytes:
    """Encode one photo delivery as the raw bytes Meta would sign."""
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "messages": [
                                {
                                    "from": number,
                                    "id": f"wamid.{uuid4().hex}",
                                    "timestamp": EPOCH,
                                    "type": "image",
                                    "image": {
                                        "id": f"media-{uuid4().hex}",
                                        "mime_type": "image/jpeg",
                                        "caption": "boleta",
                                    },
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    return json.dumps(payload).encode("utf-8")


async def _post(client: httpx.AsyncClient, body: bytes) -> httpx.Response:
    """POST body to the webhook, signed with the test app secret."""
    digest = hmac.new(APP_KEY.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return await client.post(
        "/webhook/whatsapp",
        content=body,
        headers={"X-Hub-Signature-256": f"sha256={digest}"},
    )


async def _message_count(pool: Pool, profile_id: str) -> int:
    """Return how many messages the profile has stored."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM messages WHERE profile_id = %s", (profile_id,))
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def test_health_ok_when_database_is_up(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.14: /health returns 200 when SELECT 1 succeeds against Postgres."""
    app = create_app(partial(ping, db_pool))
    response = await _get_health(app)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}


async def test_health_degraded_when_pool_cannot_connect() -> None:
    """R-9.14: /health returns 503 when the pool points at a closed port."""
    pool = create_pool(CLOSED_PORT_DSN)
    await pool.open(wait=False)
    try:
        app = create_app(partial(ping, pool))
        response = await _get_health(app)
    finally:
        await pool.close()
    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "db": "down"}


async def test_health_returns_503_with_closed_pool_then_200_after_it_opens(
    test_database_url: str,
) -> None:
    """R-9.14: /health answers 503 while the pool is closed and 200 once it opens."""
    pool = create_pool(test_database_url)
    try:
        app = create_app(partial(ping, pool))
        down = await _get_health(app)
        await pool.open(wait=True)
        up = await _get_health(app)
    finally:
        await pool.close()

    assert down.status_code == 503
    assert down.json() == {"status": "degraded", "db": "down"}
    assert up.status_code == 200
    assert up.json() == {"status": "ok", "db": "ok"}


async def test_closed_pool_webhook_returns_503_then_stores_the_replayed_delivery_once(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    test_database_url: str,
) -> None:
    """R-9.15: with the pool closed the webhook is 503; after it opens the replay is stored once."""
    profile_id, number = _sender()
    body = _photo_body(number)
    pool = create_pool(test_database_url)
    app = _webhook_app(pool, profile_id, number)
    try:
        async with _webhook_client(app) as client:
            down = await _post(client, body)
            assert down.status_code == 503
            assert await _message_count(db_pool, profile_id) == 0

            await pool.open(wait=True)
            first = await _post(client, body)
            second = await _post(client, body)
    finally:
        await pool.close()

    assert (first.status_code, second.status_code) == (200, 200)
    assert await _message_count(db_pool, profile_id) == 1
