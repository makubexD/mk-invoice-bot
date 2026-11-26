"""The portal probe's state per issuer: queued, then done with its report (D193).

One row per RUC. A row belongs to one taught-link version; a newer version replaces
it, and a result for an older version is dropped. Every function takes the caller's
connection and never commits: the caller owns the transaction.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

#: The upsert: a new version replaces any older row and starts it queued (D193).
START_SQL = """
    INSERT INTO portal_probes (ruc_emisor, link_version, invoice_id, state)
    VALUES (%s, %s, %s, 'queued')
    ON CONFLICT (ruc_emisor) DO UPDATE
    SET link_version = EXCLUDED.link_version, invoice_id = EXCLUDED.invoice_id, state = 'queued',
        tries = 0, outcome = NULL, report_json = NULL, updated_at = now()
"""

#: The reads: the issuer's row, and the still-queued row waiting on an invoice (D193).
GET_SQL = """
    SELECT ruc_emisor, link_version, invoice_id, state, tries, outcome, report_json
    FROM portal_probes WHERE ruc_emisor = %s
"""
GET_FOR_INVOICE_SQL = """
    SELECT ruc_emisor, link_version, invoice_id, state, tries, outcome, report_json
    FROM portal_probes WHERE invoice_id = %s AND state = 'queued'
"""

#: Counts one try, only for the version the caller holds (D193).
COUNT_TRY_SQL = """
    UPDATE portal_probes SET tries = tries + 1, updated_at = now()
    WHERE ruc_emisor = %s AND link_version = %s
    RETURNING tries
"""

#: Closes a queued probe of the same version; a done row or a moved version matches nothing (D193).
FINISH_SQL = """
    UPDATE portal_probes
    SET state = 'done', outcome = %s, report_json = %s, updated_at = now()
    WHERE ruc_emisor = %s AND link_version = %s AND state = 'queued'
"""


@dataclass(frozen=True)
class PortalProbe:
    """One row of ``portal_probes`` (D193)."""

    ruc_emisor: int
    link_version: int
    invoice_id: int | None
    state: str
    tries: int
    outcome: str | None
    report: dict[str, Any] | None


def _row(row: tuple[Any, ...]) -> PortalProbe:
    """Build a PortalProbe from a row selected by the read queries."""
    return PortalProbe(
        ruc_emisor=row[0],
        link_version=row[1],
        invoice_id=row[2],
        state=row[3],
        tries=row[4],
        outcome=row[5],
        report=row[6],
    )


async def start(
    conn: AsyncConnection[Any], ruc_emisor: int, link_version: int, invoice_id: int | None
) -> None:
    """Queue a probe for ``ruc_emisor``, replacing any earlier row (tries 0, no result) (D193).

    Does not commit.
    """
    await conn.execute(START_SQL, (ruc_emisor, link_version, invoice_id))


async def get(conn: AsyncConnection[Any], ruc_emisor: int) -> PortalProbe | None:
    """Return the probe row for ``ruc_emisor``, or None (D193).

    Does not commit.
    """
    cur = await conn.execute(GET_SQL, (ruc_emisor,))
    row = await cur.fetchone()
    return None if row is None else _row(row)


async def get_for_invoice(conn: AsyncConnection[Any], invoice_id: int) -> PortalProbe | None:
    """Return the still-queued probe row waiting on ``invoice_id``, or None (D193).

    Does not commit.
    """
    cur = await conn.execute(GET_FOR_INVOICE_SQL, (invoice_id,))
    row = await cur.fetchone()
    return None if row is None else _row(row)


async def count_try(conn: AsyncConnection[Any], ruc_emisor: int, link_version: int) -> int:
    """Count one try for this RUC and version and return the new total (D193).

    Returns 0 when no row matches that version (never started, or a newer version
    replaced it), so a caller treats it as "nothing to try". Does not commit.
    """
    cur = await conn.execute(COUNT_TRY_SQL, (ruc_emisor, link_version))
    row = await cur.fetchone()
    return 0 if row is None else int(row[0])


async def finish(
    conn: AsyncConnection[Any],
    ruc_emisor: int,
    link_version: int,
    outcome: str,
    report: Mapping[str, Any],
) -> bool:
    """Record the probe's outcome and report; False when the version moved or it is already done (D193).

    ``report`` is the probe report already dumped to JSON-ready data, so the store
    does not depend on the fetcher's models. Does not commit.
    """
    cur = await conn.execute(FINISH_SQL, (outcome, Jsonb(dict(report)), ruc_emisor, link_version))
    return cur.rowcount == 1
