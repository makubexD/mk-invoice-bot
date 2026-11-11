"""Integration tests for the worker loop and the heavy semaphore (R-9.4)."""

import asyncio
import contextlib
import time
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.queue.jobs import Job, enqueue
from invoicebot.queue.worker import HEAVY, Worker

pytestmark = pytest.mark.asyncio(loop_scope="session")

Pool = AsyncConnectionPool[AsyncConnection[Any]]


async def _reset(pool: Pool) -> int:
    """Delete leftover jobs and insert a fresh invoice owned by this test."""
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM jobs")
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


async def _enqueue(pool: Pool, invoice_id: int, step: str) -> int:
    """Commit one job for ``invoice_id`` (and its NOTIFY)."""
    async with pool.connection() as conn:
        return await enqueue(conn, invoice_id, step)


async def _stop(task: "asyncio.Task[None]") -> None:
    """Cancel a worker task and wait for it to unwind (always used in a finally)."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _wait_for_count(pool: Pool, job_id: int, expected: int, deadline_s: float = 2.0) -> None:
    """Wait until the ``jobs`` table holds ``expected`` rows for ``job_id``."""
    deadline = time.monotonic() + deadline_s
    while True:
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM jobs WHERE id = %s", (job_id,))
            row = await cur.fetchone()
        assert row is not None
        count = int(row[0])
        if count == expected:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"job {job_id}: expected {expected} rows, found {count}")
        await asyncio.sleep(0.02)


async def _wait_for_failure(pool: Pool, job_id: int, deadline_s: float = 2.0) -> tuple[int, str]:
    """Wait until ``job_id`` has one recorded failure and return (attempts, last_error)."""
    deadline = time.monotonic() + deadline_s
    while True:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT attempts, last_error FROM jobs WHERE id = %s",
                (job_id,),
            )
            row = await cur.fetchone()
        if row is not None and row[0] >= 1:
            return int(row[0]), str(row[1])
        if time.monotonic() >= deadline:
            raise AssertionError(f"job {job_id} was not failed within {deadline_s}s")
        await asyncio.sleep(0.02)


async def test_job_enqueued_after_start_runs_within_one_second(
    db_pool: Pool,
) -> None:
    """R-9.4: NOTIFY wakes an idle worker, so a new job runs within a second."""
    invoice_id = await _reset(db_pool)
    ran = asyncio.Event()
    served: list[int] = []

    async def handler(job: Job) -> None:
        served.append(job.id)
        ran.set()

    # A large idle timeout means only the NOTIFY can wake the worker.
    worker = Worker(db_pool, {"fetch": handler}, idle_timeout_s=120.0)
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.sleep(0.5)  # let the worker LISTEN and go idle
        job_id = await _enqueue(db_pool, invoice_id, "fetch")
        await asyncio.wait_for(ran.wait(), timeout=1.0)
        assert served == [job_id]
        await _wait_for_count(db_pool, job_id, 0)
    finally:
        await _stop(task)


async def test_unknown_step_fails_and_the_loop_keeps_serving(
    db_pool: Pool,
) -> None:
    """R-9.4: an unknown step is failed naming it, and the next job still runs."""
    invoice_id = await _reset(db_pool)
    served = asyncio.Event()

    async def handler(job: Job) -> None:
        served.set()

    bad_id = await _enqueue(db_pool, invoice_id, "mystery")
    good_id = await _enqueue(db_pool, invoice_id, "fetch")
    worker = Worker(db_pool, {"fetch": handler}, idle_timeout_s=120.0)
    task = asyncio.create_task(worker.run())
    try:
        attempts, last_error = await _wait_for_failure(db_pool, bad_id)
        assert attempts == 1
        assert "unknown step" in last_error
        assert "mystery" in last_error
        await asyncio.wait_for(served.wait(), timeout=2.0)
        await _wait_for_count(db_pool, good_id, 0)
    finally:
        await _stop(task)


async def test_handler_exception_fails_the_job_and_the_loop_survives(
    db_pool: Pool,
) -> None:
    """R-9.4: a raising handler fails its job and the worker keeps serving."""
    invoice_id = await _reset(db_pool)
    served = asyncio.Event()

    async def boom(job: Job) -> None:
        raise RuntimeError("kaboom")

    async def ok(job: Job) -> None:
        served.set()

    bad_id = await _enqueue(db_pool, invoice_id, "boom")
    good_id = await _enqueue(db_pool, invoice_id, "ok")
    worker = Worker(db_pool, {"boom": boom, "ok": ok}, idle_timeout_s=120.0)
    task = asyncio.create_task(worker.run())
    try:
        attempts, last_error = await _wait_for_failure(db_pool, bad_id)
        assert attempts == 1
        assert "RuntimeError" in last_error
        assert "kaboom" in last_error
        await asyncio.wait_for(served.wait(), timeout=2.0)
        await _wait_for_count(db_pool, good_id, 0)
    finally:
        await _stop(task)


async def test_two_heavy_handlers_never_overlap(db_pool: Pool) -> None:
    """R-9.4: HEAVY serialises heavy handlers, even across two workers."""
    invoice_id = await _reset(db_pool)
    intervals: list[tuple[float, float, int]] = []
    first_started = asyncio.Event()
    both_done = asyncio.Event()

    async def heavy(job: Job) -> None:
        async with HEAVY:
            running = asyncio.current_task()
            assert running is not None
            start = time.monotonic()
            first_started.set()
            await asyncio.sleep(0.25)
            intervals.append((start, time.monotonic(), id(running)))
            if len(intervals) == 2:
                both_done.set()

    await _enqueue(db_pool, invoice_id, "heavy")
    await _enqueue(db_pool, invoice_id, "heavy")
    first = Worker(db_pool, {"heavy": heavy}, worker_id="worker-a", idle_timeout_s=120.0)
    second = Worker(db_pool, {"heavy": heavy}, worker_id="worker-b", idle_timeout_s=120.0)
    first_task = asyncio.create_task(first.run())
    second_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(first_started.wait(), timeout=2.0)
        second_task = asyncio.create_task(second.run())
        await asyncio.wait_for(both_done.wait(), timeout=5.0)
    finally:
        await _stop(first_task)
        if second_task is not None:
            await _stop(second_task)

    assert len(intervals) == 2
    (start_a, end_a, task_a), (start_b, end_b, task_b) = intervals
    assert end_a <= start_b or end_b <= start_a
    assert task_a != task_b  # two workers, not one draining both jobs


async def test_cancelling_the_worker_finishes_quickly(db_pool: Pool) -> None:
    """R-9.4: cancelling run() releases its connections and raises within a second."""
    await _reset(db_pool)
    worker = Worker(db_pool, {}, idle_timeout_s=120.0)
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.sleep(0.5)
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - started < 1.0
    finally:
        await _stop(task)
