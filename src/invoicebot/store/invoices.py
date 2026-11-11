"""Invoice, file and channel-window persistence for the intake pipeline.

Every function takes the caller's connection and never commits: the caller
owns the transaction (R-1.1, R-1.4, R-1.10).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection

from invoicebot.channels.base import AttachmentKind, ChannelName

#: Length of the WhatsApp customer-service window, in hours (R-1.10).
WINDOW_HOURS = 24


@dataclass(frozen=True)
class FileRecord:
    """A file to attach to an invoice: where it came from and what it is (R-1.4)."""

    kind: AttachmentKind
    local_path: str | None
    remote_ref: str | None
    sha256: str
    size: int


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


async def set_state(
    conn: AsyncConnection[Any],
    invoice_id: int,
    state: str,
    *,
    rule_id: str | None = None,
    error: str | None = None,
) -> None:
    """Move invoice_id to state, record rule_id and error, and stamp updated_at."""
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
