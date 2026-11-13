"""WhatsApp webhook routes: handshake, signature check and intake (R-1.2, R-1.3, R-9.15).

The body is untrusted: its size is capped while reading, the signature is
verified over the raw bytes before any JSON parsing, and messages from senders
outside the profile index are only ever logged through their hash. When
Postgres is unreachable the route answers 503 so Meta retries the delivery.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import psycopg
import psycopg_pool
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from invoicebot.channels.base import InboundMessage
from invoicebot.channels.profiles import ProfileIndex
from invoicebot.channels.whatsapp.parse import parse_webhook
from invoicebot.channels.whatsapp.signature import verify_handshake, verify_signature
from invoicebot.pipeline.intake import handle_inbound
from invoicebot.store.db import Pool

logger = logging.getLogger(__name__)

#: Largest webhook body accepted, in bytes, enforced while reading.
MAX_BODY_BYTES = 1024 * 1024


@dataclass(frozen=True)
class WebhookDeps:
    """Everything the WhatsApp webhook routes need, supplied by the runtime wiring."""

    pool: Pool
    index: ProfileIndex
    owner_profile: str
    admin_profiles: frozenset[str]
    app_secret: str
    verify_token: str
    sender_hash_key: str
    max_queued_per_sender: int


def make_router(deps: WebhookDeps) -> APIRouter:
    """Build the WhatsApp webhook router over ``deps``."""
    router = APIRouter()

    @router.get("/webhook/whatsapp")
    async def handshake(request: Request) -> Response:
        """Answer Meta's callback verification with the challenge (R-1.3)."""
        params = request.query_params
        if not verify_handshake(
            params.get("hub.mode"),
            params.get("hub.verify_token"),
            deps.verify_token,
        ):
            return Response(status_code=403)
        return PlainTextResponse(params.get("hub.challenge") or "")

    @router.post("/webhook/whatsapp")
    async def receive(request: Request) -> Response:
        """Verify, parse and persist one signed delivery (R-1.2, R-1.3, R-9.15)."""
        body = await _read_body(request)
        if body is None:
            return Response(status_code=413)
        if not verify_signature(deps.app_secret, body, request.headers.get("x-hub-signature-256")):
            return Response(status_code=401)
        payload = _parse_json(body)
        if payload is None:
            return Response(status_code=400)
        parsed = parse_webhook(
            payload,
            deps.index,
            owner_profile=deps.owner_profile,
            admin_profiles=deps.admin_profiles,
            sender_hash_key=deps.sender_hash_key,
        )
        for digest in dict.fromkeys(parsed.unknown_senders):
            logger.info("ignored sender %s", digest)
        try:
            await _persist(deps, parsed.messages)
        except (psycopg.Error, psycopg_pool.PoolTimeout, psycopg_pool.PoolClosed):
            logger.exception("webhook database error")
            return Response(status_code=503)
        return JSONResponse({"status": "ok"})

    return router


async def _read_body(request: Request) -> bytes | None:
    """Read the raw body, returning ``None`` as soon as it exceeds the cap."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_json(body: bytes) -> dict[str, Any] | None:
    """Decode a webhook body, or return ``None`` when it is not a JSON object."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


async def _persist(deps: WebhookDeps, messages: list[InboundMessage]) -> None:
    """Handle each message in its own pooled connection and transaction."""
    for msg in messages:
        async with deps.pool.connection() as conn:
            await handle_inbound(conn, msg, max_queued_per_sender=deps.max_queued_per_sender)
