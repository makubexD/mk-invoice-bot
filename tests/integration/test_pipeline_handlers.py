"""Integration tests for the media job handler and stale-button replies.

Covers R-1.4, R-1.11, R-3.5, BR-E07 and BR-S02 against real Postgres, with
``httpx.MockTransport`` standing in for Meta.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import struct
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import Attachment, InboundMessage
from invoicebot.channels.whatsapp.client import WhatsAppClient, WhatsAppError
from invoicebot.config.models import MediaLimits, MediaMaxBytes
from invoicebot.pipeline.handlers import make_download_handler
from invoicebot.pipeline.intake import DOWNLOAD_STEP, handle_inbound
from invoicebot.pipeline.replies import resolve_reply
from invoicebot.queue.jobs import Job
from invoicebot.store.invoices import create_invoice, set_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

PHONE_NUMBER_ID = "123456789"
GRAPH_VERSION = "v23.0"
BEARER = "synthetic-bearer-value"
MEDIA_URL = "https://mmg.whatsapp.net/v/t62/media"
GRAPH_HOST = "graph.facebook.com"
PNG_MIME = "image/png"
MAX_QUEUED = 10
MEGABYTE = 1024 * 1024
VALID_STATES: dict[str, frozenset[str]] = {"confirm": frozenset({"CONFIRMING"})}

Handler = Callable[[httpx.Request], httpx.Response]

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@contextlib.asynccontextmanager
async def _make_client(handler: Handler) -> AsyncIterator[WhatsAppClient]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        yield WhatsAppClient(
            http,
            phone_number_id=PHONE_NUMBER_ID,
            graph_api_version=GRAPH_VERSION,
            token=BEARER,
        )


def _png_header(width: int, height: int) -> bytes:
    """Build a PNG header declaring the given pixel size."""
    ihdr_data = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    ihdr_chunk = struct.pack(">I", 13) + b"IHDR" + ihdr_data
    return _PNG_SIGNATURE + ihdr_chunk + b"\x00\x00\x00\x00"


def _limits() -> MediaLimits:
    """Limits with a 40 MP pixel cap and synthetic byte caps."""
    return MediaLimits(
        max_megapixels=40,
        max_bytes=MediaMaxBytes(
            image=8 * MEGABYTE,
            pdf=16 * MEGABYTE,
            xml=2 * MEGABYTE,
            zip=4 * MEGABYTE,
        ),
        zip_max_entries=20,
        zip_max_ratio=50,
    )


def _profile() -> str:
    """Return a profile id no other test uses, so tests cannot interfere."""
    return f"profile-{uuid4().hex}"


def _external_id() -> str:
    """Return a channel message id no other test uses."""
    return f"ext-{uuid4().hex}"


def _photo(media_ref: str) -> Attachment:
    """Return a photo attachment for ``media_ref``."""
    return Attachment(kind="photo", media_ref=media_ref, mime=PNG_MIME, size=1024)


def _message(
    profile_id: str,
    *,
    external_id: str,
    attachments: list[Attachment] | None = None,
    button_id: str | None = None,
) -> InboundMessage:
    """Build a canonical inbound message for profile_id."""
    return InboundMessage(
        channel="whatsapp",
        external_id=external_id,
        profile_id=profile_id,
        sender_role="owner",
        text="",
        attachments=[] if attachments is None else attachments,
        button_id=button_id,
        received_at=datetime.now(UTC),
    )


def _job(invoice_id: int) -> Job:
    """Build the download job of invoice_id; only its invoice id is used."""
    return Job(
        id=1,
        invoice_id=invoice_id,
        step=DOWNLOAD_STEP,
        attempts=0,
        crashes=0,
        lease_until=datetime.now(UTC),
    )


async def _queue_photo(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    media_ref: str,
) -> int:
    """Store a photo through intake and return the invoice it created."""
    msg = _message(profile_id, external_id=_external_id(), attachments=[_photo(media_ref)])
    async with pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
    assert result.invoice_id is not None
    return result.invoice_id


async def _invoice_in_state(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    state: str,
) -> int:
    """Create an invoice for profile_id directly in ``state``."""
    async with pool.connection() as conn:
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        if state != "RECEIVED":
            await set_state(conn, invoice_id, state)
    return invoice_id


async def _invoice_row(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str, str | None]:
    """Return ``(state, rule_id)`` of an invoice."""
    cur = await conn.execute("SELECT state, rule_id FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0]), None if row[1] is None else str(row[1])


async def _files(conn: AsyncConnection[Any], invoice_id: int) -> list[tuple[Any, ...]]:
    """Return every files row of an invoice."""
    cur = await conn.execute(
        "SELECT kind, local_path, remote_ref, sha256, size FROM files WHERE invoice_id = %s",
        (invoice_id,),
    )
    return list(await cur.fetchall())


async def _outbox_rows(conn: AsyncConnection[Any], to: str, rule_id: str) -> list[tuple[str, str]]:
    """Return ``(state, text)`` of the outbox rows for ``to`` under ``rule_id``."""
    cur = await conn.execute(
        """
        SELECT state, payload_json->>'text'
        FROM outbox
        WHERE "to" = %s AND payload_json->>'rule_id' = %s
        """,
        (to, rule_id),
    )
    return [(str(state), str(text)) for state, text in await cur.fetchall()]


async def test_valid_photo_stores_the_file_and_keeps_the_invoice_received(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-1.4: a valid photo stores one files row with its hash and leaves the invoice RECEIVED."""
    media_ref = f"ref-{uuid4().hex}"
    body = _png_header(2000, 1500) + b"\x00" * 50

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == GRAPH_HOST:
            return httpx.Response(
                200,
                json={"url": MEDIA_URL, "mime_type": PNG_MIME, "file_size": len(body)},
            )
        return httpx.Response(200, content=body)

    profile_id = _profile()
    invoice_id = await _queue_photo(db_pool, profile_id, media_ref)
    async with _make_client(handler) as client:
        download = make_download_handler(db_pool, client, tmp_path, _limits())
        await download(_job(invoice_id))

    async with db_pool.connection() as conn:
        files = await _files(conn, invoice_id)
        state, rule_id = await _invoice_row(conn, invoice_id)

    assert len(files) == 1
    kind, local_path, remote_ref, sha256, size = files[0]
    assert kind == "photo"
    assert local_path == str(tmp_path / "tmp" / media_ref / "file")
    assert await asyncio.to_thread(Path(local_path).read_bytes) == body
    assert remote_ref == media_ref
    assert sha256 == hashlib.sha256(body).hexdigest()
    assert size == len(body)
    assert (state, rule_id) == ("RECEIVED", None)


