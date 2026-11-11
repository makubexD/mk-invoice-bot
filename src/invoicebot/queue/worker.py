"""Worker loop that runs durable jobs (S1.4, R-9.4).

A worker holds one dedicated autocommit connection with ``LISTEN jobs`` for its
whole lifetime. Each cycle it drains every claimable job and then waits for the
next ``NOTIFY`` (or an idle timeout, as a safety net) before draining again.
The ``LISTEN`` is active before the first drain so a notification that arrives
while no job is claimable is not lost.

A job is claimed on one pooled connection, its handler runs with no database
transaction open, and its outcome is recorded on another pooled connection.
Cancellation is never swallowed: the job keeps its lease, so another worker
re-claims it once the lease expires.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.queue.jobs import Job, claim, complete, fail

logger = logging.getLogger(__name__)

Pool = AsyncConnectionPool[AsyncConnection[Any]]

Handler = Callable[[Job], Awaitable[None]]

#: The single heavy-work slot of this process. Heavy handlers (OCR, LLM calls)
#: must hold it with ``async with HEAVY:`` so at most one runs at a time.
HEAVY = asyncio.Semaphore(1)

#: ``last_error`` only needs to identify the failure; a traceback is in the log.
MAX_ERROR_CHARS = 500


class Worker:
    """Claim and run jobs until cancelled (R-9.4)."""

    def __init__(
        self,
        pool: Pool,
        handlers: Mapping[str, Handler],
        *,
        worker_id: str = "worker-1",
        lease_s: int = 300,
        idle_timeout_s: float = 60.0,
    ) -> None:
        self._pool = pool
        self._handlers = handlers
        self._worker_id = worker_id
        self._lease_s = lease_s
        self._idle_timeout_s = idle_timeout_s

    async def run(self) -> None:
        """Listen for jobs and run them until the task is cancelled (R-9.4)."""
        listen_conn = await self._pool.getconn()
        try:
            await listen_conn.set_autocommit(True)
            # LISTEN before the first drain: a NOTIFY committed between a drain
            # and the wait below would otherwise be lost.
            await listen_conn.execute("LISTEN jobs")
            logger.info("worker %s: listening for jobs", self._worker_id)
            while True:
                await self._drain()
                await self._wait_for_notification(listen_conn)
        finally:
            await self._release_listen_connection(listen_conn)

    async def _release_listen_connection(self, conn: AsyncConnection[Any]) -> None:
        """Give the LISTEN connection back to the pool with its defaults."""
        try:
            if not conn.closed:
                await conn.set_autocommit(False)
        except Exception:  # the connection may have died while listening
            logger.warning("worker %s: could not reset the LISTEN connection", self._worker_id)
        await self._pool.putconn(conn)

    async def _drain(self) -> None:
        """Run claimable jobs one at a time until none is left (R-9.4)."""
        while True:
            async with self._pool.connection() as conn:
                job = await claim(conn, self._worker_id, self._lease_s)
            if job is None:
                return
            await self._run_job(job)

    async def _run_job(self, job: Job) -> None:
        """Run one handler outside a transaction and record the outcome."""
        handler = self._handlers.get(job.step)
        if handler is None:
            await self._record_failure(job, f"unknown step: {job.step}")
            return
        logger.info("worker %s: running job %d (%s)", self._worker_id, job.id, job.step)
        try:
            await handler(job)
        except asyncio.CancelledError:
            # Keep the lease: the job is re-claimed once it expires (R-1.11).
            raise
        except Exception as exc:
            logger.warning("worker %s: job %d (%s) raised", self._worker_id, job.id, job.step, exc_info=True)
            await self._record_failure(job, describe_error(exc))
            return
        async with self._pool.connection() as conn:
            if not await complete(conn, job):
                logger.warning("worker %s: job %d finished after losing its lease", self._worker_id, job.id)

    async def _record_failure(self, job: Job, error: str) -> None:
        """Record a failed attempt; a lost lease is only logged (R-9.4)."""
        async with self._pool.connection() as conn:
            if not await fail(conn, job, error):
                logger.warning("worker %s: job %d failed after losing its lease", self._worker_id, job.id)

    async def _wait_for_notification(self, conn: AsyncConnection[Any]) -> None:
        """Wait for one ``jobs`` notification, or the idle timeout (R-9.4)."""
        async for _notification in conn.notifies(timeout=self._idle_timeout_s):
            return


def describe_error(exc: BaseException) -> str:
    """Render ``exc`` as ``Type: message``, capped for the jobs table."""
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
