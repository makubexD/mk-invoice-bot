"""Raise a gap, alert the owner and report an opened issue (R-11.2, R-11.5, R-11.8, D116).

:func:`raise_gap` counts one sighting of a gap, then alerts once: with GitHub
enabled it queues a ``create_issue`` outbox row, and the issue callback
:func:`issue_opened` later stores the URL and alerts with it. With GitHub
disabled the owner alert goes out when the gap is raised. Kinds in
``gaps.digest_kinds`` never alert one by one. Every function takes the caller's connection or pool and
never opens a transaction of its own except :func:`issue_opened`, the outbox
transport's callback.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any

from psycopg import AsyncConnection

from invoicebot.config.models import Gaps, Profile
from invoicebot.gaps.kinds import GAP_KINDS, fingerprint
from invoicebot.github.issue_text import IssueFields
from invoicebot.pipeline.outgoing import queue_admin_alert
from invoicebot.queue import outbox
from invoicebot.rules.messages_es import GAP_DIGEST_LINE, KIND_DESC, MESSAGES
from invoicebot.store.db import Pool
from invoicebot.store.gaps import (
    GapRow,
    alert_due,
    get_gap,
    mark_alerted,
    mark_reported,
    unreported_overdue,
    upsert_gap,
)

#: The message key and rule id of one gap's owner alert (spec 06 §3).
RULE_GAP_ADMIN = "GAP_admin"

#: The message key and rule id of the daily digest of the digest kinds (spec 06 §3).
RULE_GAP_DIGEST = "GAP_DIGEST_admin"


@dataclass(frozen=True)
class GapContext:
    """Who receives a gap alert and how the gaps feature is configured."""

    profiles: Mapping[str, Profile]
    gaps_cfg: Gaps


@dataclass(frozen=True)
class GapSighting:
    """One occurrence of a gap: its identity, subject and typed issue facts."""

    kind: str
    parts: tuple[str, ...]
    subject: str
    invoice_id: int | None
    fields: IssueFields
    raised_with: str | None = None


async def raise_gap(
    conn: AsyncConnection[Any],
    ctx: GapContext,
    sighting: GapSighting,
) -> GapRow:
    """Count one sighting and queue its alert, in the caller's transaction (R-11.2, R-11.8).

    A first raise, or a re-alert due after ``gaps.realert_days``, queues the
    issue row (GitHub enabled) or the owner alert (GitHub disabled, non-digest
    kind). An ignored gap is still counted and linked but never alerted.
    """
    kind = GAP_KINDS[sighting.kind]
    gap_fingerprint = fingerprint(sighting.kind, *sighting.parts)
    gap = await upsert_gap(
        conn,
        kind,
        gap_fingerprint,
        sighting.subject,
        sighting.invoice_id,
        raised_with=sighting.raised_with,
    )
    if gap.state == "ignored":
        return gap
    if not gap.raised_now and not await _alert_is_due(conn, ctx, gap):
        return gap
    await _queue_alert(conn, ctx, sighting, gap, gap_fingerprint)
    return gap


async def issue_opened(
    pool: Pool,
    profiles: Mapping[str, Profile],
    gaps_cfg: Gaps,
    gap_fingerprint: str,
    issue_url: str,
) -> None:
    """Report the issue, store its URL and alert the owner with the link (R-11.5, D116).

    Called by the GitHub transport after it creates or reuses an issue. A kind
    in ``gaps.digest_kinds`` is reported but never alerted one by one. A repeated
    call for a URL already stored, as when the outbox row is retried after this
    commit, alerts nothing. An ignored gap gets its URL but no alert (D139).
    """
    async with pool.connection() as conn, conn.transaction():
        gap = await get_gap(conn, gap_fingerprint)
        await mark_reported(conn, gap_fingerprint, issue_url)
        if (
            gap is None
            or gap.state == "ignored"
            or gap.issue_url == issue_url
            or gap.kind in gaps_cfg.digest_kinds
        ):
            return
        await queue_admin_alert(
            conn,
            profiles,
            RULE_GAP_ADMIN,
            _gap_admin_text(gap.kind, gap.subject, issue_url),
        )
        await mark_alerted(conn, [gap_fingerprint])


async def daily_digest(pool: Pool, profiles: Mapping[str, Profile], gaps_cfg: Gaps) -> None:
    """Send the digest and the overdue-gap alerts in one transaction (R-11.8, R-11.11).

    The digest lists every due gap of ``gaps.digest_kinds``, then stamps their
    alert time. A non-digest gap raised over a day ago that has neither a link
    nor an alert is alerted on its own, without a link.
    """
    async with pool.connection() as conn, conn.transaction():
        await _queue_digest(conn, profiles, gaps_cfg)
        await _queue_overdue(conn, profiles, gaps_cfg)


async def _queue_digest(conn: AsyncConnection[Any], profiles: Mapping[str, Profile], gaps_cfg: Gaps) -> None:
    """Queue one digest per admin listing every due digest gap, then stamp them (R-11.8)."""
    due = await alert_due(conn, gaps_cfg.realert_days, gaps_cfg.digest_kinds)
    if not due:
        return
    lines = "\n".join(
        GAP_DIGEST_LINE.format(kind_desc=KIND_DESC[gap.kind], subject=gap.subject) for gap in due
    )
    text = MESSAGES[RULE_GAP_DIGEST].format(gap_lines=lines)
    await queue_admin_alert(conn, profiles, RULE_GAP_DIGEST, text)
    await mark_alerted(conn, [gap.fingerprint for gap in due])


async def _queue_overdue(conn: AsyncConnection[Any], profiles: Mapping[str, Profile], gaps_cfg: Gaps) -> None:
    """Queue an alert for each overdue non-digest gap that was never alerted (R-11.11)."""
    overdue = [gap for gap in await unreported_overdue(conn) if gap.kind not in gaps_cfg.digest_kinds]
    if not overdue:
        return
    for gap in overdue:
        await queue_admin_alert(conn, profiles, RULE_GAP_ADMIN, _gap_admin_text(gap.kind, gap.subject, None))
    await mark_alerted(conn, [gap.fingerprint for gap in overdue])


async def writes_today(pool: Pool, tz: str) -> int:
    """Return the GitHub outbox rows sent since local midnight on the database clock (R-11.10)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT count(*)
            FROM outbox
            WHERE channel = 'github' AND state = 'sent'
              AND sent_at >= date_trunc('day', now() AT TIME ZONE %(tz)s) AT TIME ZONE %(tz)s
            """,
            {"tz": tz},
        )
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError("SELECT count(*) FROM outbox yielded no row")
    return int(row[0])


async def _alert_is_due(conn: AsyncConnection[Any], ctx: GapContext, gap: GapRow) -> bool:
    """Return whether ``gap`` may be re-alerted now under ``gaps.realert_days`` (D116)."""
    due = await alert_due(conn, ctx.gaps_cfg.realert_days, [gap.kind])
    return any(candidate.fingerprint == gap.fingerprint for candidate in due)


async def _queue_alert(
    conn: AsyncConnection[Any],
    ctx: GapContext,
    sighting: GapSighting,
    gap: GapRow,
    gap_fingerprint: str,
) -> None:
    """Queue the issue row or the owner alert for one raised gap (R-11.5, R-11.8, D116).

    With GitHub enabled and no issue yet, only the issue row is queued: its
    callback alerts with the link. Once the issue exists, a due re-alert
    carries that link.
    """
    if ctx.gaps_cfg.github.enabled and gap.issue_url is None:
        if not await _issue_queued(conn, gap_fingerprint):
            fields = sighting.fields
            if fields.hint_url is None and gap.hint_url is not None:
                fields = replace(fields, hint_url=gap.hint_url)
            await outbox.add(
                conn,
                "github",
                ctx.gaps_cfg.github.repo,
                {
                    "op": "create_issue",
                    "fingerprint": gap_fingerprint,
                    "fields": _issue_payload(fields),
                },
            )
        return
    if sighting.kind in ctx.gaps_cfg.digest_kinds:
        return
    await queue_admin_alert(
        conn,
        ctx.profiles,
        RULE_GAP_ADMIN,
        _gap_admin_text(sighting.kind, sighting.subject, gap.issue_url),
    )
    await mark_alerted(conn, [gap_fingerprint])


async def _issue_queued(conn: AsyncConnection[Any], gap_fingerprint: str) -> bool:
    """Return whether a pending ``create_issue`` row already covers this fingerprint (D117)."""
    cur = await conn.execute(
        """
        SELECT 1
        FROM outbox
        WHERE channel = 'github' AND state = 'pending'
          AND payload_json->>'op' = 'create_issue'
          AND payload_json->>'fingerprint' = %s
        LIMIT 1
        """,
        (gap_fingerprint,),
    )
    return await cur.fetchone() is not None


def _gap_admin_text(kind: str, subject: str, issue_url: str | None) -> str:
    """Render ``GAP_admin`` with the kind's description and its issue link, if any (D116)."""
    issue_line = f" Issue: {issue_url}" if issue_url else ""
    return MESSAGES[RULE_GAP_ADMIN].format(
        kind_desc=KIND_DESC[kind],
        subject=subject,
        issue_line=issue_line,
    )


def _issue_payload(fields: IssueFields) -> dict[str, Any]:
    """Return the JSON payload the GitHub transport reads: typed fields and reason values (D117)."""
    payload = asdict(fields)
    payload["reasons"] = [reason.value for reason in fields.reasons]
    return payload
