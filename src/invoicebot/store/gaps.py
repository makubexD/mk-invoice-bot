"""Gap persistence: the fingerprint upsert, its raise state and the alert queries (R-11.2, D115).

Every function takes the caller's connection and never commits: the caller
owns the transaction.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, cast

from psycopg import AsyncConnection

from invoicebot.gaps.kinds import GAP_KINDS, GapKind
from invoicebot.states import MANUAL_REQUIRED

#: The kinds whose config a reevaluation can fix (D157).
FIXABLE_KINDS: Final[tuple[str, ...]] = (
    GAP_KINDS["PORTAL_MISSING"].kind,
    GAP_KINDS["PORTAL_BROKEN"].kind,
    GAP_KINDS["TEMPLATE_INVALID"].kind,
)


@dataclass(frozen=True)
class GapRow:
    """A gap as the pipeline and the alert tasks see it (R-11.2, D115)."""

    fingerprint: str
    kind: str
    subject: str
    state: str
    count: int
    raised_at: datetime | None
    last_alert_at: datetime | None
    issue_url: str | None
    hint_url: str | None
    raised_with: str | None
    raised_now: bool


def _gap_row(row: Sequence[Any], *, raised_now: bool = False) -> GapRow:
    """Map one ``gaps`` row in the column order the queries select."""
    return GapRow(
        fingerprint=str(row[0]),
        kind=str(row[1]),
        subject=str(row[2]),
        state=str(row[3]),
        count=int(row[4]),
        raised_at=cast(datetime | None, row[5]),
        last_alert_at=cast(datetime | None, row[6]),
        issue_url=cast(str | None, row[7]),
        hint_url=cast(str | None, row[8]),
        raised_with=cast(str | None, row[9]),
        raised_now=raised_now,
    )


async def upsert_gap(
    conn: AsyncConnection[Any],
    kind: GapKind,
    fingerprint: str,
    subject: str,
    invoice_id: int | None,
    *,
    raised_with: str | None = None,
) -> GapRow:
    """Count one occurrence of the gap, link the invoice and report a first raise (R-11.2, D115).

    A kind with a window restarts at count 1 when ``last_seen`` is older than the
    window. The upsert never sets ``raised_at``: a separate conditional update
    claims the raise once the count reaches the kind's threshold. Under READ
    COMMITTED two writers racing on the same new fingerprint serialize on the
    row lock, and only the one that re-checks ``raised_at IS NULL`` after the
    other commits sees the row, so ``raised_now`` is true for exactly one of
    them. A sighting of a ``fixed`` or ``closed`` gap reopens it: ``reported`` when it has an
    issue, else ``open``, keeping ``raised_at`` so it is not raised again
    (D157). ``raised_with`` is the digest of the config entry this sighting
    failed on, written on every sighting. The row and its ``invoice_gaps`` link
    are written in one statement each, and the caller owns the transaction.
    """
    cur = await conn.execute(
        """
        INSERT INTO gaps AS g
            (fingerprint, kind, subject, state, count, first_seen, last_seen, raised_with)
        VALUES (%(fingerprint)s, %(kind)s, %(subject)s, 'open', 1, now(), now(), %(raised_with)s)
        ON CONFLICT (fingerprint) DO UPDATE
        SET count = CASE
                WHEN %(window_days)s::int IS NOT NULL
                 AND g.last_seen < now() - make_interval(days => %(window_days)s::int)
                THEN 1
                ELSE g.count + 1
            END,
            first_seen = CASE
                WHEN %(window_days)s::int IS NOT NULL
                 AND g.last_seen < now() - make_interval(days => %(window_days)s::int)
                THEN now()
                ELSE g.first_seen
            END,
            last_seen = now(),
            raised_with = %(raised_with)s,
            state = CASE
                WHEN g.state IN ('fixed', 'closed') AND g.issue_url IS NOT NULL THEN 'reported'
                WHEN g.state IN ('fixed', 'closed') THEN 'open'
                ELSE g.state
            END
        RETURNING g.fingerprint, g.kind, g.subject, g.state, g.count,
                  g.raised_at, g.last_alert_at, g.issue_url, g.hint_url, g.raised_with
        """,
        {
            "fingerprint": fingerprint,
            "kind": kind.kind,
            "subject": subject,
            "window_days": kind.window_days,
            "raised_with": raised_with,
        },
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("INSERT INTO gaps RETURNING yielded no row")

    cur = await conn.execute(
        """
        UPDATE gaps
        SET raised_at = now()
        WHERE fingerprint = %s AND raised_at IS NULL AND count >= %s
        RETURNING fingerprint, kind, subject, state, count,
                  raised_at, last_alert_at, issue_url, hint_url, raised_with
        """,
        (fingerprint, kind.threshold),
    )
    raised = await cur.fetchone()
    if raised is not None:
        row = raised

    if invoice_id is not None:
        await conn.execute(
            """
            INSERT INTO invoice_gaps (invoice_id, fingerprint)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
            """,
            (invoice_id, fingerprint),
        )
    return _gap_row(row, raised_now=raised is not None)


async def mark_reported(conn: AsyncConnection[Any], fingerprint: str, issue_url: str) -> None:
    """Store the issue URL and move the gap to reported unless it is ignored (D116, D139); does not commit."""
    await conn.execute(
        """
        UPDATE gaps
        SET state = CASE WHEN state = 'ignored' THEN state ELSE 'reported' END,
            issue_url = %s
        WHERE fingerprint = %s
        """,
        (issue_url, fingerprint),
    )


async def mark_alerted(conn: AsyncConnection[Any], fingerprints: Sequence[str]) -> None:
    """Stamp ``last_alert_at`` with the database clock for the given gaps (R-11.8); does not commit."""
    await conn.execute(
        "UPDATE gaps SET last_alert_at = now() WHERE fingerprint = ANY(%s)",
        (list(fingerprints),),
    )


async def set_hint(conn: AsyncConnection[Any], fingerprint: str, url: str) -> None:
    """Store the approved portal link as the gap's hint (D118); does not commit."""
    await conn.execute("UPDATE gaps SET hint_url = %s WHERE fingerprint = %s", (url, fingerprint))