async def test_oversized_image_cancels_the_invoice_and_replies_br_e07(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-1.11: a 48 MP header cancels the invoice with BR-E07 and stores no file."""
    body = _png_header(8000, 6000) + b"\x00" * 50

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == GRAPH_HOST:
            return httpx.Response(
                200,
                json={"url": MEDIA_URL, "mime_type": PNG_MIME, "file_size": len(body)},
            )
        return httpx.Response(200, content=body)

    profile_id = _profile()
    invoice_id = await _queue_photo(db_pool, profile_id, f"ref-{uuid4().hex}")
    async with _make_client(handler) as client:
        download = make_download_handler(db_pool, client, tmp_path, _limits())
        await download(_job(invoice_id))

    async with db_pool.connection() as conn:
        files = await _files(conn, invoice_id)
        state, rule_id = await _invoice_row(conn, invoice_id)
        outbox_rows = await _outbox_rows(conn, profile_id, "BR-E07")

    assert files == []
    assert (state, rule_id) == ("CANCELLED", "BR-E07")
    assert len(outbox_rows) == 1
    assert "la imagen es demasiado grande" in outbox_rows[0][1]


async def test_media_info_failure_raises_and_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-1.4: a Meta 500 on media_info raises WhatsAppError and leaves the invoice alone."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    profile_id = _profile()
    invoice_id = await _queue_photo(db_pool, profile_id, f"ref-{uuid4().hex}")
    async with _make_client(handler) as client:
        download = make_download_handler(db_pool, client, tmp_path, _limits())
        with pytest.raises(WhatsAppError):
            await download(_job(invoice_id))

    async with db_pool.connection() as conn:
        state, rule_id = await _invoice_row(conn, invoice_id)
        files = await _files(conn, invoice_id)

    assert (state, rule_id) == ("RECEIVED", None)
    assert files == []


async def test_confirm_button_in_confirming_resolves_to_the_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.5: a button pressed in a valid state resolves to (invoice_id, action) and sends nothing."""
    profile_id = _profile()
    invoice_id = await _invoice_in_state(db_pool, profile_id, "CONFIRMING")
    msg = _message(
        profile_id,
        external_id=_external_id(),
        button_id=f"{invoice_id}:confirm",
    )

    async with db_pool.connection() as conn:
        resolved = await resolve_reply(conn, msg, VALID_STATES)
        outbox_rows = await _outbox_rows(conn, profile_id, "BR-S02")

    assert resolved == (invoice_id, "confirm")
    assert outbox_rows == []


async def test_confirm_button_in_done_sends_br_s02_and_keeps_the_state(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S02: a stale button sends BR-S02, resolves to None and changes nothing."""
    profile_id = _profile()
    invoice_id = await _invoice_in_state(db_pool, profile_id, "DONE")
    msg = _message(
        profile_id,
        external_id=_external_id(),
        button_id=f"{invoice_id}:confirm",
    )

    async with db_pool.connection() as conn:
        resolved = await resolve_reply(conn, msg, VALID_STATES)
        outbox_rows = await _outbox_rows(conn, profile_id, "BR-S02")
        state, rule_id = await _invoice_row(conn, invoice_id)

    assert resolved is None
    assert len(outbox_rows) == 1
    assert "terminado" in outbox_rows[0][1]
    assert (state, rule_id) == ("DONE", None)


async def test_malformed_foreign_and_unknown_buttons_resolve_to_none(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S02: a foreign invoice, a malformed id and an unknown action send nothing."""
    foreign_profile = _profile()
    foreign_invoice = await _invoice_in_state(db_pool, foreign_profile, "CONFIRMING")
    sender_profile = _profile()
    sender_msg = _message(
        sender_profile,
        external_id=_external_id(),
        button_id=f"{foreign_invoice}:confirm",
    )

    own_profile = _profile()
    own_invoice = await _invoice_in_state(db_pool, own_profile, "CONFIRMING")
    malformed_msg = _message(own_profile, external_id=_external_id(), button_id="x")
    unknown_msg = _message(
        own_profile,
        external_id=_external_id(),
        button_id=f"{own_invoice}:borrar",
    )

    async with db_pool.connection() as conn:
        foreign = await resolve_reply(conn, sender_msg, VALID_STATES)
        malformed = await resolve_reply(conn, malformed_msg, VALID_STATES)
        unknown = await resolve_reply(conn, unknown_msg, VALID_STATES)
        sender_outbox = await _outbox_rows(conn, sender_profile, "BR-S02")
        own_outbox = await _outbox_rows(conn, own_profile, "BR-S02")

    assert foreign is None
    assert malformed is None
    assert unknown is None
    assert sender_outbox == []
    assert own_outbox == []
