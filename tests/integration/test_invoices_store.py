"""Integration tests for invoice, file and channel-window stores (R-1.1, R-1.4, R-1.10, BR-S01)."""

import asyncio
import os
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import Attachment, AttachmentKind, ChannelName, InboundMessage
from invoicebot.extract.fields import Field, Reading
from invoicebot.queue.jobs import enqueue
from invoicebot.states import BATCHED, CANCELLED, FAILED, StateMoved
from invoicebot.store.flow_b import open_batch
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
    get_waiting,
    has_file,
    list_pending,
    list_unattached_uploads,
    load_flow,
    load_invoice_head,
    load_reading,
    park_stale,
    record_upload_rejection,
    save_reading,
    set_flow,
    set_state,
    set_sunat_status,
    sunat_rechecks,
    touch_window,
    unpark,
    window_open,
)
from invoicebot.store.messages import link_message_to_invoice, store_inbound
from invoicebot.sunat.validez import CpStatus

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


async def _store_upload(
    conn: AsyncConnection[Any],
    invoice_id: int,
    kind: AttachmentKind,
    media_ref: str,
    received_at: datetime,
) -> None:
    """Store one inbound upload and link it to invoice_id, as intake does (R-1.5)."""
    msg = InboundMessage(
        channel=CHANNEL,
        external_id=f"ext-{uuid4().hex}",
        profile_id="yo",
        sender_role="owner",
        attachments=[Attachment(kind=kind, media_ref=media_ref, mime="application/octet-stream", size=1)],
        received_at=received_at,
    )
    assert await store_inbound(conn, msg)
    await link_message_to_invoice(conn, msg.channel, msg.external_id, invoice_id)


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