async def ignore_gap(conn: AsyncConnection[Any], fingerprint: str) -> bool:
    """Move a not-yet-ignored gap to ignored, reporting whether a row changed (S8.9, D139).

    Does not commit.
    """
    cur = await conn.execute(
        "UPDATE gaps SET state = 'ignored' WHERE fingerprint = %s AND state <> 'ignored'",
        (fingerprint,),
    )
    return cur.rowcount == 1


async def get_gap(conn: AsyncConnection[Any], fingerprint: str) -> GapRow | None:
    """Return the gap, or None when its fingerprint is unknown; does not commit."""
    cur = await conn.execute(
        """
        SELECT fingerprint, kind, subject, state, count,
               raised_at, last_alert_at, issue_url, hint_url, raised_with
        FROM gaps
        WHERE fingerprint = %s
        """,
        (fingerprint,),
    )
    row = await cur.fetchone()
    return None if row is None else _gap_row(row)


async def open_gaps(conn: AsyncConnection[Any], limit: int) -> list[GapRow]:
    """Return the open or reported gaps, most recently seen first (S8.9, D139); does not commit."""
    cur = await conn.execute(
        """
        SELECT fingerprint, kind, subject, state, count,
               raised_at, last_alert_at, issue_url, hint_url, raised_with
        FROM gaps
        WHERE state IN ('open', 'reported')
        ORDER BY last_seen DESC
        LIMIT %s
        """,
        (limit,),
    )
    return [_gap_row(row) for row in await cur.fetchall()]


async def linked_gap(conn: AsyncConnection[Any], invoice_id: int, kind: str) -> GapRow | None:
    """Return the gap of kind linked to the invoice, if any (R-11.9); does not commit."""
    cur = await conn.execute(
        """
        SELECT g.fingerprint, g.kind, g.subject, g.state, g.count,
               g.raised_at, g.last_alert_at, g.issue_url, g.hint_url, g.raised_with
        FROM gaps AS g
        JOIN invoice_gaps AS ig ON ig.fingerprint = g.fingerprint
        WHERE ig.invoice_id = %s AND g.kind = %s
        ORDER BY g.fingerprint
        LIMIT 1
        """,
        (invoice_id, kind),
    )
    row = await cur.fetchone()
    return None if row is None else _gap_row(row)


