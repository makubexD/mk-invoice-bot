"""Integration tests for the job queue: leases, backoff, crash-loop guard (R-9.4, R-1.11, BR-S03)."""

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.queue.jobs import (
    CRASH_LOOP_ERROR,
    MAX_ATTEMPTS,
    MAX_CRASHES,
    Job,
    backoff_minutes,
    claim,
    complete,
    enqueue,
    fail,
    is_dead,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

LEASE_S = 60


async def _clear_jobs(conn: AsyncConnection[Any]) -> None:
    """Remove every job row so tests cannot depend on each other's leftovers."""
    await conn.execute("DELETE FROM jobs")


async def _insert_invoice(conn: AsyncConnection[Any]) -> int:
    """Insert a fresh invoice (owned by this test) and return its id."""
    cur = await conn.execute(
        """
        INSERT INTO invoices (profile_id, origin_channel, state)
        VALUES (%(profile_id)s, 'whatsapp', 'MANUAL_REQUIRED')
        RETURNING id
        """,
        {"profile_id": f"profile-{uuid4().hex}"},
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _enqueue(conn: AsyncConnection[Any], invoice_id: int, step: str = "fetch") -> int:
    """Enqueue one job for ``invoice_id`` in the caller's transaction."""
    return await enqueue(conn, invoice_id, step)


async def _claim(
    pool: AsyncConnectionPool[AsyncConnection[Any]], worker_id: str, lease_s: int = LEASE_S
) -> Job | None:
    """Claim one job on a fresh connection from the pool."""
    async with pool.connection() as conn:
        return await claim(conn, worker_id, lease_s)


async def test_two_concurrent_claimers_never_get_the_same_job(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.4: FOR UPDATE SKIP LOCKED keeps one job to at most one worker, across many rounds."""
    rounds = 10
    claimed: list[int] = []
    async with db_pool.connection() as conn:
        await _clear_jobs(conn)
    for _ in range(rounds):
        async with db_pool.connection() as conn:
            invoice_id = await _insert_invoice(conn)
            await _enqueue(conn, invoice_id)
        results = await asyncio.gather(
            _claim(db_pool, "worker-a"),
            _claim(db_pool, "worker-b"),
        )
        got = [job.id for job in results if job is not None]
        assert len(got) == 1
        claimed.extend(got)
    assert len(set(claimed)) == len(claimed) == rounds


async def test_n_jobs_are_each_claimed_exactly_once(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.4: with N jobs and N claimers, every job is claimed exactly once."""
    n = 4
    async with db_pool.connection() as conn:
        await _clear_jobs(conn)
        invoice_id = await _insert_invoice(conn)
        job_ids = [await _enqueue(conn, invoice_id, f"step-{i}") for i in range(n)]
    results = await asyncio.gather(*(_claim(db_pool, f"worker-{i}") for i in range(n)))
    claimed_ids = [job.id for job in results if job is not None]
    assert sorted(claimed_ids) == sorted(job_ids)
    assert len(set(claimed_ids)) == n


async def test_expired_lease_is_reclaimed_with_crashes_incremented(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.11: an expired lease is re-leased and its crash counter becomes 1."""
    async with db_pool.connection() as conn:
        await _clear_jobs(conn)
        invoice_id = await _insert_invoice(conn)
        job_id = await _enqueue(conn, invoice_id)

    first = await _claim(db_pool, "worker-a")
    assert first is not None
    assert first.id == job_id
    assert first.crashes == 0

    async with db_pool.connection() as conn:
        await conn.execute(
            "UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = %s",
            (job_id,),
        )

    second = await _claim(db_pool, "worker-b")
    assert second is not None
    assert second.id == job_id
    assert second.crashes == 1
    assert second.lease_until is not None


async def test_live_lease_is_not_claimed(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.4: a job whose lease is still current is not claimable."""
    async with db_pool.connection() as conn:
        await _clear_jobs(conn)
        invoice_id = await _insert_invoice(conn)
        await _enqueue(conn, invoice_id)

    first = await _claim(db_pool, "worker-a")
    assert first is not None

    assert await _claim(db_pool, "worker-b") is None


async def test_backoff_schedule_and_cap() -> None:
    """R-9.4: backoff doubles 2, 4, 8, 16 minutes and is capped at 60."""
    assert [backoff_minutes(attempts) for attempts in (1, 2, 3, 4)] == [2, 4, 8, 16]
    assert backoff_minutes(5) == 32
    assert backoff_minutes(6) == 60
    assert backoff_minutes(60) == 60


async def test_fail_backs_off_and_clears_the_lease(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.4: the first failure reschedules ~2 minutes ahead and clears the lease."""
    async with db_pool.connection() as conn:
        await _clear_jobs(conn)
        invoice_id = await _insert_invoice(conn)
        job_id = await _enqueue(conn, invoice_id)

    job = await _claim(db_pool, "worker-a")
    assert job is not None

    async with db_pool.connection() as conn:
        assert await fail(conn, job, "boom") is True

    async with db_pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT attempts, lease_until, last_error,
                   run_after > now(),
                   run_after >= now() + interval '1 minute',
                   run_after <= now() + interval '3 minutes'
            FROM jobs
            WHERE id = %s
            """,
            (job_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    attempts, lease_until, last_error, after_now, after_a_minute, within_three_minutes = row
    assert attempts == 1
    assert lease_until is None
    assert last_error == "boom"
    assert after_now
    assert after_a_minute
    assert within_three_minutes


async def test_fifth_failure_parks_job_and_fails_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S03: five failures park the job and move the invoice to FAILED."""
    async with db_pool.connection() as conn:
        await _clear_jobs(conn)
        invoice_id = await _insert_invoice(conn)
        job_id = await _enqueue(conn, invoice_id)

    for attempt in range(MAX_ATTEMPTS):
        job = await _claim(db_pool, f"worker-{attempt}")
        assert job is not None
        assert job.id == job_id
        async with db_pool.connection() as conn:
            assert await fail(conn, job, f"error-{attempt}") is True
            if attempt < MAX_ATTEMPTS - 1:
                # Skip the backoff delay so the job is due again immediately.
                await conn.execute("UPDATE jobs SET run_after = now() WHERE id = %s", (job_id,))

    assert await _claim(db_pool, "worker-final") is None

    async with db_pool.connection() as conn:
        assert await is_dead(conn, job_id) is True
        cur = await conn.execute(
            """
            SELECT attempts, run_after = 'infinity'::timestamptz, lease_until
            FROM jobs WHERE id = %s
            """,
            (job_id,),
        )
        row = await cur.fetchone()
        cur = await conn.execute(
            "SELECT state, rule_id, error FROM invoices WHERE id = %s",
            (invoice_id,),
        )
        invoice = await cur.fetchone()
    assert row is not None
    assert row[0] == MAX_ATTEMPTS
    assert row[1] is True
    assert row[2] is None
    assert invoice == ("FAILED", "BR-S03", "error-4")


async def test_two_expired_leases_park_job_and_fail_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.11 / BR-S03: two worker deaths on the same job park it and fail the invoice."""
    async with db_pool.connection() as conn:
        await _clear_jobs(conn)
        invoice_id = await _insert_invoice(conn)
        job_id = await _enqueue(conn, invoice_id)

    first = await _claim(db_pool, "worker-a")
    assert first is not None

    async with db_pool.connection() as conn:
        await conn.execute(
            "UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = %s",
            (job_id,),
        )
    second = await _claim(db_pool, "worker-b")
    assert second is not None
    assert second.crashes == 1

    async with db_pool.connection() as conn:
        await conn.execute(
            "UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = %s",
            (job_id,),
        )
    assert await _claim(db_pool, "worker-c") is None

    async with db_pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT crashes, run_after = 'infinity'::timestamptz, lease_until, last_error
            FROM jobs WHERE id = %s
            """,
            (job_id,),
        )
        row = await cur.fetchone()
        cur = await conn.execute(
            "SELECT state, rule_id FROM invoices WHERE id = %s",
            (invoice_id,),
        )
        invoice = await cur.fetchone()
    assert row is not None
    assert row[0] == MAX_CRASHES
    assert row[1] is True
    assert row[2] is None
    assert row[3] == CRASH_LOOP_ERROR
    assert invoice == ("FAILED", "BR-S03")


async def test_complete_with_lost_lease_returns_false_and_leaves_the_row(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.4: complete is a no-op once another worker has taken the lease."""
    async with db_pool.connection() as conn:
        await _clear_jobs(conn)
        invoice_id = await _insert_invoice(conn)
        job_id = await _enqueue(conn, invoice_id)

    job = await _claim(db_pool, "worker-a")
    assert job is not None

    # Let worker-a's lease expire and worker-b take the job over.
    async with db_pool.connection() as conn:
        await conn.execute(
            "UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = %s",
            (job_id,),
        )
    taken = await _claim(db_pool, "worker-b")
    assert taken is not None
    assert taken.id == job_id

    async with db_pool.connection() as conn:
        assert await complete(conn, job) is False

    async with db_pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM jobs WHERE id = %s", (job_id,))
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 1


async def test_enqueue_notifies_a_listener_after_commit(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.4: the NOTIFY jobs issued by enqueue is delivered once the caller commits."""
    async with db_pool.connection() as conn:
        await _clear_jobs(conn)

    listener = await AsyncConnection.connect(db_pool.conninfo, autocommit=True)
    try:
        await listener.execute("LISTEN jobs")
        async with db_pool.connection() as conn:
            invoice_id = await _insert_invoice(conn)
            await _enqueue(conn, invoice_id)

        received = None
        async for notification in listener.notifies(timeout=2):
            received = notification
            break
        assert received is not None
        assert received.channel == "jobs"
    finally:
        await listener.close()
