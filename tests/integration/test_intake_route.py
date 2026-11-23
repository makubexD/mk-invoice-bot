"""Integration tests for the shared intake route (R-1.8, R-9.15).

The functions are driven directly and through a tiny FastAPI app against real
Postgres: the body cap is enforced while reading, a batch is stored before the
200, a redelivery is a duplicate and an unavailable database answers 503.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.api.intake_route import MAX_BODY_BYTES, IntakeDeps, intake, parse_json, read_body
from invoicebot.channels.base import Attachment, InboundMessage
from invoicebot.config.models import MediaMaxBytes
from invoicebot.pipeline.handlers import MEGABYTE
from invoicebot.pipeline.intake import NO_ROLES, IntakeSettings
from invoicebot.store.db import Pool, create_pool

pytestmark = pytest.mark.asyncio(loop_scope="session")

MAX_QUEUED = 10
MEDIA_MAX_BYTES = MediaMaxBytes(image=10 * MEGABYTE, pdf=10 * MEGABYTE, xml=MEGABYTE, zip=10 * MEGABYTE)
TIMEZONE = "America/Lima"


def _profile() -> str:
    """Return a profile id no other test uses, so tests cannot interfere."""
    return f"intake-route-{uuid4().hex}"


def _message(profile_id: str) -> InboundMessage:
    """Build one canonical photo message for profile_id."""
    return InboundMessage(
        channel="whatsapp",
        external_id=f"wamid.{uuid4().hex}",
        profile_id=profile_id,
        sender_role="owner",
        attachments=[Attachment(kind="photo", media_ref=f"ref-{uuid4().hex}", mime="image/jpeg", size=1024)],
        received_at=datetime.now(UTC),
    )


def _deps(pool: Pool) -> IntakeDeps:
    """Build the intake dependencies around one pool."""
    return IntakeDeps(
        pool=pool,
        intake=IntakeSettings(
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
            roles=NO_ROLES,
        ),
    )


@asynccontextmanager
async def _cap_client() -> AsyncIterator[httpx.AsyncClient]:
    """Yield a client for an app whose route reads a body through ``read_body``."""
    app = FastAPI()

    @app.post("/intake")
    async def receive(request: Request) -> Response:
        return Response(status_code=200 if await read_body(request) is not None else 413)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


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


async def test_read_body_refuses_a_body_over_the_cap() -> None:
    """R-9.15: a body over MAX_BODY_BYTES is refused with 413 before it is read whole."""
    async with _cap_client() as client:
        accepted = await client.post("/intake", content=b"x" * MAX_BODY_BYTES)
        refused = await client.post("/intake", content=b"x" * (MAX_BODY_BYTES + 1))

    assert accepted.status_code == 200
    assert refused.status_code == 413


async def test_intake_stores_the_whole_batch_before_acknowledging(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.8: every message in a batch is committed before the route answers 200."""
    profile_id = _profile()
    response = await intake(_deps(db_pool), [_message(profile_id) for _ in range(3)])

    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "ok"}
    assert await _counts(db_pool, profile_id) == (3, 3, 3, 3)


async def test_redelivered_message_is_a_duplicate(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.8: the same message twice is acknowledged twice but stored once."""
    profile_id = _profile()
    message = _message(profile_id)
    first = await intake(_deps(db_pool), [message])
    second = await intake(_deps(db_pool), [message])

    assert (first.status_code, second.status_code) == (200, 200)
    assert await _counts(db_pool, profile_id) == (1, 1, 1, 1)


async def test_unavailable_database_returns_503(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.15: a database that cannot accept connections answers 503 so the channel retries."""
    profile_id = _profile()
    closed_pool = create_pool(db_pool.conninfo)
    response = await intake(_deps(closed_pool), [_message(profile_id)])

    assert response.status_code == 503
    assert response.body == b""


def test_deeply_nested_body_is_refused_instead_of_raising() -> None:
    """R-1.3: a body nested past the decoder's recursion limit gets 400, not a crash."""
    assert parse_json(b"[" * 100_000) is None
