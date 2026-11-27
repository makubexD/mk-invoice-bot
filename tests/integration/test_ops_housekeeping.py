"""Integration tests for the daily housekeeping task (R-9.7, S12.4)."""

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.ops.housekeeping import housekeep
from invoicebot.pipeline.review import RULE_REVIEW
from invoicebot.states import BATCHED, CANCELLED, DONE, FAILED, FETCHING_DOCS

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: An invoice id no identity sequence has reached in the test database.
UNKNOWN_ID = 1_999_999_999
#: A lease that ended an hour ago, and one that ends in an hour.
EXPIRED_LEASE_S = -3600
LIVE_LEASE_S = 3600
#: The chat-documents retention the tests pass in (D206).
RETENTION_DAYS = 30


async def _insert_invoice(conn: AsyncConnection[Any], state: str) -> int:
    """Insert an invoice in ``state`` and return its id."""
    cur = await conn.execute(
        "INSERT INTO invoices (profile_id, origin_channel, state) VALUES (%s, 'whatsapp', %s) RETURNING id",
        (f"profile-{uuid4().hex}", state),
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _insert_job(conn: AsyncConnection[Any], invoice_id: int, lease_s: int) -> int:
    """Insert a job for ``invoice_id`` whose lease ends ``lease_s`` from now, and return its id."""
    cur = await conn.execute(
        """
        INSERT INTO jobs (invoice_id, step, lease_until)
        VALUES (%s, 'fetch', now() + make_interval(secs => %s))
        RETURNING id
        """,
        (invoice_id, lease_s),
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _job_exists(conn: AsyncConnection[Any], job_id: int) -> bool:
    """Report whether the jobs table still has ``job_id``."""
    cur = await conn.execute("SELECT 1 FROM jobs WHERE id = %s", (job_id,))
    return await cur.fetchone() is not None


async def _delete_invoices(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_ids: list[int]) -> None:
    """Delete the jobs and invoices this file created."""
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("DELETE FROM jobs WHERE invoice_id = ANY(%s)", (invoice_ids,))
        await conn.execute("DELETE FROM invoices WHERE id = ANY(%s)", (invoice_ids,))


async def test_housekeep_holds_an_invoice_that_leaves_failed(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-9.7 housekeeping waits for an uncommitted state change, then removes the folder.

    A FAILED invoice retried into FETCHING keeps its tmp folder while that change
    is uncommitted; once the change rolls back, the folder goes.
    """
    async with db_pool.connection() as conn, conn.transaction():
        invoice_id = await _insert_invoice(conn, FAILED)
    tmp = tmp_path / "tmp"
    folder = tmp / str(invoice_id)
    folder.mkdir(parents=True)
    blocker = await db_pool.getconn()
    try:
        async with blocker.transaction(force_rollback=True):
            await blocker.execute("UPDATE invoices SET state = %s WHERE id = %s", (FETCHING_DOCS, invoice_id))
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(1):
                    await housekeep(db_pool, tmp_path, RETENTION_DAYS)
            assert folder.is_dir()
    finally:
        await blocker.rollback()
        await db_pool.putconn(blocker)
    try:
        await housekeep(db_pool, tmp_path, RETENTION_DAYS)

        assert not folder.exists()
    finally:
        await _delete_invoices(db_pool, [invoice_id])


async def test_housekeep_removes_tmp_folders_of_terminal_and_unknown_invoices(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-9.7 a DONE invoice's tmp folder goes, an active one stays.

    An unknown invoice id's folder goes too, and a non-digit name stays.
    """
    async with db_pool.connection() as conn, conn.transaction():
        done_id = await _insert_invoice(conn, DONE)
        active_id = await _insert_invoice(conn, FETCHING_DOCS)
    tmp = tmp_path / "tmp"
    done_dir = tmp / str(done_id)
    active_dir = tmp / str(active_id)
    unknown_dir = tmp / str(UNKNOWN_ID)
    other_dir = tmp / "not-an-invoice"
    for folder in (done_dir, active_dir, unknown_dir, other_dir):
        folder.mkdir(parents=True)
    try:
        await housekeep(db_pool, tmp_path, RETENTION_DAYS)

        assert not done_dir.exists()
        assert active_dir.is_dir()
        assert not unknown_dir.exists()
        assert other_dir.is_dir()
    finally:
        await _delete_invoices(db_pool, [done_id, active_id])


async def test_housekeep_does_not_wait_for_a_running_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-9.7: a running invoice's locked row does not hold housekeeping; a DONE folder still goes."""
    async with db_pool.connection() as conn, conn.transaction():
        done_id = await _insert_invoice(conn, DONE)
        active_id = await _insert_invoice(conn, FETCHING_DOCS)
    tmp = tmp_path / "tmp"
    done_dir = tmp / str(done_id)
    active_dir = tmp / str(active_id)
    for folder in (done_dir, active_dir):
        folder.mkdir(parents=True)
    blocker = await db_pool.getconn()
    try:
        async with blocker.transaction(force_rollback=True):
            await blocker.execute("SELECT 1 FROM invoices WHERE id = %s FOR UPDATE", (active_id,))
            async with asyncio.timeout(5):
                await housekeep(db_pool, tmp_path, RETENTION_DAYS)

            assert not done_dir.exists()
            assert active_dir.is_dir()
    finally:
        await blocker.rollback()
        await db_pool.putconn(blocker)
        await _delete_invoices(db_pool, [done_id, active_id])


@pytest.mark.parametrize("name", ["\u00b2", "9" * 20])
async def test_housekeep_skips_a_name_that_is_not_an_invoice_id(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path, name: str
) -> None:
    """R-9.7: a non-ASCII digit or an id past bigint is left alone and does not stop housekeeping."""
    odd = tmp_path / "tmp" / name
    unknown_dir = tmp_path / "tmp" / str(UNKNOWN_ID)
    for folder in (odd, unknown_dir):
        folder.mkdir(parents=True)

    await housekeep(db_pool, tmp_path, RETENTION_DAYS)

    assert odd.is_dir()
    assert not unknown_dir.exists()


async def test_housekeep_skips_a_folder_it_cannot_remove(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-9.7: a folder whose removal fails is kept, and the next stale folder still goes."""
    tmp = tmp_path / "tmp"
    stuck = tmp / str(UNKNOWN_ID)
    locked = stuck / "locked"
    locked.mkdir(parents=True)
    (locked / "file").write_text("x", encoding="utf-8")
    locked.chmod(0o500)
    other = tmp / str(UNKNOWN_ID + 1)
    other.mkdir()
    try:
        await housekeep(db_pool, tmp_path, RETENTION_DAYS)

        assert stuck.is_dir()
        assert not other.exists()
    finally:
        locked.chmod(0o700)


async def test_housekeep_skips_a_symlink_named_like_an_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-9.7: a digit-named symlink in tmp is left alone and its target is untouched."""
    target = tmp_path / "outside"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    link = tmp / str(UNKNOWN_ID)
    link.symlink_to(target, target_is_directory=True)

    await housekeep(db_pool, tmp_path, RETENTION_DAYS)

    assert link.is_symlink()
    assert (target / "keep.txt").is_file()


async def test_housekeep_deletes_expired_jobs_of_terminal_invoices_only(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """S12.4 a CANCELLED invoice's expired-lease job goes; a live lease and an active invoice stay."""
    async with db_pool.connection() as conn, conn.transaction():
        cancelled_id = await _insert_invoice(conn, CANCELLED)
        active_id = await _insert_invoice(conn, FETCHING_DOCS)
        expired_job = await _insert_job(conn, cancelled_id, EXPIRED_LEASE_S)
        live_job = await _insert_job(conn, cancelled_id, LIVE_LEASE_S)
        active_job = await _insert_job(conn, active_id, EXPIRED_LEASE_S)
    try:
        await housekeep(db_pool, tmp_path, RETENTION_DAYS)

        async with db_pool.connection() as conn:
            assert not await _job_exists(conn, expired_job)
            assert await _job_exists(conn, live_job)
            assert await _job_exists(conn, active_job)
    finally:
        await _delete_invoices(db_pool, [cancelled_id, active_id])


async def _mail_case(
    pool: AsyncConnectionPool[AsyncConnection[Any]], state: str, *, pending_review: bool = False
) -> int:
    """Insert an invoice in ``state`` (with a pending review document row on request) and return its id."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await _insert_invoice(conn, state)
        if pending_review:
            await conn.execute(
                """
                INSERT INTO outbox (channel, "to", invoice_id, payload_json, state)
                VALUES ('console', 'x', %s, %s::jsonb, 'pending')
                """,
                (invoice_id, json.dumps({"document": "a.pdf", "rule_id": RULE_REVIEW})),
            )
    return invoice_id


async def _cleanup_mail(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int) -> None:
    """Delete the outbox rows and the invoice a mail test created."""
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("DELETE FROM outbox WHERE invoice_id = %s", (invoice_id,))
    await _delete_invoices(pool, [invoice_id])


@pytest.mark.parametrize(
    ("state", "pending_review", "kept"),
    [(DONE, False, False), (BATCHED, False, True), (DONE, True, True)],
)
async def test_housekeep_mail_dir_follows_state_and_pending_review(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    state: str,
    pending_review: bool,
    kept: bool,
) -> None:
    """R-9.7, D195: a mail dir goes when terminal, stays when held or a review document waits."""
    invoice_id = await _mail_case(db_pool, state, pending_review=pending_review)
    folder = tmp_path / "mail" / str(invoice_id)
    folder.mkdir(parents=True)
    try:
        await housekeep(db_pool, tmp_path, RETENTION_DAYS)

        assert folder.exists() is kept
    finally:
        await _cleanup_mail(db_pool, invoice_id)


async def test_housekeep_leaves_a_mail_eml_draft_file(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-9.7, D195: the dry-run ``mail/<id>.eml`` of a DONE invoice is a file and stays."""
    invoice_id = await _mail_case(db_pool, DONE)
    draft = tmp_path / "mail" / f"{invoice_id}.eml"
    draft.parent.mkdir(parents=True)
    draft.write_text("draft")
    try:
        await housekeep(db_pool, tmp_path, RETENTION_DAYS)

        assert draft.is_file()
    finally:
        await _cleanup_mail(db_pool, invoice_id)


async def test_housekeep_removes_the_mail_dir_of_an_unknown_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-9.7, D195: a mail dir named for an invoice no row has is removed."""
    folder = tmp_path / "mail" / str(UNKNOWN_ID)
    folder.mkdir(parents=True)

    await housekeep(db_pool, tmp_path, RETENTION_DAYS)

    assert not folder.exists()


async def _docs_folder_of(
    pool: AsyncConnectionPool[AsyncConnection[Any]], root: Path, state: str, age_days: int
) -> tuple[int, Path]:
    """Insert an invoice in ``state`` last changed ``age_days`` ago and make its docs folder."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await _insert_invoice(conn, state)
        await conn.execute(
            "UPDATE invoices SET updated_at = now() - make_interval(days => %s) WHERE id = %s",
            (age_days, invoice_id),
        )
    folder = root / "docs" / str(invoice_id)
    folder.mkdir(parents=True)
    (folder / "factura.pdf").write_bytes(b"%PDF")
    return invoice_id, folder


@pytest.mark.parametrize(
    ("state", "age_days", "removed"),
    [(DONE, 31, True), (DONE, 1, False), (FETCHING_DOCS, 31, False)],
    ids=["old-terminal", "recent-terminal", "non-terminal"],
)
async def test_housekeep_docs_folder_follows_the_retention(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    state: str,
    age_days: int,
    removed: bool,
) -> None:
    """D206: a terminal invoice's docs folder goes once past retention; others stay."""
    invoice_id, folder = await _docs_folder_of(db_pool, tmp_path, state, age_days)
    try:
        await housekeep(db_pool, tmp_path, RETENTION_DAYS)

        assert folder.exists() is not removed
    finally:
        await _delete_invoices(db_pool, [invoice_id])


async def test_housekeep_removes_a_docs_folder_with_no_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D206: a docs folder whose invoice no longer exists is removed whatever its age."""
    folder = tmp_path / "docs" / str(UNKNOWN_ID)
    folder.mkdir(parents=True)

    await housekeep(db_pool, tmp_path, RETENTION_DAYS)

    assert not folder.exists()
