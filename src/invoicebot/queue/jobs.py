"""Durable job queue with leases, backoff and a crash-loop guard (S1.4).

Jobs live in the ``jobs`` table and are claimed with ``FOR UPDATE SKIP
LOCKED`` so concurrent workers never take the same job (R-9.4). A lease is a
``lease_until`` timestamp: a worker that dies leaves its lease to expire, and
the job is re-leased after that. Re-claiming an expired lease increments
``crashes``; at ``MAX_CRASHES`` the job is parked and its invoice fails with
``BR-S03`` (R-1.11). Failing a job retries it with exponential backoff and
parks it after ``MAX_ATTEMPTS`` failures, also with ``BR-S03``. Either way the
caller's ``on_dead`` runs in the same transaction, so the notice and the failed
invoice commit together (D190).
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection

from invoicebot.queue.retry import backoff_minutes
from invoicebot.states import SETTLED

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
MAX_CRASHES = 2

#: Reason recorded when the crash-loop guard parks a job (R-1.11, BR-S03).
CRASH_LOOP_ERROR = "worker died twice on the same job"

#: Called when a job dies, with the connection, invoice id, step and error (BR-S03, D190).
OnDead = Callable[[AsyncConnection[Any], int, str, str], Awaitable[None]]


@dataclass(frozen=True)
class Job:
    """A leased job as returned by :func:`claim`."""

    id: int
    invoice_id: int
    step: str
    attempts: int
    crashes: int
    lease_until: datetime


async def _fail_invoice(conn: AsyncConnection[Any], invoice_id: int, reason: str) -> None:
    """Move an invoice to FAILED with rule BR-S03 because its job is dead.

    A DONE or CANCELLED invoice keeps its state: a job that runs after filing, such
    as a folder rename, does not undo the filing (BR-S03, D190).
    """
    await conn.execute(
        """
        UPDATE invoices
        SET state = 'FAILED',
            rule_id = 'BR-S03',
            error = %(reason)s,
            updated_at = now()
        WHERE id = %(invoice_id)s
          AND NOT (state = ANY(%(settled)s))
        """,
        {"invoice_id": invoice_id, "reason": reason, "settled": sorted(SETTLED)},
    )


async def _notify_parked(conn: AsyncConnection[Any], on_dead: OnDead, invoice_id: int, step: str) -> None:
    """Run ``on_dead`` for a crash-looped job in a savepoint (BR-S03, D190).

    A failing ``on_dead`` loses only the notice: rolling back ``claim`` instead would
    leave the job first in line with its crash uncounted, and every later claim would
    fail on it, stopping the whole queue.
    """
    try:
        async with conn.transaction():
            await on_dead(conn, invoice_id, step, CRASH_LOOP_ERROR)
    except Exception:  # the queue's boundary: no notice failure may stop it
        logger.exception("job for invoice %d parked without its BR-S03 notice", invoice_id)


async def enqueue(
    conn: AsyncConnection[Any],
    invoice_id: int,
    step: str,
    run_after: datetime | None = None,
) -> int:
    """Insert a job for ``invoice_id`` and notify ``jobs`` listeners.

    ``run_after`` defaults to the database clock's ``now()``. The INSERT and
    the ``NOTIFY jobs`` are part of the caller's transaction, so a rollback
    enqueues nothing (R-9.4). The caller owns committing.
    """
    cur = await conn.execute(
        """
        INSERT INTO jobs (invoice_id, step, run_after)
        VALUES (%(invoice_id)s, %(step)s, COALESCE(%(run_after)s, now()))
        RETURNING id
        """,
        {"invoice_id": invoice_id, "step": step, "run_after": run_after},
    )
    await conn.execute("NOTIFY jobs")
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("INSERT INTO jobs RETURNING id yielded no row")
    return int(row[0])


async def claim(
    conn: AsyncConnection[Any],
    worker_id: str,
    lease_s: int,
    *,
    on_dead: OnDead,
) -> Job | None:
    """Claim one runnable job and lease it for ``lease_s`` seconds (R-9.4).

    Runs in its own transaction. Candidates are selected with ``FOR UPDATE
    SKIP LOCKED`` in ``run_after, id`` order. A candidate whose lease expired
    is a worker death: its ``crashes`` counter is incremented and, once it
    reaches ``MAX_CRASHES``, the job is parked and its invoice failed with
    ``BR-S03`` (R-1.11), and ``on_dead`` is called in a savepoint, before the
    loop moves to the next candidate.
    """
    logger.info("worker %s: claiming a job", worker_id)
    async with conn.transaction():
        while True:
            cur = await conn.execute(
                """
                SELECT id, invoice_id, step, attempts, crashes, lease_until
                FROM jobs
                WHERE run_after <= now()
                  AND (lease_until IS NULL OR lease_until < now())
                ORDER BY run_after, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """
            )
            row = await cur.fetchone()
            if row is None:
                return None
            job_id = int(row[0])
            invoice_id = int(row[1])
            step = str(row[2])
            attempts = int(row[3])
            crashes = int(row[4])
            if row[5] is not None:  # expired lease: its holder died (R-1.11)
                crashes += 1
                if crashes >= MAX_CRASHES:
                    logger.warning(
                        "worker %s: job %d died twice; parking it (BR-S03)",
                        worker_id,
                        job_id,
                    )
                    await conn.execute(
                        """
                        UPDATE jobs
                        SET run_after = 'infinity'::timestamptz,
                            lease_until = NULL,
                            crashes = %(crashes)s,
                            last_error = %(reason)s
                        WHERE id = %(job_id)s
                        """,
                        {"job_id": job_id, "crashes": crashes, "reason": CRASH_LOOP_ERROR},
                    )
                    await _fail_invoice(conn, invoice_id, CRASH_LOOP_ERROR)
                    await _notify_parked(conn, on_dead, invoice_id, step)
                    continue
                logger.info(
                    "worker %s: re-leasing job %d after a worker death",
                    worker_id,
                    job_id,
                )
            cur = await conn.execute(
                """
                UPDATE jobs
                SET lease_until = now() + make_interval(secs => %(lease_s)s),
                    crashes = %(crashes)s
                WHERE id = %(job_id)s
                RETURNING lease_until
                """,
                {"lease_s": lease_s, "crashes": crashes, "job_id": job_id},
            )
            row = await cur.fetchone()
            if row is None:
                raise RuntimeError("UPDATE jobs RETURNING lease_until yielded no row")
            return Job(
                id=job_id,
                invoice_id=invoice_id,
                step=step,
                attempts=attempts,
                crashes=crashes,
                lease_until=row[0],
            )


async def complete(conn: AsyncConnection[Any], job: Job) -> bool:
    """Delete ``job`` while the caller still holds its lease (R-9.4).

    Returns ``False`` and changes nothing when the lease has been lost to
    another worker.
    """
    cur = await conn.execute(
        "DELETE FROM jobs WHERE id = %(job_id)s AND lease_until = %(lease_until)s",
        {"job_id": job.id, "lease_until": job.lease_until},
    )
    return cur.rowcount == 1


async def is_superseded(conn: AsyncConnection[Any], job: Job) -> bool:
    """Return True when a newer job for the same invoice and step exists (R-5.1).

    Locks ``job``'s own row first, so two runs of the same job serialize; a
    missing row is not an error because the worker deletes a job after its
    handler commits. A crash before that delete redelivers the job, and the
    next job the handler enqueued (a higher ``id``) makes this one stale. The
    caller owns the transaction and commits.
    """
    await conn.execute(
        "SELECT id FROM jobs WHERE id = %(job_id)s FOR UPDATE",
        {"job_id": job.id},
    )
    cur = await conn.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM jobs
            WHERE invoice_id = %(invoice_id)s
              AND step = %(step)s
              AND id > %(job_id)s
        )
        """,
        {"invoice_id": job.invoice_id, "step": job.step, "job_id": job.id},
    )
    row = await cur.fetchone()
    return row is not None and bool(row[0])


