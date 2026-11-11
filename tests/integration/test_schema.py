"""Integration tests for the initial schema migration (specs/03-design.md section 3)."""

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
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
    "messages",
    "outbox",
    "padron",
    "pending_actions",
    "taught_links",
}

INSERT_MESSAGE = """
    INSERT INTO messages (channel, external_id, profile_id, kind, received_at, payload_json)
    VALUES (%s, %s, %s, %s, %s, %s)
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
