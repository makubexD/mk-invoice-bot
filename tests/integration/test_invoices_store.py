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
from invoicebot.states import CANCELLED, FAILED, StateMoved
from invoicebot.store.invoices import (
    Duplicate,
    FileRecord,
    InvoiceHead,
    InvoiceRef,
    add_file,
    claim_natural_key,
    count_active_jobs,
    create_invoice,
    get_file_path,
    get_state,
    has_file,
    load_flow,
    load_invoice_head,
    load_reading,
    park_stale,
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


def _natural_key(numero: str) -> str:
    """Return the key routing claims for the synthetic invoice with ``numero`` (D93)."""
    return f"{SYNTHETIC_RUC}|01|F001|{numero}"


async def _stored_key(conn: AsyncConnection[Any], invoice_id: int) -> str | None:
    """Return the natural key stored on invoice_id, or None when it holds none."""
    cur = await conn.execute("SELECT natural_key FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return None if row[0] is None else str(row[0])


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


async def test_load_flow_is_none_before_routing_and_the_flow_after(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.1: load_flow returns None until set_flow stores the routed flow."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        before = await load_flow(conn, invoice_id)
        await set_flow(conn, invoice_id, "personal_google")
        after = await load_flow(conn, invoice_id)

    assert (before, after) == (None, "personal_google")


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


async def test_set_state_rejects_a_needs_rule_state_without_a_rule(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """S5.2: entering CANCELLED without a rule id is refused before any write."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        with pytest.raises(ValueError, match="CANCELLED needs a rule id"):
            await set_state(conn, invoice_id, "CANCELLED")
        state = await get_state(conn, invoice_id)

    assert state == ("RECEIVED", "yo")


async def test_set_state_rejects_an_unknown_rule_id(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """S5.2: a rule id that is not in the catalog is refused before any write."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        with pytest.raises(ValueError, match="unknown rule id 'BR-X99'"):
            await set_state(conn, invoice_id, "CANCELLED", rule_id="BR-X99")
        state = await get_state(conn, invoice_id)

    assert state == ("RECEIVED", "yo")


async def test_set_state_allows_a_rule_free_state(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """S5.2: DELIVERING is not in NEEDS_RULE, so it moves without a rule id."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await set_state(conn, invoice_id, "DELIVERING")
        cur = await conn.execute("SELECT state, rule_id FROM invoices WHERE id = %s", (invoice_id,))
        row = await cur.fetchone()

    assert row == ("DELIVERING", None)


async def test_set_state_with_a_stale_expected_state_raises_state_moved(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D95: a transition expected from FETCHING_DOCS loses to the CANCELLED row and changes nothing."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await set_state(conn, invoice_id, "CANCELLED", rule_id="BR-E07")
        with pytest.raises(StateMoved) as raised:
            await set_state(conn, invoice_id, "DELIVERING", expected="FETCHING_DOCS")
        state = await get_state(conn, invoice_id)

    assert raised.value.invoice_id == invoice_id
    assert raised.value.expected == "FETCHING_DOCS"
    assert raised.value.actual == "CANCELLED"
    assert state == ("CANCELLED", "yo")


async def test_set_state_with_a_matching_expected_state_moves_the_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D95: the UPDATE matches a row still in the expected state and moves it."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await set_state(conn, invoice_id, "FETCHING_DOCS")
        await set_state(conn, invoice_id, "DELIVERING", expected="FETCHING_DOCS")
        state = await get_state(conn, invoice_id)

    assert state == ("DELIVERING", "yo")


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


async def test_park_stale_parks_only_old_waiting_invoices(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S08: an unanswered CONFIRMING old enough is parked; a recent wait and FETCHING_DOCS are not."""
    async with db_pool.connection() as conn:
        stale = await create_invoice(conn, "yo", CHANNEL)
        await set_state(conn, stale, "CONFIRMING", rule_id="BR-R03")
        recent = await create_invoice(conn, "asistente", CHANNEL)
        await set_state(conn, recent, "CONFIRMING", rule_id="BR-R03")
        fetching = await create_invoice(conn, "yo", CHANNEL)
        await set_state(conn, fetching, "FETCHING_DOCS")

        await conn.execute(
            "UPDATE invoices SET updated_at = now() - interval '25 hours' WHERE id = %s",
            (stale,),
        )
        await conn.execute(
            "UPDATE invoices SET updated_at = now() - interval '23 hours' WHERE id = %s",
            (recent,),
        )
        await conn.execute(
            "UPDATE invoices SET updated_at = now() - interval '48 hours' WHERE id = %s",
            (fetching,),
        )

        refs = await park_stale(conn, 24)
        cur = await conn.execute(
            """
            SELECT state, waiting_state, waiting_rule_id, rule_id
            FROM invoices
            WHERE id = ANY(%s)
            ORDER BY id
            """,
            ([stale, recent, fetching],),
        )
        rows = await cur.fetchall()

    assert refs == [InvoiceRef(id=stale, profile_id="yo", origin_channel=CHANNEL)]
    assert rows == [
        ("PENDING_USER", "CONFIRMING", "BR-R03", "BR-S08"),
        ("CONFIRMING", None, None, "BR-R03"),
        ("FETCHING_DOCS", None, None, None),
    ]


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


async def test_claim_natural_key_stores_the_first_key(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D93: the first invoice to claim a free natural key gets it."""
    key = _natural_key("15160")

    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        duplicate = await claim_natural_key(conn, invoice_id, key)
        stored = await _stored_key(conn, invoice_id)

    assert duplicate is None
    assert stored == key


async def test_claim_natural_key_returns_the_done_holder(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-E04: a DONE holder wins, carrying its id and created_at, and the second key stays unset."""
    key = _natural_key("15161")

    async with db_pool.connection() as conn:
        first = await create_invoice(conn, "yo", CHANNEL)
        assert await claim_natural_key(conn, first, key) is None
        await set_state(conn, first, "DONE")
        cur = await conn.execute("SELECT created_at FROM invoices WHERE id = %s", (first,))
        row = await cur.fetchone()
        assert row is not None
        created_at = row[0]

        second = await create_invoice(conn, "yo", CHANNEL)
        duplicate = await claim_natural_key(conn, second, key)
        second_key = await _stored_key(conn, second)

    assert duplicate == Duplicate(invoice_id=first, created_at=created_at)
    assert second_key is None


@pytest.mark.parametrize(
    ("holder_state", "rule_id", "numero"),
    [(FAILED, "BR-E07", "15162"), (CANCELLED, "BR-E04", "15163")],
)
async def test_claim_natural_key_takes_over_from_a_terminal_holder(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    holder_state: str,
    rule_id: str,
    numero: str,
) -> None:
    """D93: a FAILED or CANCELLED holder gives the key up and the second invoice takes it."""
    key = _natural_key(numero)

    async with db_pool.connection() as conn:
        first = await create_invoice(conn, "yo", CHANNEL)
        assert await claim_natural_key(conn, first, key) is None
        await set_state(conn, first, holder_state, rule_id=rule_id)

        second = await create_invoice(conn, "yo", CHANNEL)
        duplicate = await claim_natural_key(conn, second, key)
        first_key = await _stored_key(conn, first)
        second_key = await _stored_key(conn, second)

    assert duplicate is None
    assert first_key is None
    assert second_key == key


async def test_claim_natural_key_again_by_its_holder_returns_none(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D93: re-routing the same invoice re-claims its own key without a duplicate."""
    key = _natural_key("15164")

    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        first = await claim_natural_key(conn, invoice_id, key)
        second = await claim_natural_key(conn, invoice_id, key)
        stored = await _stored_key(conn, invoice_id)

    assert first is None
    assert second is None
    assert stored == key


async def test_claim_natural_key_replaces_the_invoices_old_key(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D93: an edited numero makes the same invoice claim its new key and drop the old one."""
    old_key = _natural_key("15165")
    new_key = _natural_key("15166")

    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        assert await claim_natural_key(conn, invoice_id, old_key) is None
        duplicate = await claim_natural_key(conn, invoice_id, new_key)
        stored = await _stored_key(conn, invoice_id)

    assert duplicate is None
    assert stored == new_key


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
