"""Integration tests for forgetting an invoice in the database (D196)."""

from typing import Any

import pytest
from psycopg import AsyncConnection, sql
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from invoicebot.store.forget import (
    NULLED_TABLES,
    PURGED_TABLES,
    cancel_held,
    lock_target,
    purge,
    release_key,
)
from invoicebot.store.invoices import claim_natural_key

pytestmark = pytest.mark.asyncio(loop_scope="session")

KEY = "20000000001-F001-1"


async def _insert_invoice(
    conn: AsyncConnection[Any],
    *,
    batch_id: int | None = None,
    natural_key: str | None = None,
    state: str = "DONE",
) -> int:
    """Insert a synthetic invoice (DONE unless ``state`` says otherwise) and return its id."""
    cur = await conn.execute(
        """
        INSERT INTO invoices (profile_id, origin_channel, state, flow_id, serie, numero, natural_key,
                              batch_id, flow_data_json)
        VALUES ('profile-forget', 'whatsapp', %s, 'flow-x', 'F001', '1', %s, %s, %s)
        RETURNING id
        """,
        (state, natural_key, batch_id, Jsonb({"folder_path": "Facturas/2026-11/F001-1"})),
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _insert_batch(conn: AsyncConnection[Any], state: str = "open") -> int:
    """Insert a batch (``open`` unless ``state`` says otherwise) and return its id."""
    cur = await conn.execute(
        "INSERT INTO batches (flow_id, state) VALUES ('flow-x', %s) RETURNING id", (state,)
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _insert_children(conn: AsyncConnection[Any], invoice_id: int) -> None:
    """Insert one row for the invoice in every referencing table."""
    await conn.execute(
        "INSERT INTO llm_usage (invoice_id, provider, model, tokens_in, tokens_out, cost_usd) "
        "VALUES (%s, 'p', 'm', 10, 5, 0.25)",
        (invoice_id,),
    )
    await conn.execute(
        "INSERT INTO files (invoice_id, kind, local_path, sha256, size) "
        "VALUES (%s, 'photo', 'tmp/a.jpg', 'ab', 1)",
        (invoice_id,),
    )
    await conn.execute("INSERT INTO jobs (invoice_id, step) VALUES (%s, 'extract')", (invoice_id,))
    await conn.execute(
        "INSERT INTO fetch_attempts (invoice_id, fetcher, ok, ms) VALUES (%s, 'f', true, 1)", (invoice_id,)
    )
    await conn.execute(
        "INSERT INTO gaps (fingerprint, kind, subject, state) VALUES ('g-forget', 'k', 's', 'open') "
        "ON CONFLICT DO NOTHING"
    )
    await conn.execute(
        "INSERT INTO invoice_gaps (invoice_id, fingerprint) VALUES (%s, 'g-forget')", (invoice_id,)
    )
    await conn.execute(
        "INSERT INTO upload_rejections (invoice_id, media_ref, rule_id) VALUES (%s, 'm', 'BR-S01')",
        (invoice_id,),
    )
    await conn.execute(
        "INSERT INTO pending_actions (kind, invoice_id, requested_by, state) "
        "VALUES ('cancel', %s, 'u', 'pending')",
        (invoice_id,),
    )
    await conn.execute(
        "INSERT INTO events (type, version, invoice_id, payload_json) VALUES ('invoice.created', 1, %s, %s)",
        (invoice_id, Jsonb({})),
    )
    await conn.execute(
        "INSERT INTO messages (channel, external_id, profile_id, kind, invoice_id) "
        "VALUES ('whatsapp', 'ext-forget-1', 'profile-forget', 'text', %s)",
        (invoice_id,),
    )
    await conn.execute(
        'INSERT INTO outbox (channel, "to", payload_json, state, invoice_id) '
        "VALUES ('whatsapp', '51900000000', %s, 'pending', %s)",
        (Jsonb({}), invoice_id),
    )
    await conn.execute(
        "INSERT INTO mail_jobs (job_id, invoice_id) VALUES ('job-forget-1', %s)", (invoice_id,)
    )
    await conn.execute(
        "INSERT INTO portal_candidates (invoice_id, url) VALUES (%s, 'https://example.invalid/x')",
        (invoice_id,),
    )


async def _count(conn: AsyncConnection[Any], query: str | sql.Composable, params: tuple[Any, ...]) -> int:
    """Run a ``SELECT count(*)`` query and return the number."""
    cur = await conn.execute(query, params)
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def test_lock_target_missing_invoice_is_none(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D196: a missing invoice id has no target."""
    async with db_pool.connection() as conn:
        try:
            assert await lock_target(conn, 2_000_000_000) is None
        finally:
            await conn.rollback()


async def test_lock_target_returns_invoice_facts(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """D196: the target carries state, flow, serie, numero, folder and the files' local paths."""
    async with db_pool.connection() as conn:
        try:
            invoice_id = await _insert_invoice(conn)
            await conn.execute(
                "INSERT INTO files (invoice_id, kind, local_path, sha256, size) VALUES "
                "(%s, 'photo', 'tmp/a.jpg', 'a1', 1), (%s, 'xml', NULL, 'a2', 1)",
                (invoice_id, invoice_id),
            )
            target = await lock_target(conn, invoice_id)
            assert target is not None
            assert target.invoice_id == invoice_id
            assert target.state == "DONE"
            assert target.flow_id == "flow-x"
            assert (target.serie, target.numero) == ("F001", "1")
            assert target.folder_path == "Facturas/2026-11/F001-1"
            assert target.paid is False
            assert target.held is False
            assert target.local_paths == ("tmp/a.jpg",)
        finally:
            await conn.rollback()


async def test_release_key_clears_key_and_allows_claim(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D196: releasing the key keeps the row and lets a new invoice claim the same key."""
    async with db_pool.connection() as conn:
        try:
            await conn.execute("DELETE FROM invoices WHERE natural_key = %s", (KEY,))
            first = await _insert_invoice(conn, natural_key=KEY)
            await release_key(conn, first)
            cur = await conn.execute("SELECT natural_key FROM invoices WHERE id = %s", (first,))
            row = await cur.fetchone()
            assert row is not None
            assert row[0] is None
            second = await _insert_invoice(conn)
            assert await claim_natural_key(conn, second, KEY) is None
        finally:
            await conn.rollback()


async def test_purge_deletes_every_referencing_row(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D196: purge removes the invoice and one row per referencing table, with matching counts."""
    async with db_pool.connection() as conn:
        try:
            invoice_id = await _insert_invoice(conn)
            await _insert_children(conn, invoice_id)
            counts = await purge(conn, invoice_id)
            assert counts == {**dict.fromkeys(PURGED_TABLES, 1), "invoices": 1, "batches": 0}
            for table in PURGED_TABLES:
                remaining = await _count(
                    conn,
                    sql.SQL("SELECT count(*) FROM {} WHERE invoice_id = %s").format(sql.Identifier(table)),
                    (invoice_id,),
                )
                assert remaining == 0
            assert await _count(conn, "SELECT count(*) FROM invoices WHERE id = %s", (invoice_id,)) == 0
        finally:
            await conn.rollback()


async def test_purge_keeps_probe_with_null_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D196: purge deletes llm_usage rows; a portal_probes row stays with invoice_id null."""
    async with db_pool.connection() as conn:
        try:
            invoice_id = await _insert_invoice(conn)
            await conn.execute(
                "INSERT INTO llm_usage (invoice_id, provider, model, tokens_in, tokens_out, cost_usd) "
                "VALUES (%s, 'p', 'm', 10, 5, 0.25)",
                (invoice_id,),
            )
            await conn.execute(
                "INSERT INTO portal_probes (ruc_emisor, link_version, invoice_id, state) "
                "VALUES (20000000002, 1, %s, 'done')",
                (invoice_id,),
            )
            await purge(conn, invoice_id)
            assert (
                await _count(conn, "SELECT count(*) FROM llm_usage WHERE invoice_id = %s", (invoice_id,)) == 0
            )
            cur = await conn.execute("SELECT invoice_id FROM portal_probes WHERE ruc_emisor = 20000000002")
            row = await cur.fetchone()
            assert row is not None
            assert row[0] is None
        finally:
            await conn.rollback()


async def test_purge_deletes_unshared_batch_only(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """D196: the batch goes with its only invoice and stays while another invoice uses it."""
    async with db_pool.connection() as conn:
        try:
            alone = await _insert_batch(conn)
            only = await _insert_invoice(conn, batch_id=alone)
            assert (await purge(conn, only))["batches"] == 1
            assert await _count(conn, "SELECT count(*) FROM batches WHERE id = %s", (alone,)) == 0

            shared = await _insert_batch(conn)
            first = await _insert_invoice(conn, batch_id=shared)
            await _insert_invoice(conn, batch_id=shared)
            assert (await purge(conn, first))["batches"] == 0
            assert await _count(conn, "SELECT count(*) FROM batches WHERE id = %s", (shared,)) == 1
        finally:
            await conn.rollback()


async def test_every_foreign_key_to_invoices_is_covered(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D196: each table referencing invoices(id) is purged or nulls itself; a new one fails here."""
    async with db_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT DISTINCT conrelid::regclass::text FROM pg_constraint "
            "WHERE contype = 'f' AND confrelid = 'invoices'::regclass"
        )
        referencing = {row[0] for row in await cur.fetchall()}
    assert referencing == set(PURGED_TABLES) | set(NULLED_TABLES)


@pytest.mark.parametrize(
    ("state", "batch_state", "held"),
    [
        ("BATCHED", "review", True),
        ("BATCHED", "prepared", False),
        ("BATCHED", "sending", False),
        ("BATCHED", None, False),
        ("DONE", "review", False),
        ("DONE", None, False),
    ],
)
async def test_lock_target_held_only_for_batched_on_review_batch(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], state: str, batch_state: str | None, held: bool
) -> None:
    """D204: an invoice is held only when it is BATCHED and its batch is in state review."""
    async with db_pool.connection() as conn:
        try:
            batch_id = await _insert_batch(conn, batch_state) if batch_state else None
            invoice_id = await _insert_invoice(conn, batch_id=batch_id, state=state)
            target = await lock_target(conn, invoice_id)
            assert target is not None
            assert target.held is held
        finally:
            await conn.rollback()


async def test_cancel_held_leaves_invoice_cancelled(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D204: cancel_held sets a BATCHED invoice CANCELLED with rule BR-D23."""
    async with db_pool.connection() as conn:
        try:
            batch_id = await _insert_batch(conn, "review")
            invoice_id = await _insert_invoice(conn, batch_id=batch_id, state="BATCHED")
            await cancel_held(conn, invoice_id)
            cur = await conn.execute("SELECT state, rule_id FROM invoices WHERE id = %s", (invoice_id,))
            assert await cur.fetchone() == ("CANCELLED", "BR-D23")
        finally:
            await conn.rollback()
