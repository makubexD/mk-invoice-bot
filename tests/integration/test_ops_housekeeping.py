"""Integration tests for the daily housekeeping task (R-9.7, S12.4)."""

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.ops.housekeeping import housekeep
from invoicebot.states import CANCELLED, DONE, FAILED, FETCHING_DOCS

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: An invoice id no identity sequence has reached in the test database.
UNKNOWN_ID = 1_999_999_999
#: A lease that ended an hour ago, and one that ends in an hour.
EXPIRED_LEASE_S = -3600
LIVE_LEASE_S = 3600


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
                    await housekeep(db_pool, tmp_path)
            assert folder.is_dir()
    finally:
        await blocker.rollback()
        await db_pool.putconn(blocker)
    try:
        await housekeep(db_pool, tmp_path)

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
        await housekeep(db_pool, tmp_path)

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
                await housekeep(db_pool, tmp_path)

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

    await housekeep(db_pool, tmp_path)

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
        await housekeep(db_pool, tmp_path)

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

    await housekeep(db_pool, tmp_path)

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
        await housekeep(db_pool, tmp_path)

        async with db_pool.connection() as conn:
            assert not await _job_exists(conn, expired_job)
            assert await _job_exists(conn, live_job)
            assert await _job_exists(conn, active_job)
    finally:
        await _delete_invoices(db_pool, [cancelled_id, active_id])
