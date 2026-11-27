"""Forget an invoice in the database: release its key or delete its rows (D196)."""

from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection, sql

from invoicebot.states import BATCHED, CANCELLED
from invoicebot.store.invoices import set_state

#: Tables whose ``invoice_id`` references ``invoices(id)``; purge deletes the invoice's rows from each.
#: A new referencing table is one line here (a guard test fails until it is added).
PURGED_TABLES = (
    "llm_usage",
    "files",
    "jobs",
    "fetch_attempts",
    "invoice_gaps",
    "upload_rejections",
    "pending_actions",
    "events",
    "messages",
    "outbox",
    "mail_jobs",
    "portal_candidates",
    # shortcut: the row goes; its captcha PNG (no invoice values, <= 64 KiB) stays in data_dir/captchas.
    "portal_captchas",
)
#: Referencing tables whose foreign key nulls itself (``ON DELETE SET NULL``): the rows stay.
NULLED_TABLES = ("portal_probes",)

_DELETE_ROWS = {
    table: sql.SQL("DELETE FROM {} WHERE invoice_id = %s").format(sql.Identifier(table))
    for table in PURGED_TABLES
}
_DELETE_INVOICE = "DELETE FROM invoices WHERE id = %s RETURNING batch_id"
_DELETE_BATCH = """
    DELETE FROM batches
    WHERE id = %s
      AND NOT EXISTS (SELECT 1 FROM invoices WHERE batch_id = %s)
"""
#: The rule that records a held mail's invoice cancelled by ``forget`` (D204).
RULE_HELD_FORGOTTEN = "BR-D23"
# The batch state is read without locking the batch: a race can only turn a held invoice into a
# false "not held", which refuses (D204).
_LOCK_INVOICE = """
    SELECT invoices.state, invoices.flow_id, invoices.serie, invoices.numero,
           invoices.flow_data_json ->> 'folder_path', invoices.payment_id IS NOT NULL,
           COALESCE(invoices.state = 'BATCHED' AND b.state = 'review', false)
    FROM invoices
    LEFT JOIN batches AS b ON b.id = invoices.batch_id
    WHERE invoices.id = %s
    FOR UPDATE OF invoices
"""
_LOCAL_PATHS = "SELECT local_path FROM files WHERE invoice_id = %s AND local_path IS NOT NULL ORDER BY id"
_RELEASE_KEY = "UPDATE invoices SET natural_key = NULL, updated_at = now() WHERE id = %s"


@dataclass(frozen=True)
class ForgetTarget:
    """What forgetting an invoice needs to know about it."""

    invoice_id: int
    state: str
    flow_id: str | None
    serie: str | None
    numero: str | None
    folder_path: str | None
    paid: bool
    held: bool
    local_paths: tuple[str, ...]


async def lock_target(conn: AsyncConnection[Any], invoice_id: int) -> ForgetTarget | None:
    """Lock the invoice row and return its target, or ``None`` when there is no such invoice.

    ``held`` is true for a ``BATCHED`` invoice whose batch is in ``review``: its mail waits
    for [Enviar] and nothing was sent (D204).
    """
    cur = await conn.execute(_LOCK_INVOICE, (invoice_id,))
    row = await cur.fetchone()
    if row is None:
        return None
    paths = await conn.execute(_LOCAL_PATHS, (invoice_id,))
    return ForgetTarget(
        invoice_id=invoice_id,
        state=row[0],
        flow_id=row[1],
        serie=row[2],
        numero=row[3],
        folder_path=row[4],
        paid=bool(row[5]),
        held=bool(row[6]),
        local_paths=tuple(str(p[0]) for p in await paths.fetchall()),
    )


async def release_key(conn: AsyncConnection[Any], invoice_id: int) -> None:
    """Clear the invoice's natural key so the same invoice can be sent again. Does not commit."""
    await conn.execute(_RELEASE_KEY, (invoice_id,))


async def cancel_held(conn: AsyncConnection[Any], invoice_id: int) -> None:
    """Set a held ``BATCHED`` invoice ``CANCELLED`` so a later [Enviar] answers BR-S02 (D204, BR-D23).

    BR-S02 is the stale-button answer: [Enviar] applies to ``BATCHED`` only. Does not commit.
    """
    await set_state(conn, invoice_id, CANCELLED, rule_id=RULE_HELD_FORGOTTEN, expected=BATCHED)


async def purge(conn: AsyncConnection[Any], invoice_id: int) -> dict[str, int]:
    """Delete the invoice's rows, the invoice and its batch when unused; return the count per table.

    Does not commit.
    """
    counts: dict[str, int] = {}
    for table in PURGED_TABLES:
        cur = await conn.execute(_DELETE_ROWS[table], (invoice_id,))
        counts[table] = cur.rowcount
    cur = await conn.execute(_DELETE_INVOICE, (invoice_id,))
    counts["invoices"] = cur.rowcount
    row = await cur.fetchone()
    batch_id = row[0] if row is not None else None
    counts["batches"] = 0
    if batch_id is not None:
        cur = await conn.execute(_DELETE_BATCH, (batch_id, batch_id))
        counts["batches"] = cur.rowcount
    return counts
