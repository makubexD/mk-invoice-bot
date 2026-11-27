"""Integration tests for the schema migrations (specs/03-design.md section 3)."""

import asyncio
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from psycopg import AsyncConnection
from psycopg.errors import CheckViolation, UniqueViolation
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

pytestmark = pytest.mark.asyncio(loop_scope="session")

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_TABLES = {
    "api_tokens",
    "batches",
    "channel_windows",
    "event_subscribers",
    "events",
    "fetch_attempts",
    "files",
    "gaps",
    "invoice_gaps",
    "invoices",
    "jobs",
    "kv",
    "llm_usage",
    "mail_jobs",
    "messages",
    "ops_state",
    "outbox",
    "payment_proposals",
    "payments",
    "padron",
    "pending_actions",
    "portal_candidates",
    "portal_probes",
    "taught_links",
    "upload_rejections",
}

INSERT_MESSAGE = """
    INSERT INTO messages (channel, external_id, profile_id, kind, received_at, payload_json)
    VALUES (%s, %s, %s, %s, %s, %s)
"""

INSERT_INVOICE = """
    INSERT INTO invoices (profile_id, origin_channel, state)
    VALUES (%s, %s, %s)
    RETURNING id, flow_data_json
"""

INSERT_BATCH = "INSERT INTO batches (flow_id, state) VALUES (%s, %s)"

INSERT_OUTBOX = """
    INSERT INTO outbox (channel, "to", payload_json, state)
    VALUES (%s, %s, %s, %s)
    RETURNING id
"""


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


async def _tables(pool: AsyncConnectionPool[AsyncConnection[Any]]) -> set[str]:
    """Return the table names in the public schema, excluding alembic_version."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
        )
        rows = await cur.fetchall()
    return {row[0] for row in rows}


async def _version(pool: AsyncConnectionPool[AsyncConnection[Any]]) -> str | None:
    """Return the current Alembic revision, or None when the database is at base."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT version_num FROM alembic_version")
        row = await cur.fetchone()
    return None if row is None else row[0]


async def _columns(pool: AsyncConnectionPool[AsyncConnection[Any]], table: str) -> set[str]:
    """Return the column names of a public table."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s",
            (table,),
        )
        rows = await cur.fetchall()
    return {row[0] for row in rows}


async def test_schema_has_every_section_3_table(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.4: every table of specs/03-design.md section 3 exists after upgrade head."""
    assert await _tables(db_pool) == EXPECTED_TABLES


