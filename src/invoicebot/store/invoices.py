"""Invoice, file and channel-window persistence for the intake pipeline.

Every function takes the caller's connection and never commits: the caller
owns the transaction (R-1.1, R-1.4, R-1.10).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from invoicebot.channels.base import Attachment, AttachmentKind, ChannelName
from invoicebot.extract.fields import Reading, reading_from_json, reading_to_json
from invoicebot.rules.catalog import RULES
from invoicebot.states import (
    BATCHED,
    CANCELLED,
    EDITABLE,
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
    batch_prepared_at: date | None


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


async def start_fetch_round(conn: AsyncConnection[Any], invoice_id: int) -> None:
    """Start a new fetch round for invoice_id with the database clock (D166).

    The chain decision, the retry counts and BR-F07's list read only the attempts at
    or after ``fetch_round_at``; the full log stays for the per-portal stats. Does not
    commit.
    """
    await conn.execute(
        "UPDATE invoices SET fetch_round_at = now() WHERE id = %s",
        (invoice_id,),
    )


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


def pending_auth_key(provider: str, account: str) -> str:
    """Return the ``error`` value that names a parked invoice's account (BR-D01)."""
    return f"{provider}:{account}"


async def resume_pending_auth(conn: AsyncConnection[Any], provider: str, account: str) -> list[int]:
    """Move the ``PENDING_AUTH`` invoices parked for ``provider``'s ``account`` to ``DELIVERING`` (BR-D01).

    Only the invoices whose ``error`` is ``<provider>:<account>`` resume, so a sign-in never
    moves another account's parked invoice. The caller adds each invoice's ``deliver`` job in
    the same transaction, so a crash never leaves an invoice ``DELIVERING`` without its job.
    """
    cur = await conn.execute(
        """
        UPDATE invoices
        SET state = 'DELIVERING',
            rule_id = NULL,
            error = NULL,
            updated_at = now()
        WHERE state = 'PENDING_AUTH'
          AND error = %s
        RETURNING id
        """,
        (pending_auth_key(provider, account),),
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
    timezone: str,
) -> tuple[list[PendingRow], int]:
    """Return the profile's invoices that wait on it, oldest first, and how many match.

    A row waits on the sender when it is ``PENDING_USER``, ``WAITING_FOR_USER`` or
    ``MANUAL_REQUIRED``, or when it is ``BATCHED`` on a ``prepared_batch_state`` batch
    that has sat at least ``remind_days`` days (S4.8, S11.4). ``limit`` caps the rows
    returned; the count covers every match. ``timezone`` is the owner's zone: a batch's
    ``batch_prepared_at`` is its ``updated_at`` as a date there. The database clock
    measures a batch's age, and ``updated_at`` stamps it because nothing updates a
    ``BATCHED`` invoice until it leaves that state. Does not commit.
    """
    cur = await conn.execute(
        """
        SELECT i.id,
               i.state,
               i.waiting_state,
               i.waiting_rule_id,
               i.razon_social,
               i.total,
               (i.updated_at AT TIME ZONE %s)::date,
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
            timezone,
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
            batch_prepared_at=cast(date, row[6]) if str(row[1]) == BATCHED else None,
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


#: The flow-data key an [Editar] press sets on its invoice (R-3.5, D90).
EDITING_KEY = "editing"


async def mark_editing(conn: AsyncConnection[Any], profile_id: str, invoice_id: int) -> None:
    """Pin invoice_id as its profile's edit target, clearing the profile's other pins (R-3.5, D90).

    A ``campo=valor`` message without a quote reaches the pinned invoice even when a newer
    one is open. Does not commit.
    """
    await conn.execute(
        """
        UPDATE invoices
        SET flow_data_json = flow_data_json - %s
        WHERE profile_id = %s
          AND id <> %s
          AND flow_data_json ? %s
        """,
        (EDITING_KEY, profile_id, invoice_id, EDITING_KEY),
    )
    await conn.execute(
        """
        UPDATE invoices
        SET flow_data_json = flow_data_json || %s::jsonb
        WHERE id = %s
          AND profile_id = %s
        """,
        (Jsonb({EDITING_KEY: True}), invoice_id, profile_id),
    )


async def clear_editing(conn: AsyncConnection[Any], invoice_id: int) -> None:
    """Remove the edit pin from the invoice's flow data; does not commit (R-3.5, D90)."""
    await conn.execute(
        """
        UPDATE invoices
        SET flow_data_json = flow_data_json - %s
        WHERE id = %s
          AND flow_data_json ? %s
        """,
        (EDITING_KEY, invoice_id, EDITING_KEY),
    )


async def find_override_target(
    conn: AsyncConnection[Any],
    profile_id: str,
    reply_to: tuple[ChannelName, str] | None,
) -> int | None:
    """Return the invoice a chat override applies to, or None (D90).

    The quoted message's invoice wins when it belongs to the same profile; otherwise an
    invoice pinned by [Editar] comes first while it is still ``EDITABLE`` (R-3.5, D90),
    then the profile's most recent invoice that is not ``CANCELLED``/``FAILED``/``DONE``
    and whose batch is not ``sent`` (a finished invoice is only reached by quoting it). A
    quote of a message that is not stored (the bot's own summary) falls back the same
    way. Does not commit.
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
        ORDER BY (i.flow_data_json ? %s AND i.state = ANY(%s)) DESC, i.created_at DESC, i.id DESC
        LIMIT 1
        """,
        (profile_id, EDITING_KEY, sorted(EDITABLE)),
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


@dataclass(frozen=True)
class CpStatus:
    """The ``estadoCp`` SUNAT answered, with the issuer's RUC state (R-4.1)."""

    estado_cp: int
    estado_ruc: str | None
    cond_domi_ruc: str | None


async def set_sunat_status(
    conn: AsyncConnection[Any], invoice_id: int, status: CpStatus | None, rechecks: int
) -> None:
    """Store the SUNAT answer for invoice_id and count its rechecks (R-4.1).

    The three status fields are JSON null when SUNAT gave no answer, and the database
    clock stamps ``checked_at``. Does not commit.
    """
    if status is None:
        estado_cp, estado_ruc, cond_domi_ruc = None, None, None
    else:
        estado_cp, estado_ruc, cond_domi_ruc = status.estado_cp, status.estado_ruc, status.cond_domi_ruc
    await conn.execute(
        """
        UPDATE invoices
        SET sunat_status_json = jsonb_build_object(
                'estado_cp', %s::int,
                'estado_ruc', %s::text,
                'cond_domi_ruc', %s::text,
                'checked_at', now(),
                'rechecks', %s::int
            )
        WHERE id = %s
        """,
        (estado_cp, estado_ruc, cond_domi_ruc, rechecks, invoice_id),
    )


async def bump_rechecks(conn: AsyncConnection[Any], invoice_id: int, rechecks: int) -> None:
    """Count a SUNAT check that gave no answer, keeping the last answer (R-4.1, D125).

    Only the ``rechecks`` key of ``sunat_status_json`` changes, so the stored status and
    ``checked_at`` survive an outage. Does not commit.
    """
    await conn.execute(
        """
        UPDATE invoices
        SET sunat_status_json = jsonb_set(
                COALESCE(sunat_status_json, '{}'::jsonb),
                '{rechecks}',
                to_jsonb(%s::int)
            )
        WHERE id = %s
        """,
        (rechecks, invoice_id),
    )


async def sunat_rechecks(conn: AsyncConnection[Any], invoice_id: int) -> int:
    """Return how many SUNAT checks invoice_id recorded, or 0 when it has none (R-4.1)."""
    cur = await conn.execute(
        """
        SELECT COALESCE((sunat_status_json->>'rechecks')::int, 0)
        FROM invoices
        WHERE id = %s
        """,
        (invoice_id,),
    )
    row = await cur.fetchone()
    return 0 if row is None else int(row[0])


async def lock_sunat_rechecks(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str, int] | None:
    """Lock invoice_id's row and return its state and recheck count, or None when missing (D125).

    The caller compares the count it started with, so a revalidation or another recheck
    that committed first wins. Does not commit.
    """
    cur = await conn.execute(
        """
        SELECT state, COALESCE((sunat_status_json->>'rechecks')::int, 0)
        FROM invoices
        WHERE id = %s
        FOR UPDATE
        """,
        (invoice_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return str(row[0]), int(row[1])


async def record_upload_rejection(
    conn: AsyncConnection[Any], invoice_id: int, media_ref: str, rule_id: str
) -> bool:
    """Record that invoice_id's upload at media_ref was rejected under rule_id (D103).

    Returns whether this call added the row. A second run that records the same
    rejection gets ``False``, so its caller does not repeat the reply. Does not commit.
    """
    cur = await conn.execute(
        """
        INSERT INTO upload_rejections (invoice_id, media_ref, rule_id)
        VALUES (%s, %s, %s)
        ON CONFLICT (invoice_id, media_ref) DO NOTHING
        """,
        (invoice_id, media_ref, rule_id),
    )
    return cur.rowcount == 1


@dataclass(frozen=True)
class StoredUpload:
    """A linked upload and the channel of the message that carried it (R-1.5, D102).

    The channel picks the media source: an upload may reach an invoice that came in on
    another channel of the same profile (D102, D165).
    """

    channel: ChannelName
    attachment: Attachment


async def list_unattached_uploads(
    conn: AsyncConnection[Any], invoice_id: int, kinds: Sequence[str]
) -> list[StoredUpload]:
    """Return the invoice's linked uploads of ``kinds`` with no file or rejection row, oldest first.

    The attachments travel in the stored message payload; a kept upload records its media
    ref as ``files.remote_ref`` and a rejected one gets an ``upload_rejections`` row, so a
    redelivered job finds nothing left to do (R-1.5, D101, D103). Does not commit.
    """
    cur = await conn.execute(
        """
        SELECT m.channel,
               attachment->>'kind',
               attachment->>'media_ref',
               attachment->>'mime',
               attachment->>'size'
        FROM messages AS m
        CROSS JOIN LATERAL jsonb_array_elements(m.payload_json->'attachments')
             WITH ORDINALITY AS a(attachment, position)
        WHERE m.invoice_id = %s
          AND attachment->>'kind' = ANY(%s)
          AND NOT EXISTS (
              SELECT 1
              FROM files AS f
              WHERE f.invoice_id = m.invoice_id
                AND f.remote_ref = attachment->>'media_ref'
          )
          AND NOT EXISTS (
              SELECT 1
              FROM upload_rejections AS r
              WHERE r.invoice_id = m.invoice_id
                AND r.media_ref = attachment->>'media_ref'
          )
        ORDER BY m.received_at, m.external_id, a.position
        """,
        (invoice_id, list(kinds)),
    )
    return [
        StoredUpload(
            channel=cast(ChannelName, channel),
            attachment=Attachment(
                kind=cast(AttachmentKind, kind),
                media_ref=str(media_ref),
                mime=str(mime),
                size=int(size),
            ),
        )
        for channel, kind, media_ref, mime, size in await cur.fetchall()
    ]


async def get_file_path(conn: AsyncConnection[Any], invoice_id: int, kind: FileKind) -> str | None:
    """Return the local path of the invoice's newest file of ``kind``, or None."""
    cur = await conn.execute(
        """
        SELECT local_path
        FROM files
        WHERE invoice_id = %s AND kind = %s
        ORDER BY id DESC -- newest upload wins (D101)
        LIMIT 1
        """,
        (invoice_id, kind),
    )
    row = await cur.fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


async def photo_paths(conn: AsyncConnection[Any], invoice_id: int) -> list[Path]:
    """Return the local paths of the invoice's photos, oldest first; does not commit (R-9.7).

    Read-only: the caller still owns the files until it deletes them after a committed state
    change. :func:`invoicebot.store.flow_b.take_photo_paths` is the clearing counterpart.
    """
    cur = await conn.execute(
        """
        SELECT local_path
        FROM files
        WHERE invoice_id = %s AND kind = 'photo' AND local_path IS NOT NULL
        ORDER BY id
        """,
        (invoice_id,),
    )
    return [Path(str(row[0])) for row in await cur.fetchall()]


async def load_sunat_status(conn: AsyncConnection[Any], invoice_id: int) -> dict[str, object] | None:
    """Return the invoice's stored ``sunat_status_json`` object, or None (R-4.2)."""
    cur = await conn.execute("SELECT sunat_status_json FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    if row is None or not isinstance(row[0], dict):
        return None
    return cast(dict[str, object], row[0])


def _ruc_emisor(reading: Reading) -> int | None:
    """Return the issuer RUC as the BIGINT column stores it, or None when missing."""
    value = reading.value("ruc_emisor")
    return None if value is None else int(value)
