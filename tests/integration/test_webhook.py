"""Integration tests for the WhatsApp webhook routes.

Covers R-1.1, R-1.2, R-1.3, R-1.8, R-1.9 and R-9.15 against real Postgres, with
the app built from ``make_router`` and reached through ``httpx.ASGITransport``.
Every request body is signed in the test with the test app secret.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from psycopg import AsyncConnection

from invoicebot.api.webhook import WebhookDeps, make_router
from invoicebot.channels.profiles import ProfileIndex
from invoicebot.channels.whatsapp.signature import sender_hash
from invoicebot.config.models import Profile
from invoicebot.store.db import Pool, create_pool

pytestmark = pytest.mark.asyncio(loop_scope="session")

APP_KEY = "synthetic-app-key"
WRONG_KEY = "synthetic-wrong-key"
HANDSHAKE = "synthetic-verify-token"
CHALLENGE = "1158201444"
EPOCH = "1700000000"
MAX_QUEUED = 10


@dataclass(frozen=True)
class _Sender:
    """A config profile and its WhatsApp number, unique to one test."""

    profile_id: str
    number: str


def _sender() -> _Sender:
    """Return a profile id and number no other test uses."""
    suffix = uuid4().hex
    return _Sender(
        profile_id=f"webhook-{suffix}",
        number=f"51{int(suffix[:9], 16) % 10**9:09d}",
    )


def _unknown_number() -> str:
    """Return a well-formed number that is not in the profile index."""
    return f"52{uuid4().int % 10**9:09d}"


def _deps(pool: Pool, sender: _Sender) -> WebhookDeps:
    """Build the webhook dependencies around one allowlisted sender."""
    profile = Profile(
        name="Webhook sender",
        whatsapp=sender.number,
        preferred_channel="whatsapp",
        flows=["personal_google"],
        can_send_batch=True,
        admin=True,
    )
    return WebhookDeps(
        pool=pool,
        index=ProfileIndex({sender.profile_id: profile}),
        owner_profile=sender.profile_id,
        admin_profiles=frozenset(),
        app_secret=APP_KEY,
        verify_token=HANDSHAKE,
        max_queued_per_sender=MAX_QUEUED,
    )


@asynccontextmanager
async def _client(deps: WebhookDeps) -> AsyncIterator[httpx.AsyncClient]:
    """Yield a client that calls the app built from ``make_router(deps)``."""
    app = FastAPI()
    app.include_router(make_router(deps))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _signature(body: bytes, secret: str = APP_KEY) -> dict[str, str]:
    """Return the signature header Meta would send for ``body``."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}"}


async def _post(
    client: httpx.AsyncClient,
    body: bytes,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """POST body to the webhook, signing it unless explicit headers are given."""
    return await client.post(
        "/webhook/whatsapp",
        content=body,
        headers=_signature(body) if headers is None else headers,
    )


def _payload(*messages: dict[str, Any]) -> dict[str, Any]:
    """Wrap messages in the Meta webhook envelope."""
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


def _photo_payload(sender_number: str, *, message_id: str | None = None) -> dict[str, Any]:
    """Return a webhook carrying one photo from sender_number."""
    return _payload(
        {
            "from": sender_number,
            "id": message_id or f"wamid.{uuid4().hex}",
            "timestamp": EPOCH,
            "type": "image",
            "image": {"id": f"media-{uuid4().hex}", "mime_type": "image/jpeg", "caption": "boleta"},
        }
    )


def _statuses_payload() -> dict[str, Any]:
    """Return a delivery-status webhook, which carries no message."""
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
                            "statuses": [{"id": f"wamid.{uuid4().hex}", "status": "delivered"}],
                        },
                    }
                ],
            }
        ],
    }


def _body(payload: dict[str, Any]) -> bytes:
    """Encode a webhook as the raw bytes Meta would sign."""
    return json.dumps(payload).encode("utf-8")


async def _scalar(conn: AsyncConnection[Any], query: str, params: tuple[Any, ...]) -> int:
    """Run a query that returns one count and return it."""
    cur = await conn.execute(query, params)
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _counts(pool: Pool, profile_id: str) -> tuple[int, int, int, int]:
    """Return (messages, invoices, jobs, outbox rows) owned by profile_id."""
    async with pool.connection() as conn:
        messages = await _scalar(conn, "SELECT count(*) FROM messages WHERE profile_id = %s", (profile_id,))
        invoices = await _scalar(conn, "SELECT count(*) FROM invoices WHERE profile_id = %s", (profile_id,))
        jobs = await _scalar(
            conn,
            """
            SELECT count(*)
            FROM jobs AS j
            JOIN invoices AS i ON i.id = j.invoice_id
            WHERE i.profile_id = %s
            """,
            (profile_id,),
        )
        outbox = await _scalar(conn, 'SELECT count(*) FROM outbox WHERE "to" = %s', (profile_id,))
    return messages, invoices, jobs, outbox


