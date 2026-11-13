"""Invoice, file and channel-window persistence for the intake pipeline.

Every function takes the caller's connection and never commits: the caller
owns the transaction (R-1.1, R-1.4, R-1.10).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from invoicebot.channels.base import AttachmentKind, ChannelName
from invoicebot.extract.fields import Reading, reading_from_json, reading_to_json

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


@dataclass(frozen=True)
class InvoiceHead:
    """The invoice columns the pipeline handlers act on (R-1.1)."""

    state: str
    profile_id: str
    origin_channel: ChannelName


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


async def set_flow(conn: AsyncConnection[Any], invoice_id: int, flow: str) -> None:
    """Store the flow the invoice was routed to (R-3.1)."""
    await conn.execute("UPDATE invoices SET flow_id = %s WHERE id = %s", (flow, invoice_id))


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
    the reading, not rejected here.
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
            fields_json = %s
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


async def get_file_path(conn: AsyncConnection[Any], invoice_id: int, kind: AttachmentKind) -> str | None:
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