async def alert_due(conn: AsyncConnection[Any], realert_days: int, kinds: Sequence[str]) -> list[GapRow]:
    """Return raised, non-ignored gaps of these kinds not alerted within ``realert_days`` (R-11.8).

    A gap is due when it was never alerted or its last alert is older than the
    window. Does not commit.
    """
    cur = await conn.execute(
        """
        SELECT fingerprint, kind, subject, state, count,
               raised_at, last_alert_at, issue_url, hint_url, raised_with
        FROM gaps
        WHERE raised_at IS NOT NULL
          AND state <> 'ignored'
          AND kind = ANY(%s)
          AND (last_alert_at IS NULL OR last_alert_at < now() - make_interval(days => %s))
        ORDER BY raised_at, fingerprint
        """,
        (list(kinds), realert_days),
    )
    return [_gap_row(row) for row in await cur.fetchall()]


async def unreported_overdue(conn: AsyncConnection[Any]) -> list[GapRow]:
    """Return raised, non-ignored gaps with no issue and no alert more than 24 h after the raise (R-11.11).

    Does not commit.
    """
    cur = await conn.execute(
        """
        SELECT fingerprint, kind, subject, state, count,
               raised_at, last_alert_at, issue_url, hint_url, raised_with
        FROM gaps
        WHERE raised_at IS NOT NULL
          AND state <> 'ignored'
          AND raised_at < now() - interval '24 hours'
          AND issue_url IS NULL
          AND last_alert_at IS NULL
        ORDER BY raised_at, fingerprint
        """
    )
    return [_gap_row(row) for row in await cur.fetchall()]


async def fixable_gaps(conn: AsyncConnection[Any]) -> list[GapRow]:
    """Return the open or reported gaps of the three config-fixable kinds, locked (D157).

    Rows come in fingerprint order and are locked ``FOR UPDATE`` so the caller
    fixes them in one transaction without a concurrent reevaluation racing it.
    Does not commit.
    """
    cur = await conn.execute(
        """
        SELECT fingerprint, kind, subject, state, count,
               raised_at, last_alert_at, issue_url, hint_url, raised_with
        FROM gaps
        WHERE state IN ('open', 'reported') AND kind = ANY(%s)
        ORDER BY fingerprint
        FOR UPDATE
        """,
        (list(FIXABLE_KINDS),),
    )
    return [_gap_row(row) for row in await cur.fetchall()]


async def mark_fixed(conn: AsyncConnection[Any], fingerprint: str) -> None:
    """Move the gap to fixed (D157); does not commit."""
    await conn.execute("UPDATE gaps SET state = 'fixed' WHERE fingerprint = %s", (fingerprint,))


async def mark_closed(conn: AsyncConnection[Any], fingerprint: str) -> None:
    """Move the gap to closed (D157); does not commit."""
    await conn.execute("UPDATE gaps SET state = 'closed' WHERE fingerprint = %s", (fingerprint,))


async def stuck_invoices(conn: AsyncConnection[Any], fingerprint: str, rule_id: str) -> list[int]:
    """Return the gap's invoices still waiting in ``MANUAL_REQUIRED`` under ``rule_id``, locked (R-11.9).

    ``rule_id`` is the rule the gap's failing step leaves the invoice under, so an
    invoice linked to this gap long ago but now waiting for another reason stays put.
    Locks each invoice row ``FOR UPDATE OF invoices`` so the caller moves them
    in the same transaction. Does not commit.
    """
    cur = await conn.execute(
        """
        SELECT invoices.id
        FROM invoice_gaps
        JOIN invoices ON invoices.id = invoice_gaps.invoice_id
        WHERE invoice_gaps.fingerprint = %s AND invoices.state = %s AND invoices.rule_id = %s
        ORDER BY invoices.id
        FOR UPDATE OF invoices
        """,
        (fingerprint, MANUAL_REQUIRED, rule_id),
    )
    return [int(row[0]) for row in await cur.fetchall()]
