"""Integration tests for the job queue: leases, backoff, crash-loop guard (R-9.4, R-1.11, BR-S03)."""

import asyncio
from dataclasses import dataclass, field
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
    OnDead,
    backoff_minutes,
    claim,
    complete,
    enqueue,
    fail,
    is_dead,
    is_superseded,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

LEASE_S = 60


class DeathRaisedError(RuntimeError):
    """Raised by ``on_dead`` to prove its rollback."""


@dataclass
class _Deaths:
    """An ``on_dead`` that records each ``(invoice_id, step, error)`` call."""

    calls: list[tuple[int, str, str]] = field(default_factory=list)

    async def __call__(self, conn: AsyncConnection[Any], invoice_id: int, step: str, error: str) -> None:
        """Record one death."""
        self.calls.append((invoice_id, step, error))


async def _ignore_dead(conn: AsyncConnection[Any], invoice_id: int, step: str, error: str) -> None:
    """An ``on_dead`` that does nothing, for tests about other behavior."""


async def _raise_dead(conn: AsyncConnection[Any], invoice_id: int, step: str, error: str) -> None:
    """An ``on_dead`` that fails, so its transaction must roll back."""
    raise DeathRaisedError(error)


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
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    worker_id: str,
    lease_s: int = LEASE_S,
    on_dead: OnDead = _ignore_dead,
) -> Job | None:
    """Claim one job on a fresh connection from the pool."""
    async with pool.connection() as conn:
        return await claim(conn, worker_id, lease_s, on_dead=on_dead)


async def _expire_lease(pool: AsyncConnectionPool[AsyncConnection[Any]], job_id: int) -> None:
    """Make ``job_id``'s lease expire, as if its worker died."""
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = %s",
            (job_id,),
        )


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
        assert await fail(conn, job, "boom", on_dead=_ignore_dead) is True

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
            assert await fail(conn, job, f"error-{attempt}", on_dead=_ignore_dead) is True
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


async def test_is_superseded_is_false_for_the_only_job_of_an_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-5.1: the only job of an invoice is current, so it is not superseded."""
    async with db_pool.connection() as conn:
        await _clear_jobs(conn)
        invoice_id = await _insert_invoice(conn)
        job_id = await _enqueue(conn, invoice_id)

    job = await _claim(db_pool, "worker-a")
    assert job is not None
    assert job.id == job_id

    async with db_pool.connection() as conn:
        assert await is_superseded(conn, job) is False


async def test_is_superseded_is_true_once_a_newer_job_for_the_same_step_exists(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-5.1: a newer job for the same invoice and step makes the running one stale."""
    async with db_pool.connection() as conn:
        await _clear_jobs(conn)
        invoice_id = await _insert_invoice(conn)
        await _enqueue(conn, invoice_id)

    job = await _claim(db_pool, "worker-a")
    assert job is not None

    async with db_pool.connection() as conn:
        await _enqueue(conn, invoice_id)

    async with db_pool.connection() as conn:
        assert await is_superseded(conn, job) is True


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


