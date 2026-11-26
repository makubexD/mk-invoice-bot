"""Telegram webhook route: secret check and intake (R-1.7, R-1.8, D173, D177, D195).

The body is untrusted: the ``X-Telegram-Bot-Api-Secret-Token`` header is
compared with the configured secret before the body is read, so an
unauthenticated caller never makes the server read up to the cap; an
authenticated body's size is capped while reading, and only then is it parsed
as JSON. Senders outside the profile index are only ever logged through their
keyed digest. A button press is acknowledged with
``answerCallbackQuery`` right after it is stored (D177); that call is an
acknowledgement, not a message, so it bypasses the outbox and a failure is
logged and ignored. Once the press is stored and acknowledged, the pressed
message's keyboard is edited so its callback buttons go and its URL buttons
stay (D195); like the acknowledgement, that edit is not a message, so it
bypasses the outbox and a failure is logged and ignored.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response

from invoicebot.api.intake_route import IntakeDeps, intake, parse_json, read_body
from invoicebot.channels.profiles import ProfileIndex
from invoicebot.channels.telegram.client import TelegramClient, TelegramError
from invoicebot.channels.telegram.parse import SettledKeyboard, parse_update

logger = logging.getLogger(__name__)

#: The header Telegram signs every webhook call with (D173).
WEBHOOK_HEADER = "X-Telegram-Bot-Api-Secret-Token"
#: How long a button press may wait for its acknowledgement (D177).
ANSWER_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class TelegramWebhookDeps:
    """Everything the Telegram webhook route needs, supplied by the runtime wiring."""

    intake_deps: IntakeDeps
    index: ProfileIndex
    owner_profile: str
    webhook_secret: str
    sender_hash_key: str
    client: TelegramClient


def _secret_matches(header: str | None, secret: str) -> bool:
    """Compare the presented secret with the configured one in constant time (D173)."""
    if header is None:
        return False
    return hmac.compare_digest(header.encode(), secret.encode())


def make_router(deps: TelegramWebhookDeps) -> APIRouter:
    """Build the Telegram webhook router over ``deps``."""
    router = APIRouter()

    @router.post("/webhook/telegram")
    async def receive(request: Request) -> Response:
        """Verify, parse and persist one update (D173, R-1.7, R-1.8)."""
        if not _secret_matches(request.headers.get(WEBHOOK_HEADER), deps.webhook_secret):
            return Response(status_code=401)
        body = await read_body(request)
        if body is None:
            return Response(status_code=413)
        payload = parse_json(body)
        if payload is None:
            return Response(status_code=400)
        parsed = parse_update(
            payload,
            deps.index,
            owner_profile=deps.owner_profile,
            admin_profiles=deps.intake_deps.intake.roles.admins,
            sender_hash_key=deps.sender_hash_key,
            now=datetime.now(UTC),
        )
        for digest in dict.fromkeys(parsed.unknown_senders):
            logger.info("ignored sender %s", digest)
        response = await intake(deps.intake_deps, parsed.messages)
        if response.status_code == 200:
            for query_id in parsed.callback_query_ids:
                await _answer_callback_query(deps.client, query_id)
            for settled in parsed.settled:
                await _settle_keyboard(deps.client, settled)
        return response

    return router


async def _answer_callback_query(client: TelegramClient, query_id: str) -> None:
    """Acknowledge one button press, logging and ignoring a failure (D177)."""
    try:
        async with asyncio.timeout(ANSWER_TIMEOUT_SECONDS):
            await client.answer_callback_query(query_id)
    except (TelegramError, TimeoutError) as exc:
        logger.warning("answerCallbackQuery failed", extra={"error_type": type(exc).__name__})


async def _settle_keyboard(client: TelegramClient, settled: SettledKeyboard) -> None:
    """Drop the pressed message's callback buttons, logging and ignoring a failure (D195)."""
    try:
        async with asyncio.timeout(ANSWER_TIMEOUT_SECONDS):
            await client.edit_reply_markup(settled.chat_id, settled.message_id, settled.url_rows)
    except (TelegramError, TimeoutError) as exc:
        logger.warning("editMessageReplyMarkup failed", extra={"error_type": type(exc).__name__})
