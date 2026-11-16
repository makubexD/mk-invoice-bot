"""Transactional outbox with at-least-once delivery and backoff (R-11.5).

A row is written with :func:`add` in the caller's transaction, so the message
exists exactly when the state change that produced it commits (R-1.8).
:class:`OutboxSender` picks due rows with ``FOR UPDATE SKIP LOCKED``, calls the
transport for the row's channel while holding the row lock, marks it ``sent``
and stores the message id the channel returned. A failure increments
``attempts``, stores ``last_error`` and reschedules the row with exponential
backoff; once ``max_attempts`` is reached the row becomes ``dead`` and is
logged. A :class:`PermanentSendError` marks the row ``dead`` at once, whatever
the attempt count. All scheduling uses the database clock.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
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


#: A coroutine that delivers one outbox row and returns the channel's message id,
#: or None when the channel assigns none; raising means the send failed.
Transport = Callable[[OutboxRow], Awaitable[str | None]]


class PermanentSendError(Exception):
    """The row can never be sent; retrying is pointless."""


async def add(
    conn: AsyncConnection[Any],
    channel: str,
    to: str,
    payload: dict[str, Any],
    *,
    template: str | None = None,
    invoice_id: int | None = None,
) -> int:
    """Insert a pending outbox row in the caller's transaction and return its id.

    The channel is validated before any SQL runs. ``invoice_id`` links the row
    to the invoice it talks about, so a parked invoice's question can be sent
    again (R-3.7). The caller owns committing, so a rollback enqueues nothing
    (R-1.8).
    """
    if channel not in VALID_CHANNELS:
        raise ValueError(f"unknown outbox channel: {channel!r}")
    cur = await conn.execute(
        """
        INSERT INTO outbox (channel, "to", payload_json, template, state, invoice_id)
        VALUES (%(channel)s, %(to)s, %(payload)s, %(template)s, 'pending', %(invoice_id)s)
        RETURNING id
        """,
        {
            "channel": channel,
            "to": to,
            "payload": Json(payload),
            "template": template,
            "invoice_id": invoice_id,
        },
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("INSERT INTO outbox RETURNING id yielded no row")
    return int(row[0])


async def last_text(conn: AsyncConnection[Any], invoice_id: int, rule_id: str) -> str | None:
    """Return the newest stored text for invoice_id whose payload names rule_id.

    ``/estado`` shows the invoice's last question, so an invoice with no stored
    text for the rule (or none at all) answers None. Does not commit.
    """
    cur = await conn.execute(
        """
        SELECT payload_json->>'text'
        FROM outbox
        WHERE invoice_id = %(invoice_id)s AND payload_json->>'rule_id' = %(rule_id)s
        ORDER BY id DESC
        LIMIT 1
        """,
        {"invoice_id": invoice_id, "rule_id": rule_id},
    )
    row = await cur.fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


async def last_rule(conn: AsyncConnection[Any], invoice_id: int, ignore: str) -> str | None:
    """Return the newest rule id stored for invoice_id, skipping ``ignore`` (BR-S02).

    BR-S02 answers a stale press, so it is not the question the invoice waits on:
    callers pass it as ``ignore`` to find the last real question. A row without a
    rule id, and an invoice with no such row, give None. Does not commit.
    """
    cur = await conn.execute(
        """
        SELECT payload_json->>'rule_id'
        FROM outbox
        WHERE invoice_id = %(invoice_id)s
          AND payload_json->>'rule_id' IS NOT NULL
          AND payload_json->>'rule_id' <> %(ignore)s
        ORDER BY id DESC
        LIMIT 1
        """,
        {"invoice_id": invoice_id, "ignore": ignore},
    )
    row = await cur.fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


async def requeue_last(
    conn: AsyncConnection[Any],
    invoice_id: int,
    rule_id: str,
    min_interval: timedelta,
) -> bool:
    """Queue the invoice's newest message for rule_id again, returning whether it did.

    ``/pendientes`` sends a parked question again with the same channel, recipient and
    payload, buttons included, as a new pending row in the caller's transaction
    (S4.8, D94). An invoice with no stored message for the rule queues nothing, so a
    missing question is never invented; one sent within ``min_interval`` is not sent
    again, measured on the database clock. Does not commit.
    """
    cur = await conn.execute(
        """
        INSERT INTO outbox (channel, "to", payload_json, template, state, invoice_id)
        SELECT channel, "to", payload_json, template, 'pending', invoice_id
        FROM (
            SELECT channel, "to", payload_json, template, invoice_id, created_at
            FROM outbox
            WHERE invoice_id = %(invoice_id)s
              AND payload_json->>'rule_id' = %(rule_id)s
            ORDER BY id DESC
            LIMIT 1
        ) AS newest
        WHERE newest.created_at <= now() - %(min_interval)s
        """,
        {
            "invoice_id": invoice_id,
            "rule_id": rule_id,
            "min_interval": min_interval,
        },
    )
    return cur.rowcount == 1


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
                    message_id, failure = await self._deliver(row)
                    if failure is None:
                        await conn.execute(
                            """
                            UPDATE outbox
                            SET state = 'sent', sent_at = now(), channel_message_id = %(message_id)s
                            WHERE id = %(id)s
                            """,
                            {"id": row.id, "message_id": message_id},
                        )
                        sent += 1
                    else:
                        error, permanent = failure
                        await self._record_failure(conn, row, error, permanent=permanent)

    async def _deliver(self, row: OutboxRow) -> tuple[str | None, tuple[str, bool] | None]:
        """Call the row's transport.

        Return the channel's message id and no failure when the row was
        delivered, or None and the error description with whether it is
        permanent.
        """
        transport = self._transports.get(row.channel)
        if transport is None:
            return None, (f"no transport for channel {row.channel}", False)
        try:
            message_id = await transport(row)
        except Exception as exc:  # CancelledError is a BaseException and propagates
            return None, (describe_error(exc), isinstance(exc, PermanentSendError))
        return message_id, None

    async def _record_failure(
        self,
        conn: AsyncConnection[Any],
        row: OutboxRow,
        error: str,
        *,
        permanent: bool,
    ) -> None:
        """Increment ``attempts`` and reschedule the row, or mark it dead."""
        attempts = row.attempts + 1
        if permanent or attempts >= self._max_attempts:
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