async def test_the_last_attempt_calls_on_dead_once(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D190, BR-S03: the attempt that reaches MAX_ATTEMPTS calls on_dead with the invoice, step and error."""
    deaths = _Deaths()
    async with db_pool.connection() as conn:
        await _clear_jobs(conn)
        invoice_id = await _insert_invoice(conn)
        job_id = await _enqueue(conn, invoice_id)

    for attempt in range(MAX_ATTEMPTS):
        job = await _claim(db_pool, f"worker-{attempt}")
        assert job is not None
        async with db_pool.connection() as conn:
            assert await fail(conn, job, f"error-{attempt}", on_dead=deaths) is True
            await conn.execute(
                "UPDATE jobs SET run_after = now() WHERE id = %s AND attempts < %s", (job_id, MAX_ATTEMPTS)
            )

    assert deaths.calls == [(invoice_id, "fetch", f"error-{MAX_ATTEMPTS - 1}")]


async def test_an_earlier_attempt_does_not_call_on_dead(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D190, BR-S03: a failure before MAX_ATTEMPTS only backs off; on_dead is not called."""
    deaths = _Deaths()
    async with db_pool.connection() as conn:
        await _clear_jobs(conn)
        invoice_id = await _insert_invoice(conn)
        await _enqueue(conn, invoice_id)

    job = await _claim(db_pool, "worker-a")
    assert job is not None
    async with db_pool.connection() as conn:
        assert await fail(conn, job, "boom", on_dead=deaths) is True

    assert deaths.calls == []


async def test_a_crash_loop_calls_on_dead_once_from_claim(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D190, BR-S03: a job whose worker died MAX_CRASHES times calls on_dead once from claim."""
    deaths = _Deaths()
    async with db_pool.connection() as conn:
        await _clear_jobs(conn)
        invoice_id = await _insert_invoice(conn)
        job_id = await _enqueue(conn, invoice_id)

    for crash in range(MAX_CRASHES):
        assert await _claim(db_pool, f"worker-{crash}", on_dead=deaths) is not None
        await _expire_lease(db_pool, job_id)
    assert await _claim(db_pool, "worker-last", on_dead=deaths) is None
    assert await _claim(db_pool, "worker-after", on_dead=deaths) is None

    assert deaths.calls == [(invoice_id, "fetch", CRASH_LOOP_ERROR)]


async def test_a_failing_on_dead_rolls_the_death_back(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D190, BR-S03: when on_dead raises, the job and the invoice stay as they were."""
    async with db_pool.connection() as conn:
        await _clear_jobs(conn)
        invoice_id = await _insert_invoice(conn)
        job_id = await _enqueue(conn, invoice_id)
        await conn.execute("UPDATE jobs SET attempts = %s WHERE id = %s", (MAX_ATTEMPTS - 1, job_id))

    job = await _claim(db_pool, "worker-a")
    assert job is not None
    with pytest.raises(DeathRaisedError, match="boom"):
        async with db_pool.connection() as conn:
            await fail(conn, job, "boom", on_dead=_raise_dead)

    async with db_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT attempts, lease_until = %s, run_after < 'infinity'::timestamptz FROM jobs WHERE id = %s",
            (job.lease_until, job_id),
        )
        job_row = await cur.fetchone()
        cur = await conn.execute("SELECT state, rule_id FROM invoices WHERE id = %s", (invoice_id,))
        invoice = await cur.fetchone()
    assert job_row == (MAX_ATTEMPTS - 1, True, True)
    assert invoice == ("MANUAL_REQUIRED", None)


async def test_a_failing_on_dead_in_claim_still_parks_the_job(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D190, BR-S03: when on_dead raises in claim, the crash-looped job is parked and the queue goes on."""
    async with db_pool.connection() as conn:
        await _clear_jobs(conn)
        invoice_id = await _insert_invoice(conn)
        job_id = await _enqueue(conn, invoice_id)
        next_id = await _enqueue(conn, invoice_id)

    for crash in range(MAX_CRASHES):
        claimed = await _claim(db_pool, f"worker-{crash}", on_dead=_raise_dead)
        assert claimed is not None
        assert claimed.id == job_id
        await _expire_lease(db_pool, job_id)
    claimed = await _claim(db_pool, "worker-last", on_dead=_raise_dead)

    async with db_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT run_after = 'infinity'::timestamptz FROM jobs WHERE id = %s", (job_id,)
        )
        parked = await cur.fetchone()
        cur = await conn.execute("SELECT state, rule_id FROM invoices WHERE id = %s", (invoice_id,))
        invoice = await cur.fetchone()
    assert claimed is not None
    assert claimed.id == next_id
    assert parked == (True,)
    assert invoice == ("FAILED", "BR-S03")


async def test_a_dead_job_leaves_a_done_invoice_done(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S03, D190: the last failure of a job on a DONE invoice parks the job and keeps the invoice DONE."""
    deaths = _Deaths()
    async with db_pool.connection() as conn:
        await _clear_jobs(conn)
        invoice_id = await _insert_invoice(conn)
        await conn.execute("UPDATE invoices SET state = 'DONE' WHERE id = %s", (invoice_id,))
        job_id = await _enqueue(conn, invoice_id, "folder_status")
        await conn.execute("UPDATE jobs SET attempts = %s WHERE id = %s", (MAX_ATTEMPTS - 1, job_id))

    job = await _claim(db_pool, "worker-a")
    assert job is not None
    async with db_pool.connection() as conn:
        assert await fail(conn, job, "boom", on_dead=deaths) is True
        cur = await conn.execute("SELECT state, rule_id FROM invoices WHERE id = %s", (invoice_id,))
        invoice = await cur.fetchone()

    assert invoice == ("DONE", None)
    assert deaths.calls == [(invoice_id, "folder_status", "boom")]
