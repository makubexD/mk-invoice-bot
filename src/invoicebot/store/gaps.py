"""Gap persistence: the fingerprint upsert, its raise state and the alert queries (R-11.2, D115).

Every function takes the caller's connection and never commits: the caller
owns the transaction.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from psycopg import AsyncConnection

from invoicebot.gaps.kinds import GapKind


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
        raised_now=raised_now,
    )


async def upsert_gap(
    conn: AsyncConnection[Any],
    kind: GapKind,
    fingerprint: str,
    subject: str,
    invoice_id: int | None,
) -> GapRow:
    """Count one occurrence of the gap, link the invoice and report a first raise (R-11.2, D115).

    A kind with a window restarts at count 1 when ``last_seen`` is older than the
    window. ``raised_at`` is set the first time the new count reaches the kind's
    threshold; ``raised_now`` tells the caller that this call set it. The row and
    its ``invoice_gaps`` link are written in one statement each, and the caller
    owns the transaction.
    """
    cur = await conn.execute(
        """
        WITH previous AS (
            SELECT fingerprint, raised_at FROM gaps WHERE fingerprint = %(fingerprint)s
        ),
        upserted AS (
            INSERT INTO gaps AS g
                (fingerprint, kind, subject, state, count, first_seen, last_seen, raised_at)
            VALUES (%(fingerprint)s, %(kind)s, %(subject)s, 'open', 1, now(), now(),
                    CASE WHEN 1 >= %(threshold)s::int THEN now() END)
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
                raised_at = CASE
                    WHEN g.raised_at IS NULL
                     AND (CASE
                              WHEN %(window_days)s::int IS NOT NULL
                               AND g.last_seen < now() - make_interval(days => %(window_days)s::int)
                              THEN 1
                              ELSE g.count + 1
                          END) >= %(threshold)s::int
                    THEN now()
                    ELSE g.raised_at
                END
            RETURNING g.fingerprint, g.kind, g.subject, g.state, g.count,
                      g.raised_at, g.last_alert_at, g.issue_url, g.hint_url
        )
        SELECT u.fingerprint, u.kind, u.subject, u.state, u.count,
               u.raised_at, u.last_alert_at, u.issue_url, u.hint_url,
               (u.raised_at IS NOT NULL AND p.raised_at IS NULL) AS raised_now
        FROM upserted AS u
        LEFT JOIN previous AS p ON p.fingerprint = u.fingerprint
        """,
        {
            "fingerprint": fingerprint,
            "kind": kind.kind,
            "subject": subject,
            "threshold": kind.threshold,
            "window_days": kind.window_days,
        },
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("INSERT INTO gaps RETURNING yielded no row")

    if invoice_id is not None:
        await conn.execute(
            """
            INSERT INTO invoice_gaps (invoice_id, fingerprint)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
            """,
            (invoice_id, fingerprint),
        )
    return _gap_row(row, raised_now=bool(row[9]))


async def mark_reported(conn: AsyncConnection[Any], fingerprint: str, issue_url: str) -> None:
    """Store the issue URL and move the gap to reported (D116); does not commit."""
    await conn.execute(
        "UPDATE gaps SET state = 'reported', issue_url = %s WHERE fingerprint = %s",
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


async def get_gap(conn: AsyncConnection[Any], fingerprint: str) -> GapRow | None:
    """Return the gap, or None when its fingerprint is unknown; does not commit."""
    cur = await conn.execute(
        """
        SELECT fingerprint, kind, subject, state, count,
               raised_at, last_alert_at, issue_url, hint_url
        FROM gaps
        WHERE fingerprint = %s
        """,
        (fingerprint,),
    )
    row = await cur.fetchone()
    return None if row is None else _gap_row(row)


async def linked_gap(conn: AsyncConnection[Any], invoice_id: int, kind: str) -> GapRow | None:
    """Return the gap of kind linked to the invoice, if any (R-11.9); does not commit."""
    cur = await conn.execute(
        """
        SELECT g.fingerprint, g.kind, g.subject, g.state, g.count,
               g.raised_at, g.last_alert_at, g.issue_url, g.hint_url
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
               raised_at, last_alert_at, issue_url, hint_url
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
               raised_at, last_alert_at, issue_url, hint_url
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