async def _outbox_rule_ids(pool: Pool, profile_id: str) -> list[str | None]:
    """Return the rule ids of the profile's outbox rows, oldest first."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT payload_json->>'rule_id' FROM outbox WHERE "to" = %s ORDER BY id""",
            (profile_id,),
        )
        rows = await cur.fetchall()
    return [row[0] for row in rows]


async def test_handshake_returns_challenge_only_for_the_verify_token(db_pool: Pool) -> None:
    """R-1.3: the configured token returns the challenge; any other token gets 403."""
    params = {"hub.mode": "subscribe", "hub.verify_token": HANDSHAKE, "hub.challenge": CHALLENGE}
    async with _client(_deps(db_pool, _sender())) as client:
        accepted = await client.get("/webhook/whatsapp", params=params)
        rejected = await client.get("/webhook/whatsapp", params={**params, "hub.verify_token": "wrong-token"})

    assert accepted.status_code == 200
    assert accepted.text == CHALLENGE
    assert rejected.status_code == 403


async def test_bad_or_missing_signature_is_rejected_and_stores_nothing(db_pool: Pool) -> None:
    """R-1.3: a bad or missing signature gets 401 before anything is stored."""
    sender = _sender()
    body = _body(_photo_payload(sender.number))
    async with _client(_deps(db_pool, sender)) as client:
        bad = await _post(client, body, headers=_signature(body, WRONG_KEY))
        missing = await _post(client, body, headers={})

    assert bad.status_code == 401
    assert missing.status_code == 401
    assert await _counts(db_pool, sender.profile_id) == (0, 0, 0, 0)


async def test_signed_photo_queues_one_invoice_job_and_acknowledgement(db_pool: Pool) -> None:
    """R-1.1: a signed photo returns 200 and becomes one invoice, job and BR-S09 row."""
    sender = _sender()
    async with _client(_deps(db_pool, sender)) as client:
        started = time.perf_counter()
        response = await _post(client, _body(_photo_payload(sender.number)))
        elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert elapsed < 1.0
    assert await _counts(db_pool, sender.profile_id) == (1, 1, 1, 1)
    assert await _outbox_rule_ids(db_pool, sender.profile_id) == ["BR-S09"]


async def test_redelivered_body_stores_one_invoice(db_pool: Pool) -> None:
    """R-1.8: the same signed body twice is acknowledged twice but stored once."""
    sender = _sender()
    body = _body(_photo_payload(sender.number))
    async with _client(_deps(db_pool, sender)) as client:
        first = await _post(client, body)
        second = await _post(client, body)

    assert (first.status_code, second.status_code) == (200, 200)
    assert await _counts(db_pool, sender.profile_id) == (1, 1, 1, 1)


async def test_unknown_sender_is_logged_only_as_a_hash(
    db_pool: Pool, caplog: pytest.LogCaptureFixture
) -> None:
    """R-1.2: an unknown sender gets 200, stores nothing and is logged only by hash."""
    sender = _sender()
    unknown_number = _unknown_number()
    body = _body(_photo_payload(unknown_number))
    with caplog.at_level(logging.INFO, logger="invoicebot.api.webhook"):
        async with _client(_deps(db_pool, sender)) as client:
            response = await _post(client, body)

    assert response.status_code == 200
    assert await _counts(db_pool, sender.profile_id) == (0, 0, 0, 0)
    assert f"ignored sender {sender_hash(unknown_number)}" in caplog.text
    assert unknown_number not in caplog.text


async def test_non_json_body_is_rejected_with_400(db_pool: Pool) -> None:
    """R-1.3: once the signature checks out, a body that is not JSON gets 400."""
    async with _client(_deps(db_pool, _sender())) as client:
        response = await _post(client, b"esto no es json")

    assert response.status_code == 400


async def test_closed_pool_returns_503(db_pool: Pool) -> None:
    """R-9.15: a database that cannot accept connections answers 503 so Meta retries."""
    sender = _sender()
    closed_pool = create_pool(db_pool.conninfo)
    async with _client(_deps(closed_pool, sender)) as client:
        response = await _post(client, _body(_photo_payload(sender.number)))

    assert response.status_code == 503
    assert response.content == b""


async def test_statuses_payload_is_acknowledged_without_storing(db_pool: Pool) -> None:
    """R-1.9: a signed status-only payload returns 200 and stores nothing."""
    sender = _sender()
    async with _client(_deps(db_pool, sender)) as client:
        response = await _post(client, _body(_statuses_payload()))

    assert response.status_code == 200
    assert await _counts(db_pool, sender.profile_id) == (0, 0, 0, 0)
