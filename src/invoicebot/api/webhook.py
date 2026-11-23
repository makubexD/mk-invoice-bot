"""WhatsApp webhook routes: handshake, signature check and intake (R-1.2, R-1.3, R-9.15).

The body is untrusted: its size is capped while reading, the signature is
verified over the raw bytes before any JSON parsing, and messages from senders
outside the profile index are only ever logged through their hash. When
Postgres is unreachable, or the invoice's state moved under the message, the
route answers 503 so Meta retries the delivery against the new state (D95). A
message the database refuses outright is skipped and the delivery acknowledged
with 200, because no redelivery can ever store it (R-1.8).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse

from invoicebot.api.intake_route import IntakeDeps, intake, parse_json, read_body
from invoicebot.channels.profiles import ProfileIndex
from invoicebot.channels.whatsapp.parse import parse_webhook
from invoicebot.channels.whatsapp.signature import verify_handshake, verify_signature

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebhookDeps:
    """Everything the WhatsApp webhook routes need, supplied by the runtime wiring."""

    intake_deps: IntakeDeps
    index: ProfileIndex
    owner_profile: str
    app_secret: str
    verify_token: str
    sender_hash_key: str


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
        body = await read_body(request)
        if body is None:
            return Response(status_code=413)
        if not verify_signature(deps.app_secret, body, request.headers.get("x-hub-signature-256")):
            return Response(status_code=401)
        payload = parse_json(body)
        if payload is None:
            return Response(status_code=400)
        parsed = parse_webhook(
            payload,
            deps.index,
            owner_profile=deps.owner_profile,
            admin_profiles=deps.intake_deps.intake.roles.admins,
            sender_hash_key=deps.sender_hash_key,
        )
        for digest in dict.fromkeys(parsed.unknown_senders):
            logger.info("ignored sender %s", digest)
        return await intake(deps.intake_deps, parsed.messages)

    return router