async def fail(conn: AsyncConnection[Any], job: Job, error: str, *, on_dead: OnDead) -> bool:
    """Record a failed attempt for ``job`` (R-9.4).

    Succeeds only while the caller still holds the lease (``WHERE id = job.id
    AND lease_until = job.lease_until``); when the lease was lost, nothing
    changes and ``False`` is returned. A failure increments ``attempts``,
    stores ``last_error`` and clears the lease. The retry is scheduled
    ``backoff_minutes(attempts)`` minutes ahead, or the job is parked at
    infinity once ``attempts`` reaches ``MAX_ATTEMPTS``, its invoice failed and
    ``on_dead`` called (BR-S03).
    """
    new_attempts = job.attempts + 1
    dead = new_attempts >= MAX_ATTEMPTS
    # One transaction, so a parked job and its failed invoice change together.
    async with conn.transaction():
        if dead:
            cur = await conn.execute(
                """
                UPDATE jobs
                SET attempts = attempts + 1,
                    last_error = %(error)s,
                    run_after = 'infinity'::timestamptz,
                    lease_until = NULL
                WHERE id = %(job_id)s AND lease_until = %(lease_until)s
                RETURNING invoice_id
                """,
                {"job_id": job.id, "lease_until": job.lease_until, "error": error},
            )
        else:
            cur = await conn.execute(
                """
                UPDATE jobs
                SET attempts = attempts + 1,
                    last_error = %(error)s,
                    run_after = now() + make_interval(mins => %(backoff)s),
                    lease_until = NULL
                WHERE id = %(job_id)s AND lease_until = %(lease_until)s
                RETURNING invoice_id
                """,
                {
                    "job_id": job.id,
                    "lease_until": job.lease_until,
                    "error": error,
                    "backoff": backoff_minutes(new_attempts),
                },
            )
        row = await cur.fetchone()
        if row is None:
            return False
        if dead:
            invoice_id = int(row[0])
            await _fail_invoice(conn, invoice_id, error)
            await on_dead(conn, invoice_id, job.step, error)
        return True


async def is_dead(conn: AsyncConnection[Any], job_id: int) -> bool:
    """Return True when ``job_id`` has been parked (``run_after = infinity``).

    A parked job failed ``MAX_ATTEMPTS`` times or its worker died
    ``MAX_CRASHES`` times (BR-S03).
    """
    cur = await conn.execute(
        "SELECT run_after = 'infinity'::timestamptz FROM jobs WHERE id = %(job_id)s",
        {"job_id": job_id},
    )
    row = await cur.fetchone()
    return row is not None and bool(row[0])