async def test_second_upgrade_head_is_a_noop(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.4: a second upgrade head leaves the schema and revision unchanged."""
    tables_before = await _tables(db_pool)
    version_before = await _version(db_pool)
    await asyncio.to_thread(_run_alembic, db_pool.conninfo, "head")
    assert await _tables(db_pool) == tables_before
    assert await _version(db_pool) == version_before


async def test_downgrade_then_upgrade_restores_schema(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.4: downgrading to base drops the schema and upgrade head recreates the same tables."""
    before = await _tables(db_pool)
    try:
        await asyncio.to_thread(_run_alembic, db_pool.conninfo, "base", downgrade=True)
        downgraded = await _tables(db_pool)
    finally:
        # Leave the database at head even if an assertion below fails.
        await asyncio.to_thread(_run_alembic, db_pool.conninfo, "head")
    assert downgraded == set()
    assert await _tables(db_pool) == before == EXPECTED_TABLES


async def test_messages_pk_is_unique_per_channel(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.8: the same (channel, external_id) is rejected, the same id on two channels is not."""
    received_at = datetime.now(UTC)
    whatsapp = ("whatsapp", "external-1", "owner", "text", received_at, None)
    telegram = ("telegram", "external-1", "owner", "text", received_at, None)

    with pytest.raises(UniqueViolation):
        async with db_pool.connection() as conn:
            await conn.execute(INSERT_MESSAGE, whatsapp)
            await conn.execute(INSERT_MESSAGE, whatsapp)

    async with db_pool.connection() as conn:
        await conn.execute(INSERT_MESSAGE, whatsapp)
        await conn.execute(INSERT_MESSAGE, telegram)
        cur = await conn.execute("SELECT count(*) FROM messages WHERE external_id = %s", ("external-1",))
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 2


async def test_invoice_flow_data_json_defaults_to_empty_object(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.2: an invoice inserted without flow_data_json gets an empty JSON object."""
    async with db_pool.connection() as conn:
        cur = await conn.execute(INSERT_INVOICE, ("owner", "whatsapp", "parsed"))
        row = await cur.fetchone()
    assert row is not None
    assert row[1] == {}


async def test_invoice_accepts_console_origin_channel(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D165: an invoice with origin_channel console inserts."""
    async with db_pool.connection() as conn:
        cur = await conn.execute(INSERT_INVOICE, ("owner", "console", "parsed"))
        row = await cur.fetchone()
        assert row is not None
        # Remove it again so a later downgrade past 0013 can restore the old CHECK.
        await conn.execute("DELETE FROM invoices WHERE id = %s", (row[0],))


async def test_outbox_accepts_console_channel(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D165: an outbox row with channel console inserts."""
    async with db_pool.connection() as conn:
        cur = await conn.execute(INSERT_OUTBOX, ("console", "owner", Jsonb({}), "pending"))
        row = await cur.fetchone()
        assert row is not None
        # Remove it again so a later downgrade past 0013 can restore the old CHECK.
        await conn.execute("DELETE FROM outbox WHERE id = %s", (row[0],))


async def test_invoice_still_refuses_unknown_origin_channel(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D165: an invoice with origin_channel sms is still refused."""
    with pytest.raises(CheckViolation):
        async with db_pool.connection() as conn:
            await conn.execute(INSERT_INVOICE, ("owner", "sms", "parsed"))


async def test_batch_accepts_prepared_and_rejects_unknown_states(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.8: a batch accepts prepared and still rejects a state outside the check."""
    async with db_pool.connection() as conn:
        await conn.execute(INSERT_BATCH, ("file-b", "prepared"))

    with pytest.raises(CheckViolation):
        async with db_pool.connection() as conn:
            await conn.execute(INSERT_BATCH, ("file-b", "bogus"))


async def test_batch_accepts_review_and_rejects_paused(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D194: a batch accepts review and still rejects a state outside the check."""
    async with db_pool.connection() as conn:
        await conn.execute(INSERT_BATCH, ("file-b", "review"))

    with pytest.raises(CheckViolation):
        async with db_pool.connection() as conn:
            await conn.execute(INSERT_BATCH, ("file-b", "paused"))


async def test_batch_accepts_bounced(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D207: a batch accepts bounced."""
    async with db_pool.connection() as conn:
        await conn.execute(INSERT_BATCH, ("file-b", "bounced"))


async def test_file_accepts_eml_and_manifest_and_rejects_unknown_kinds(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.2: a file row accepts eml and manifest and still rejects a kind outside the check."""
    async with db_pool.connection() as conn:
        cur = await conn.execute(INSERT_INVOICE, ("owner", "whatsapp", "parsed"))
        row = await cur.fetchone()
        assert row is not None
        invoice_id = row[0]
        await conn.execute(
            "INSERT INTO files (invoice_id, kind, sha256, size) VALUES (%s, %s, %s, %s)",
            (invoice_id, "eml", "synthetic-sha256", 1),
        )
        await conn.execute(
            "INSERT INTO files (invoice_id, kind, sha256, size) VALUES (%s, %s, %s, %s)",
            (invoice_id, "manifest", "synthetic-sha256", 1),
        )

    with pytest.raises(CheckViolation):
        async with db_pool.connection() as conn:
            await conn.execute(
                "INSERT INTO files (invoice_id, kind, sha256, size) VALUES (%s, %s, %s, %s)",
                (invoice_id, "bogus", "synthetic-sha256", 1),
            )


@pytest.mark.parametrize(
    ("table", "expected_columns"),
    [
        (
            "payments",
            {"id", "flow_id", "profile_id", "paid_on", "amount", "created_at"},
        ),
        (
            "payment_proposals",
            {
                "id",
                "profile_id",
                "flow_id",
                "paid_on",
                "amount",
                "sets_json",
                "state",
                "created_at",
            },
        ),
        (
            "mail_jobs",
            {
                "job_id",
                "invoice_id",
                "created_at",
                "receipt_at",
                "receipt_error",
                "stale_alerted_at",
                "overdue_alerted_at",
            },
        ),
    ],
)
async def test_payment_and_mail_tables_have_their_columns(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    table: str,
    expected_columns: set[str],
) -> None:
    """R-7.12: the payment and mail job tables have their columns after upgrade head."""
    assert await _columns(db_pool, table) == expected_columns


async def test_invoices_gains_payment_id(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.12: invoices has the payment_id column after upgrade head."""
    assert "payment_id" in await _columns(db_pool, "invoices")


async def test_payment_with_zero_amount_is_rejected(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.12: a payment with amount 0 is refused and nothing is stored."""
    with pytest.raises(CheckViolation):
        async with db_pool.connection() as conn:
            await conn.execute(
                "INSERT INTO payments (flow_id, profile_id, paid_on, amount) VALUES (%s, %s, %s, %s)",
                ("file-b", "owner", date(2026, 11, 15), 0),
            )
    async with db_pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM payments")
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 0


async def test_job_without_created_at_gets_now(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.12: a job inserted without created_at gets the database clock's now()."""
    async with db_pool.connection() as conn:
        cur = await conn.execute(INSERT_INVOICE, ("owner", "whatsapp", "RECEIVED"))
        invoice = await cur.fetchone()
        assert invoice is not None
        cur = await conn.execute(
            "INSERT INTO jobs (invoice_id, step) VALUES (%s, %s) RETURNING created_at, now()",
            (invoice[0], "extract"),
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == row[1]


async def test_gaps_gains_raised_at(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.2: gaps has the raised_at column after upgrade head (D115)."""
    assert "raised_at" in await _columns(db_pool, "gaps")


async def test_taught_link_gains_version(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D137: a taught link inserted at revision 0010 has version 1 after upgrading to 0011."""
    ruc_emisor = 20000000911
    try:
        await asyncio.to_thread(_run_alembic, db_pool.conninfo, "0010", downgrade=True)
        async with db_pool.connection() as conn:
            await conn.execute(
                "INSERT INTO taught_links (ruc_emisor, url, profile_id, state) VALUES (%s, %s, %s, %s)",
                (ruc_emisor, "https://example.test/facturas", "owner", "approved"),
            )
        await asyncio.to_thread(_run_alembic, db_pool.conninfo, "0011")
        async with db_pool.connection() as conn:
            cur = await conn.execute("SELECT version FROM taught_links WHERE ruc_emisor = %s", (ruc_emisor,))
            row = await cur.fetchone()
    finally:
        # Leave the database at head even if an assertion below fails.
        await asyncio.to_thread(_run_alembic, db_pool.conninfo, "head")
    assert row is not None
    assert row[0] == 1
