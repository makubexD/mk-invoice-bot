"""Transactional outbox with at-least-once delivery and backoff (R-11.5).

A row is written with :func:`add` in the caller's transaction, so the message
exists exactly when the state change that produced it commits (R-1.8).
:class:`OutboxSender` picks due rows with ``FOR UPDATE SKIP LOCKED``, calls the
transport for the row's channel while holding the row lock and marks it
``sent``. A failure increments ``attempts``, stores ``last_error`` and
reschedules the row with exponential backoff; once ``max_attempts`` is reached
the row becomes ``dead`` and is logged. All scheduling uses the database clock.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from psycopg import AsyncConnection
from psycopg.types.json import Json

from invoicebot.queue.retry import backoff_minutes, describe_error
from invoicebot.store.db import Pool

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
VALID_CHANNELS = frozenset({"whatsapp", "telegram", "github", "event"})


@dataclass(frozen=True)
class OutboxRow:
    """A due outbox row handed to a transport."""

    id: int
    channel: str
    to: str
    payload: dict[str, Any]
    template: str | None
    attempts: int


#: A coroutine that delivers one outbox row; raising means the send failed.
Transport = Callable[[OutboxRow], Awaitable[None]]


async def add(
    conn: AsyncConnection[Any],
    channel: str,
    to: str,
    payload: dict[str, Any],
    *,
    template: str | None = None,
) -> int:
    """Insert a pending outbox row in the caller's transaction and return its id.

    The channel is validated before any SQL runs. The caller owns committing,
    so a rollback enqueues nothing (R-1.8).
    """
    if channel not in VALID_CHANNELS:
        raise ValueError(f"unknown outbox channel: {channel!r}")
    cur = await conn.execute(
        """
        INSERT INTO outbox (channel, "to", payload_json, template, state)
        VALUES (%(channel)s, %(to)s, %(payload)s, %(template)s, 'pending')
        RETURNING id
        """,
        {"channel": channel, "to": to, "payload": Json(payload), "template": template},
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("INSERT INTO outbox RETURNING id yielded no row")
    return int(row[0])


class OutboxSender:
    """Deliver pending outbox rows through per-channel transports.

    One row is processed per transaction with ``FOR UPDATE SKIP LOCKED``, so
    concurrent senders never take the same row. A failed row is rescheduled
    and never blocks the rows behind it in the same :meth:`send_pending` call.
    """

    def __init__(
        self,
        pool: Pool,
        transports: Mapping[str, Transport],
        *,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self._pool = pool
        self._transports = dict(transports)
        self._max_attempts = max_attempts

    async def send_pending(self) -> int:
        """Send every due row once and return how many were delivered.

        Stops at the first moment no row is due. ``asyncio.CancelledError``
        propagates and rolls the in-flight transaction back, leaving its row
        pending.
        """
        sent = 0
        async with self._pool.connection() as conn:
            while True:
                async with conn.transaction():
                    cur = await conn.execute(
                        """
                        SELECT id, channel, "to", payload_json, template, attempts
                        FROM outbox
                        WHERE state = 'pending' AND run_after <= now()
                        ORDER BY id
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                        """
                    )
                    raw = await cur.fetchone()
                    if raw is None:
                        return sent
                    row = _outbox_row(raw)
                    error = await self._deliver(row)
                    if error is None:
                        await conn.execute(
                            "UPDATE outbox SET state = 'sent', sent_at = now() WHERE id = %(id)s",
                            {"id": row.id},
                        )
                        sent += 1
                    else:
                        await self._record_failure(conn, row, error)

    async def _deliver(self, row: OutboxRow) -> str | None:
        """Call the row's transport; return an error description or None."""
        transport = self._transports.get(row.channel)
        if transport is None:
            return f"no transport for channel {row.channel}"
        try:
            await transport(row)
        except Exception as exc:  # CancelledError is a BaseException and propagates
            return describe_error(exc)
        return None

    async def _record_failure(
        self,
        conn: AsyncConnection[Any],
        row: OutboxRow,
        error: str,
    ) -> None:
        """Increment ``attempts`` and reschedule the row, or mark it dead."""
        attempts = row.attempts + 1
        if attempts >= self._max_attempts:
            await conn.execute(
                """
                UPDATE outbox
                SET attempts = %(attempts)s, last_error = %(error)s, state = 'dead'
                WHERE id = %(id)s
                """,
                {"attempts": attempts, "error": error, "id": row.id},
            )
            logger.error(
                "outbox row %d on channel %s is dead after %d attempts: %s",
                row.id,
                row.channel,
                attempts,
                error,
            )
            return
        await conn.execute(
            """
            UPDATE outbox
            SET attempts = %(attempts)s,
                last_error = %(error)s,
                run_after = now() + make_interval(mins => %(backoff)s)
            WHERE id = %(id)s
            """,
            {
                "attempts": attempts,
                "error": error,
                "backoff": backoff_minutes(attempts),
                "id": row.id,
            },
        )


def _outbox_row(raw: tuple[Any, ...]) -> OutboxRow:
    """Build an :class:`OutboxRow` from a selected ``outbox`` row."""
    return OutboxRow(
        id=int(raw[0]),
        channel=str(raw[1]),
        to=str(raw[2]),
        payload=cast(dict[str, Any], raw[3]),
        template=cast(str | None, raw[4]),
        attempts=int(raw[5]),
    )
