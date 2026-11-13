"""Integration tests for invoice, file and channel-window stores (R-1.1, R-1.4, R-1.10, BR-S01)."""

import asyncio
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import ChannelName
from invoicebot.extract.fields import Field, Reading
from invoicebot.queue.jobs import enqueue
from invoicebot.store.invoices import (
    FileRecord,
    InvoiceHead,
    add_file,
    count_active_jobs,
    create_invoice,
    get_file_path,
    get_state,
    has_file,
    load_invoice_head,
    load_reading,
    save_reading,
    set_flow,
    set_state,
    touch_window,
    window_open,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNEL: ChannelName = "whatsapp"
#: An id no invoice can have, because the identity starts at 1.
UNKNOWN_INVOICE_ID = 2_147_483_647
#: From tests/synthetic_rucs.txt; no real RUC may appear here.
SYNTHETIC_RUC = "20000000001"
#: An eight-digit DNI the buyer field carries; too short to be mistaken for a RUC.
BUYER_DNI = "12345678"
#: The issuing date of the synthetic invoice, as a date column returns it.
ISSUED_ON = date(2026, 11, 13)


def _run_alembic(url: str, revision: str, *, downgrade: bool = False) -> None:
    """Run Alembic against url; call it through asyncio.to_thread like conftest does."""
    config = Config(str(REPO_ROOT / "alembic.ini"))
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        if downgrade:
            command.downgrade(config, revision)
        else:
            command.upgrade(config, revision)
    finally:
        if previous is None:
            del os.environ["DATABASE_URL"]
        else:
            os.environ["DATABASE_URL"] = previous


async def _has_table(pool: AsyncConnectionPool[AsyncConnection[Any]], name: str) -> bool:
    """Return True when the public schema has table name."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = %s)",
            (name,),
        )
        row = await cur.fetchone()
    return row is not None and bool(row[0])


def _reading() -> Reading:
    """Return a complete reading whose fields differ in source and confidence."""
    return Reading(
        fields={
            "ruc_emisor": Field(value=SYNTHETIC_RUC, source="qr", confidence=1.0),
            "tipo": Field(value="01", source="qr", confidence=1.0),
            "serie": Field(value="F001", source="qr", confidence=1.0),
            "numero": Field(value="15160", source="xml", confidence=0.9),
            "fecha_emision": Field(value="2026-11-13", source="qr", confidence=1.0),
            "igv": Field(value="7.36", source="xml", confidence=0.8),
            "total": Field(value="49.90", source="qr", confidence=1.0),
            "num_doc_adq": Field(value=BUYER_DNI, source="qr", confidence=1.0),
        },
        document_url=None,
    )


async def test_create_invoice_returns_id_and_state(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.1: a new invoice is RECEIVED for its profile; an unknown id has no state."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        state = await get_state(conn, invoice_id)
        missing = await get_state(conn, UNKNOWN_INVOICE_ID)

    assert state == ("RECEIVED", "yo")
    assert missing is None


async def test_load_invoice_head_returns_the_row_or_none(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.1: load_invoice_head returns the state, profile and channel; an unknown id is None."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        head = await load_invoice_head(conn, invoice_id)
        missing = await load_invoice_head(conn, UNKNOWN_INVOICE_ID)

    assert head == InvoiceHead(state="RECEIVED", profile_id="yo", origin_channel=CHANNEL)
    assert missing is None


async def test_set_flow_stores_the_flow(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.1: set_flow stores the flow the invoice was routed to."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await set_flow(conn, invoice_id, "personal_google")
        cur = await conn.execute("SELECT flow_id FROM invoices WHERE id = %s", (invoice_id,))
        row = await cur.fetchone()

    assert row == ("personal_google",)


async def test_set_state_records_state_rule_and_error(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.1: set_state writes the new state, its rule and its error."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await set_state(conn, invoice_id, "CANCELLED", rule_id="BR-E07", error="too_large")
        cur = await conn.execute("SELECT state, rule_id, error FROM invoices WHERE id = %s", (invoice_id,))
        row = await cur.fetchone()

    assert row == ("CANCELLED", "BR-E07", "too_large")


async def test_count_active_jobs_ignores_parked_jobs(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S01: active jobs are counted per profile; a job parked at infinity is not."""
    async with db_pool.connection() as conn:
        first = await create_invoice(conn, "yo", CHANNEL)
        second = await create_invoice(conn, "yo", CHANNEL)
        other = await create_invoice(conn, "asistente", CHANNEL)
        parked_job = await enqueue(conn, first, "download_media")
        await enqueue(conn, second, "download_media")
        await enqueue(conn, other, "download_media")

        assert await count_active_jobs(conn, "yo") == 2
        assert await count_active_jobs(conn, "asistente") == 1

        await conn.execute("UPDATE jobs SET run_after = 'infinity'::timestamptz WHERE id = %s", (parked_job,))
        assert await count_active_jobs(conn, "yo") == 1

        # Leave no jobs for the config profiles: later tests count them per profile.
        await conn.execute("DELETE FROM jobs WHERE invoice_id = ANY(%s)", ([first, second, other],))


async def test_add_file_stores_sha256_and_size(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.4: a stored file keeps the bytes' SHA-256 and size."""
    digest = "a" * 64
    size = 4321

    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        record = FileRecord(
            kind="photo",
            local_path="data/photo.jpg",
            remote_ref="media-1",
            sha256=digest,
            size=size,
        )
        file_id = await add_file(conn, invoice_id, record)
        cur = await conn.execute(
            "SELECT kind, local_path, remote_ref, sha256, size FROM files WHERE id = %s",
            (file_id,),
        )
        row = await cur.fetchone()

    assert row == ("photo", "data/photo.jpg", "media-1", digest, size)


async def test_has_file_is_false_before_and_true_after_add_file(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.4: has_file reports whether the invoice already has a stored file."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        before = await has_file(conn, invoice_id)
        record = FileRecord(
            kind="photo",
            local_path="data/photo.jpg",
            remote_ref="media-1",
            sha256="a" * 64,
            size=4321,
        )
        await add_file(conn, invoice_id, record)
        after = await has_file(conn, invoice_id)

    assert before is False
    assert after is True


async def test_touch_window_keeps_the_newest_time(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.10: touching with an older time keeps the newer one; 24 h is the cutoff."""
    async with db_pool.connection() as conn:
        cur = await conn.execute("SELECT now() - interval '1 hour', now() - interval '25 hours'")
        times = await cur.fetchone()
        assert times is not None
        recent, stale = times[0], times[1]

        await touch_window(conn, CHANNEL, "yo", recent)
        await touch_window(conn, CHANNEL, "yo", stale)
        await touch_window(conn, CHANNEL, "asistente", stale)

        cur = await conn.execute(
            "SELECT last_inbound_at FROM channel_windows WHERE channel = %s AND recipient = %s",
            (CHANNEL, "yo"),
        )
        stored = await cur.fetchone()
        assert stored is not None
        assert stored[0] == recent

        assert await window_open(conn, CHANNEL, "yo") is True
        assert await window_open(conn, CHANNEL, "asistente") is False
        assert await window_open(conn, CHANNEL, "desconocido") is False

        # Leave no windows for the config profiles: later tests read them per profile.
        await conn.execute(
            "DELETE FROM channel_windows WHERE channel = %s AND recipient = ANY(%s)",
            (CHANNEL, ["yo", "asistente", "desconocido"]),
        )


async def test_reading_round_trips_through_fields_json(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-2.6: save_reading then load_reading keeps every value, source and confidence."""
    reading = _reading()

    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await save_reading(conn, invoice_id, reading)
        loaded = await load_reading(conn, invoice_id)

    assert loaded == reading


async def test_save_reading_writes_the_typed_columns(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-2.6: the typed columns carry the RUC, buyer number, date and Decimal amounts."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await save_reading(conn, invoice_id, _reading())
        cur = await conn.execute(
            """
            SELECT ruc_emisor, tipo, serie, numero, fecha, igv, total, ruc_adq
            FROM invoices
            WHERE id = %s
            """,
            (invoice_id,),
        )
        row = await cur.fetchone()

    assert row == (
        int(SYNTHETIC_RUC),
        "01",
        "F001",
        "15160",
        ISSUED_ON,
        Decimal("7.36"),
        Decimal("49.90"),
        BUYER_DNI,
    )


async def test_load_reading_is_none_before_a_reading_is_saved(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-2.6: an invoice with no reading, or an unknown id, loads as None."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        unsaved = await load_reading(conn, invoice_id)
        missing = await load_reading(conn, UNKNOWN_INVOICE_ID)

    assert unsaved is None
    assert missing is None


async def test_get_file_path_returns_the_first_path_of_a_kind(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.4: get_file_path returns the stored path of the kind, and None when there is none."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await add_file(
            conn,
            invoice_id,
            FileRecord(kind="photo", local_path="data/photo.jpg", remote_ref=None, sha256="a" * 64, size=1),
        )
        await add_file(
            conn,
            invoice_id,
            FileRecord(kind="xml", local_path="data/invoice.xml", remote_ref=None, sha256="b" * 64, size=2),
        )
        photo = await get_file_path(conn, invoice_id, "photo")
        xml = await get_file_path(conn, invoice_id, "xml")
        absent_kind = await get_file_path(conn, invoice_id, "pdf")
        unknown_invoice = await get_file_path(conn, UNKNOWN_INVOICE_ID, "photo")

    assert photo == "data/photo.jpg"
    assert xml == "data/invoice.xml"
    assert absent_kind is None
    assert unknown_invoice is None


async def test_migration_0004_downgrades_to_0003_and_upgrades_again(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.4: migration 0004 upgrades, downgrades to 0003 and upgrades again cleanly."""
    await asyncio.to_thread(_run_alembic, db_pool.conninfo, "head")
    assert await _has_table(db_pool, "channel_windows") is True

    try:
        await asyncio.to_thread(_run_alembic, db_pool.conninfo, "0003", downgrade=True)
        assert await _has_table(db_pool, "channel_windows") is False
    finally:
        # Leave the database at head even if the assertion fails.
        await asyncio.to_thread(_run_alembic, db_pool.conninfo, "head")

    assert await _has_table(db_pool, "channel_windows") is True
