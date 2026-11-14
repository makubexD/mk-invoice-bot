"""Invoice, file and channel-window persistence for the intake pipeline.

Every function takes the caller's connection and never commits: the caller
owns the transaction (R-1.1, R-1.4, R-1.10).
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, cast

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from invoicebot.channels.base import AttachmentKind, ChannelName
from invoicebot.extract.fields import Reading, reading_from_json, reading_to_json
from invoicebot.rules.catalog import RULES
from invoicebot.states import (
    BATCHED,
    CANCELLED,
    FAILED,
    MANUAL_REQUIRED,
    NEEDS_RULE,
    PENDING_USER,
    WAITING_FOR_USER,
    StateMoved,
)

#: Length of the WhatsApp customer-service window, in hours (R-1.10).
WINDOW_HOURS = 24

#: Rule that parks an invoice whose question went unanswered (BR-S08).
RULE_PARKED = "BR-S08"

#: The kinds an invoice's file may have: the channel attachments plus flow B's own files.
type FileKind = AttachmentKind | Literal["eml", "manifest"]


@dataclass(frozen=True)
class FileRecord:
    """A file to attach to an invoice: where it came from and what it is (R-1.4)."""

    kind: FileKind
    local_path: str | None
    remote_ref: str | None
    sha256: str
    size: int


@dataclass(frozen=True)
class InvoiceHead:
    """The invoice columns the pipeline handlers act on (R-1.1)."""

    state: str
    profile_id: str
    origin_channel: ChannelName


@dataclass(frozen=True)
class Duplicate:
    """The invoice still holding a natural key, and when it arrived (BR-E04, D93)."""

    invoice_id: int
    created_at: datetime


@dataclass(frozen=True)
class InvoiceRef:
    """A parked invoice and who to tell about it (BR-S08)."""

    id: int
    profile_id: str
    origin_channel: ChannelName


@dataclass(frozen=True)
class PendingRow:
    """An invoice that waits on the sender, for ``/pendientes`` (S4.8, S11.4)."""

    id: int
    state: str
    waiting_state: str | None
    waiting_rule_id: str | None
    razon_social: str | None
    total: Decimal | None
    batch_prepared_at: datetime | None


async def create_invoice(conn: AsyncConnection[Any], profile_id: str, origin_channel: ChannelName) -> int:
    """Insert an invoice in RECEIVED for profile_id and return its id (R-1.1)."""
    cur = await conn.execute(
        """
        INSERT INTO invoices (profile_id, origin_channel, state)
        VALUES (%s, %s, 'RECEIVED')
        RETURNING id
        """,
        (profile_id, origin_channel),
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("INSERT INTO invoices RETURNING id yielded no row")
    return int(row[0])


async def get_state(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str, str] | None:
    """Return ``(state, profile_id)`` for invoice_id, or None when it does not exist."""
    cur = await conn.execute("SELECT state, profile_id FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1])


async def get_state_rule(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str, str | None] | None:
    """Return ``(state, rule_id)`` for invoice_id, or None when it does not exist."""
    cur = await conn.execute("SELECT state, rule_id FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    if row is None:
        return None
    return str(row[0]), None if row[1] is None else str(row[1])


async def get_waiting(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str | None, str | None] | None:
    """Return ``(waiting_state, waiting_rule_id)`` for invoice_id, or None when it does not exist."""
    cur = await conn.execute(
        "SELECT waiting_state, waiting_rule_id FROM invoices WHERE id = %s",
        (invoice_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return (None if row[0] is None else str(row[0])), (None if row[1] is None else str(row[1]))


async def load_invoice_head(conn: AsyncConnection[Any], invoice_id: int) -> InvoiceHead | None:
    """Return the invoice's state, profile and channel, or None when it does not exist."""
    cur = await conn.execute(
        "SELECT state, profile_id, origin_channel FROM invoices WHERE id = %s",
        (invoice_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return InvoiceHead(
        state=str(row[0]),
        profile_id=str(row[1]),
        origin_channel=cast(ChannelName, row[2]),
    )


async def lock_invoice(conn: AsyncConnection[Any], invoice_id: int) -> None:
    """Lock invoice_id's row for the caller's transaction (D95).

    A job that takes this lock before it writes serializes against an edit's update: an
    edit that committed first leaves a newer job, so the older run sees it as superseded.
    Does not commit.
    """
    await conn.execute("SELECT 1 FROM invoices WHERE id = %s FOR UPDATE", (invoice_id,))


async def set_flow(conn: AsyncConnection[Any], invoice_id: int, flow: str) -> None:
    """Store the flow the invoice was routed to (R-3.1)."""
    await conn.execute("UPDATE invoices SET flow_id = %s WHERE id = %s", (flow, invoice_id))


async def load_flow(conn: AsyncConnection[Any], invoice_id: int) -> str | None:
    """Return the flow the invoice was routed to, or None before routing or when it does not exist."""
    cur = await conn.execute("SELECT flow_id FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    return None if row is None or row[0] is None else str(row[0])


async def set_state(
    conn: AsyncConnection[Any],
    invoice_id: int,
    state: str,
    *,
    rule_id: str | None = None,
    error: str | None = None,
    expected: str | None = None,
) -> None:
    """Move invoice_id to state, record rule_id and error, and stamp updated_at.

    A state in ``NEEDS_RULE`` requires a catalogued rule id (S5.2), and any rule id
    given must be in :data:`invoicebot.rules.catalog.RULES`. With ``expected``, the
    UPDATE only matches a row still in that state; when it moved on, :class:`StateMoved`
    carries the state found and the row is left unchanged (D95). Does not commit.
    """
    if state in NEEDS_RULE and rule_id is None:
        raise ValueError(f"{state} needs a rule id")
    if rule_id is not None and rule_id not in RULES:
        raise ValueError(f"unknown rule id {rule_id!r}")
    if expected is None:
        await conn.execute(
            """
            UPDATE invoices
            SET state = %s,
                rule_id = %s,
                error = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (state, rule_id, error, invoice_id),
        )
        return
    cur = await conn.execute(
        """
        UPDATE invoices
        SET state = %s,
            rule_id = %s,
            error = %s,
            updated_at = now()
        WHERE id = %s
          AND state = %s
        """,
        (state, rule_id, error, invoice_id, expected),
    )
    if cur.rowcount == 0:
        raise StateMoved(invoice_id, expected, await _read_state(conn, invoice_id))


async def _read_state(conn: AsyncConnection[Any], invoice_id: int) -> str | None:
    """Return the stored state of invoice_id, or None when the row does not exist."""
    cur = await conn.execute("SELECT state FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    return None if row is None else str(row[0])


async def claim_natural_key(conn: AsyncConnection[Any], invoice_id: int, key: str) -> Duplicate | None:
    """Claim ``key`` for invoice_id and return the invoice still holding it (BR-E04, D93).

    A ``FAILED`` or ``CANCELLED`` holder gives the key up first. Any other holder wins
    and is returned, leaving this invoice without a key. Otherwise this invoice takes
    the key, replacing one it claimed before. A unique violation from a concurrent
    claim propagates; the job retries and then finds the holder. Does not commit.
    """
    await conn.execute(
        """
        UPDATE invoices
        SET natural_key = NULL
        WHERE natural_key = %s
          AND id <> %s
          AND state IN (%s, %s)
        """,
        (key, invoice_id, FAILED, CANCELLED),
    )
    cur = await conn.execute(
        """
        SELECT id, created_at
        FROM invoices
        WHERE natural_key = %s
          AND id <> %s
        LIMIT 1
        """,
        (key, invoice_id),
    )
    row = await cur.fetchone()
    if row is not None:
        return Duplicate(invoice_id=int(row[0]), created_at=cast(datetime, row[1]))
    await conn.execute("UPDATE invoices SET natural_key = %s WHERE id = %s", (key, invoice_id))
    return None


async def resume_pending_auth(conn: AsyncConnection[Any]) -> list[int]:
    """Move every ``PENDING_AUTH`` invoice to ``DELIVERING`` and return their ids (BR-D01).

    The caller adds each invoice's ``deliver`` job in the same transaction, so a
    crash never leaves an invoice ``DELIVERING`` without its job.
    """
    cur = await conn.execute(
        """
        UPDATE invoices
        SET state = 'DELIVERING',
            rule_id = NULL,
            error = NULL,
            updated_at = now()
        WHERE state = 'PENDING_AUTH'
        RETURNING id
        """
    )
    return [int(row[0]) for row in await cur.fetchall()]


async def park_stale(conn: AsyncConnection[Any], timeout_h: int) -> list[InvoiceRef]:
    """Park invoices whose question went unanswered for ``timeout_h`` hours (BR-S08).

    Each invoice still in ``WAITING_FOR_USER`` whose last update is older than the
    timeout moves to ``PENDING_USER``, keeping the state and rule it was waiting in
    so an answer can resume it (D94). The database clock decides the age. Returns the
    invoices moved, for the caller to tell their senders. Does not commit.
    """
    cur = await conn.execute(
        """
        UPDATE invoices
        SET waiting_state = state,
            waiting_rule_id = rule_id,
            state = %s,
            rule_id = %s,
            updated_at = now()
        WHERE state = ANY(%s)
          AND updated_at < now() - make_interval(hours => %s)
        RETURNING id, profile_id, origin_channel
        """,
        (PENDING_USER, RULE_PARKED, list(WAITING_FOR_USER), timeout_h),
    )
    return [
        InvoiceRef(
            id=int(row[0]),
            profile_id=str(row[1]),
            origin_channel=cast(ChannelName, row[2]),
        )
        for row in await cur.fetchall()
    ]


async def unpark(conn: AsyncConnection[Any], invoice_id: int) -> bool:
    """Restore a parked invoice's waiting state and rule, returning whether it did (D94).

    Only a ``PENDING_USER`` row that stored its ``waiting_state`` is resumed; the waiting
    columns are cleared and the database clock stamps ``updated_at``. Does not commit.
    """
    cur = await conn.execute(
        """
        UPDATE invoices
        SET state = waiting_state,
            rule_id = waiting_rule_id,
            waiting_state = NULL,
            waiting_rule_id = NULL,
            updated_at = now()
        WHERE id = %s
          AND state = %s
          AND waiting_state IS NOT NULL
        """,
        (invoice_id, PENDING_USER),
    )
    return cur.rowcount == 1


async def list_pending(
    conn: AsyncConnection[Any],
    profile_id: str,
    *,
    limit: int,
    remind_days: int,
    prepared_batch_state: str,
) -> tuple[list[PendingRow], int]:
    """Return the profile's invoices that wait on it, oldest first, and how many match.

    A row waits on the sender when it is ``PENDING_USER``, ``WAITING_FOR_USER`` or
    ``MANUAL_REQUIRED``, or when it is ``BATCHED`` on a ``prepared_batch_state`` batch
    that has sat at least ``remind_days`` days (S4.8, S11.4). ``limit`` caps the rows
    returned; the count covers every match. The database clock measures a batch's age,
    and ``updated_at`` stamps it because nothing updates a ``BATCHED`` invoice until it
    leaves that state. Does not commit.
    """
    cur = await conn.execute(
        """
        SELECT i.id,
               i.state,
               i.waiting_state,
               i.waiting_rule_id,
               i.razon_social,
               i.total,
               i.updated_at,
               count(*) OVER () AS total_count
        FROM invoices AS i
        LEFT JOIN batches AS b ON b.id = i.batch_id
        WHERE i.profile_id = %s
          AND (
                i.state = ANY(%s)
                OR (
                    i.state = %s
                    AND b.state = %s
                    AND i.updated_at <= now() - make_interval(days => %s)
                )
              )
        ORDER BY i.updated_at ASC, i.id ASC
        LIMIT %s
        """,
        (
            profile_id,
            [PENDING_USER, MANUAL_REQUIRED, *WAITING_FOR_USER],
            BATCHED,
            prepared_batch_state,
            remind_days,
            limit,
        ),
    )
    raw = await cur.fetchall()
    rows = [
        PendingRow(
            id=int(row[0]),
            state=str(row[1]),
            waiting_state=None if row[2] is None else str(row[2]),
            waiting_rule_id=None if row[3] is None else str(row[3]),
            razon_social=None if row[4] is None else str(row[4]),
            total=cast(Decimal | None, row[5]),
            batch_prepared_at=cast(datetime, row[6]) if str(row[1]) == BATCHED else None,
        )
        for row in raw
    ]
    return rows, (int(raw[0][7]) if raw else 0)


async def count_active_jobs(conn: AsyncConnection[Any], profile_id: str) -> int:
    """Count the profile's jobs that are not parked at infinity (BR-S01)."""
    cur = await conn.execute(
        """
        SELECT count(*)
        FROM jobs AS j
        JOIN invoices AS i ON i.id = j.invoice_id
        WHERE i.profile_id = %s
          AND j.run_after <> 'infinity'::timestamptz
        """,
        (profile_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("SELECT count(*) yielded no row")
    return int(row[0])


async def find_override_target(
    conn: AsyncConnection[Any],
    profile_id: str,
    reply_to: tuple[ChannelName, str] | None,
) -> int | None:
    """Return the invoice a chat override applies to, or None (D90).

    The quoted message's invoice wins when it belongs to the same profile; otherwise the
    profile's most recent invoice that is not ``CANCELLED``/``FAILED``/``DONE`` and whose
    batch is not ``sent`` (a finished invoice is only reached by quoting it). A quote of a
    message that is not stored (the bot's own summary) falls back the same way. Does not
    commit.
    """
    if reply_to is not None:
        channel, external_id = reply_to
        cur = await conn.execute(
            """
            SELECT i.id
            FROM messages AS m
            JOIN invoices AS i ON i.id = m.invoice_id
            WHERE m.channel = %s
              AND m.external_id = %s
              AND i.profile_id = %s
            """,
            (channel, external_id, profile_id),
        )
        row = await cur.fetchone()
        if row is not None:
            return int(row[0])
    cur = await conn.execute(
        """
        SELECT i.id
        FROM invoices AS i
        LEFT JOIN batches AS b ON b.id = i.batch_id
        WHERE i.profile_id = %s
          AND i.state NOT IN ('CANCELLED', 'FAILED', 'DONE')
          AND (b.state IS NULL OR b.state <> 'sent')
        ORDER BY i.created_at DESC, i.id DESC
        LIMIT 1
        """,
        (profile_id,),
    )
    row = await cur.fetchone()
    return None if row is None else int(row[0])


async def add_file(conn: AsyncConnection[Any], invoice_id: int, record: FileRecord) -> int:
    """Store one file of an invoice and return its id (R-1.4)."""
    cur = await conn.execute(
        """
        INSERT INTO files (invoice_id, kind, local_path, remote_ref, sha256, size)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            invoice_id,
            record.kind,
            record.local_path,
            record.remote_ref,
            record.sha256,
            record.size,
        ),
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("INSERT INTO files RETURNING id yielded no row")
    return int(row[0])


async def has_file(conn: AsyncConnection[Any], invoice_id: int) -> bool:
    """Return True when the invoice already has at least one stored file (R-1.4)."""
    cur = await conn.execute(
        "SELECT EXISTS (SELECT 1 FROM files WHERE invoice_id = %s)",
        (invoice_id,),
    )
    row = await cur.fetchone()
    return row is not None and bool(row[0])


async def touch_window(
    conn: AsyncConnection[Any], channel: ChannelName, profile_id: str, at: datetime
) -> None:
    """Record an inbound message at ``at``, keeping the greatest time (R-1.10)."""
    await conn.execute(
        """
        INSERT INTO channel_windows (channel, recipient, last_inbound_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (channel, recipient) DO UPDATE
        SET last_inbound_at = GREATEST(
            channel_windows.last_inbound_at, EXCLUDED.last_inbound_at
        )
        """,
        (channel, profile_id, at),
    )


async def window_open(conn: AsyncConnection[Any], channel: ChannelName, profile_id: str) -> bool:
    """Return True when the recipient's last inbound is inside 24 hours (R-1.10)."""
    cur = await conn.execute(
        """
        SELECT last_inbound_at > now() - make_interval(hours => %s)
        FROM channel_windows
        WHERE channel = %s AND recipient = %s
        """,
        (WINDOW_HOURS, channel, profile_id),
    )
    row = await cur.fetchone()
    return row is not None and bool(row[0])


async def save_reading(conn: AsyncConnection[Any], invoice_id: int, reading: Reading) -> None:
    """Write a reading's typed columns and keep the whole reading in fields_json (R-2.6).

    Amounts go through :meth:`Reading.amount`, so an amount that is not a finite number is
    stored as NULL and stays visible in ``fields_json``. A ``fecha_emision`` that is not an
    ISO date is likewise NULL in the typed column (D80): the doubtful value is carried by
    the reading, not rejected here. The database clock stamps ``updated_at``, so an edit
    that answers BR-E01 again restarts the park clock (BR-S08).
    """
    await conn.execute(
        """
        UPDATE invoices
        SET ruc_emisor = %s,
            tipo = %s,
            serie = %s,
            numero = %s,
            fecha = %s,
            igv = %s,
            total = %s,
            ruc_adq = %s,
            fields_json = %s,
            updated_at = now()
        WHERE id = %s
        """,
        (
            _ruc_emisor(reading),
            reading.value("tipo"),
            reading.value("serie"),
            reading.value("numero"),
            reading.fecha(),
            reading.amount("igv"),
            reading.amount("total"),
            reading.value("num_doc_adq"),
            Jsonb(reading_to_json(reading)),
            invoice_id,
        ),
    )


async def load_reading(conn: AsyncConnection[Any], invoice_id: int) -> Reading | None:
    """Return the reading stored for invoice_id, or None when it has none (R-2.6)."""
    cur = await conn.execute("SELECT fields_json FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    if row is None or row[0] is None:
        return None
    return reading_from_json(row[0])


async def get_file_path(conn: AsyncConnection[Any], invoice_id: int, kind: FileKind) -> str | None:
    """Return the local path of the invoice's first file of ``kind``, or None."""
    cur = await conn.execute(
        """
        SELECT local_path
        FROM files
        WHERE invoice_id = %s AND kind = %s
        ORDER BY id
        LIMIT 1
        """,
        (invoice_id, kind),
    )
    row = await cur.fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _ruc_emisor(reading: Reading) -> int | None:
    """Return the issuer RUC as the BIGINT column stores it, or None when missing."""
    value = reading.value("ruc_emisor")
    return None if value is None else int(value)