async def test_get_waiting_returns_the_parked_columns_or_none(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D94: get_waiting returns the stored waiting state and rule; an unknown id is None."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await set_state(conn, invoice_id, "PENDING_USER", rule_id="BR-S08")
        await conn.execute(
            """
            UPDATE invoices
            SET waiting_state = 'EXTRACT_FAILED', waiting_rule_id = 'BR-E01'
            WHERE id = %s
            """,
            (invoice_id,),
        )
        waiting = await get_waiting(conn, invoice_id)
        missing = await get_waiting(conn, UNKNOWN_INVOICE_ID)

    assert waiting == ("EXTRACT_FAILED", "BR-E01")
    assert missing is None


async def test_unpark_restores_the_waiting_state_and_rule(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D94: unpark restores a parked invoice's state and rule and clears the waiting columns."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await set_state(conn, invoice_id, "PENDING_USER", rule_id="BR-S08")
        await conn.execute(
            """
            UPDATE invoices
            SET waiting_state = 'CONFIRMING', waiting_rule_id = 'BR-R03'
            WHERE id = %s
            """,
            (invoice_id,),
        )
        unparked = await unpark(conn, invoice_id)
        cur = await conn.execute(
            "SELECT state, rule_id, waiting_state, waiting_rule_id FROM invoices WHERE id = %s",
            (invoice_id,),
        )
        row = await cur.fetchone()

    assert unparked is True
    assert row == ("CONFIRMING", "BR-R03", None, None)


@pytest.mark.parametrize(
    ("state", "rule_id"),
    [("CONFIRMING", "BR-R03"), ("PENDING_USER", "BR-S08")],
)
async def test_unpark_is_false_when_it_cannot_resume_the_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    state: str,
    rule_id: str,
) -> None:
    """D94: unpark leaves an invoice that is not parked, or has no waiting state, as it was."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await set_state(conn, invoice_id, state, rule_id=rule_id)
        unparked = await unpark(conn, invoice_id)
        stored = await get_state(conn, invoice_id)

    assert unparked is False
    assert stored == (state, "yo")


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


async def test_save_reading_restarts_the_park_clock(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S08: save_reading stamps updated_at from the database clock, restarting the park clock."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await conn.execute(
            "UPDATE invoices SET updated_at = now() - interval '25 hours' WHERE id = %s",
            (invoice_id,),
        )
        await save_reading(conn, invoice_id, _reading())
        cur = await conn.execute(
            "SELECT updated_at >= now() - interval '1 minute' FROM invoices WHERE id = %s",
            (invoice_id,),
        )
        row = await cur.fetchone()

    assert row == (True,)


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


async def test_set_sunat_status_stores_the_answer_with_the_database_clock(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.1: set_sunat_status stores the status fields and stamps checked_at from the database clock."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await set_sunat_status(
            conn,
            invoice_id,
            CpStatus(estado_cp=1, estado_ruc="10", cond_domi_ruc="00"),
            1,
        )
        cur = await conn.execute(
            """
            SELECT sunat_status_json->>'estado_cp',
                   sunat_status_json->>'estado_ruc',
                   sunat_status_json->>'cond_domi_ruc',
                   (sunat_status_json->>'checked_at')::timestamptz,
                   (sunat_status_json->>'rechecks')::int,
                   now()
            FROM invoices
            WHERE id = %s
            """,
            (invoice_id,),
        )
        row = await cur.fetchone()

    assert row is not None
    assert row[:3] == ("1", "10", "00")
    assert row[4] == 1
    assert row[3] == row[5]


async def test_sunat_rechecks_reads_zero_before_a_status_and_the_count_after(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.1: sunat_rechecks reads 0 when no status is stored and the stored count after one."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        before = await sunat_rechecks(conn, invoice_id)
        await set_sunat_status(
            conn,
            invoice_id,
            CpStatus(estado_cp=0, estado_ruc=None, cond_domi_ruc=None),
            2,
        )
        after = await sunat_rechecks(conn, invoice_id)

    assert (before, after) == (0, 2)


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


@pytest.mark.parametrize(
    ("timezone", "expected"),
    [("America/Lima", date(2024, 11, 13)), ("UTC", date(2024, 11, 14))],
)
async def test_list_pending_dates_a_prepared_batch_in_the_owner_timezone(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    timezone: str,
    expected: date,
) -> None:
    """S11.4: a prepared batch's date is its updated_at in the caller's timezone."""
    profile_id = f"profile-{uuid4().hex}"

    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, profile_id, CHANNEL)
        await set_flow(conn, invoice_id, "work_microsoft")
        await open_batch(conn, invoice_id, "work_microsoft", "prepared")
        await set_state(conn, invoice_id, BATCHED)
        await conn.execute(
            "UPDATE invoices SET updated_at = TIMESTAMPTZ '2024-11-14 03:30:00+00' WHERE id = %s",
            (invoice_id,),
        )
        rows, total = await list_pending(
            conn,
            profile_id,
            limit=20,
            remind_days=2,
            prepared_batch_state="prepared",
            timezone=timezone,
        )

    assert total == 1
    assert [row.batch_prepared_at for row in rows] == [expected]


async def test_record_upload_rejection_hides_only_its_upload(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D103: a recorded rejection hides its upload from list_unattached_uploads; the others stay."""
    first = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
    second = datetime(2026, 11, 14, 10, 1, tzinfo=UTC)

    async with db_pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await _store_upload(conn, invoice_id, "pdf", "media-pdf", first)
        await _store_upload(conn, invoice_id, "xml", "media-xml", second)
        before = await list_unattached_uploads(conn, invoice_id, ("pdf", "xml"))
        await record_upload_rejection(conn, invoice_id, "media-pdf", "BR-D15")
        after = await list_unattached_uploads(conn, invoice_id, ("pdf", "xml"))

    assert [upload.media_ref for upload in before] == ["media-pdf", "media-xml"]
    assert [upload.media_ref for upload in after] == ["media-xml"]


async def test_record_upload_rejection_twice_keeps_one_row(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D103: recording the same rejection twice leaves one row and says only the first added it."""
    async with db_pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        first = await record_upload_rejection(conn, invoice_id, "media-pdf", "BR-D15")
        second = await record_upload_rejection(conn, invoice_id, "media-pdf", "BR-D15")
        cur = await conn.execute(
            "SELECT rule_id, count(*) FROM upload_rejections WHERE invoice_id = %s GROUP BY rule_id",
            (invoice_id,),
        )
        rows = await cur.fetchall()

    assert (first, second) == (True, False)
    assert rows == [("BR-D15", 1)]


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
