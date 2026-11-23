"""Store an inbound batch, then acknowledge (constitution, R-1.8, R-9.15); every channel's router calls it."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import psycopg
import psycopg_pool
from fastapi import Request, Response
from fastapi.responses import JSONResponse

from invoicebot.channels.base import InboundMessage
from invoicebot.pipeline.intake import IntakeSettings, handle_inbound
from invoicebot.states import StateMoved
from invoicebot.store.db import Pool

logger = logging.getLogger(__name__)

#: Largest webhook body accepted, in bytes, enforced while reading.
MAX_BODY_BYTES = 1024 * 1024


@dataclass(frozen=True)
class IntakeDeps:
    """What storing a batch and answering it need, supplied by the channel's router."""

    pool: Pool
    #: The intake arguments shared with the console CLI (D165).
    intake: IntakeSettings


async def intake(deps: IntakeDeps, messages: list[InboundMessage]) -> Response:
    """Store the messages, mapping a state race or a transient database error to a response."""
    try:
        await persist(deps, messages)
    except StateMoved as moved:
        # An expected race (D95): the redelivery applies to the new state.
        logger.warning("webhook invoice %s moved; asking for redelivery", moved.invoice_id)
        return Response(status_code=503)
    except (psycopg.Error, psycopg_pool.PoolTimeout, psycopg_pool.PoolClosed) as exc:
        # Type and SQLSTATE only: a constraint error's DETAIL echoes the row's values.
        logger.error(
            "webhook database error",
            extra={"error_type": type(exc).__name__, "sqlstate": exc.sqlstate},
        )
        return Response(status_code=503)
    return JSONResponse({"status": "ok"})


async def read_body(request: Request) -> bytes | None:
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


def parse_json(body: bytes) -> dict[str, Any] | None:
    """Decode a webhook body, or return ``None`` when it is not a JSON object."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


async def persist(deps: IntakeDeps, messages: list[InboundMessage]) -> None:
    """Handle each message in its own pooled connection and transaction.

    A message the database refuses outright is logged and skipped, so the rest
    of the delivery is still stored (R-1.8). The log names the error type and
    SQLSTATE only: psycopg's message can echo the refused value.
    """
    for msg in messages:
        try:
            async with deps.pool.connection() as conn:
                await handle_inbound(
                    conn,
                    msg,
                    max_queued_per_sender=deps.intake.max_queued_per_sender,
                    media_max_bytes=deps.intake.media_max_bytes,
                    timezone=deps.intake.timezone,
                    roles=deps.intake.roles,
                )
        except psycopg.errors.DataError as exc:
            logger.error(
                "webhook message refused by the database",
                extra={"error_type": type(exc).__name__, "sqlstate": exc.sqlstate},
            )
