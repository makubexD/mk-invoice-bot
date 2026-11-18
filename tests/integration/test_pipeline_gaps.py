"""Integration tests for raising gaps, their alerts and the issue callback (R-11.2, R-11.5, R-11.8)."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.config.loader import load_config
from invoicebot.config.models import Profile
from invoicebot.fetchers.base import FetchReason
from invoicebot.gaps.kinds import GAP_KINDS
from invoicebot.github.issue_text import IssueFields
from invoicebot.pipeline.gaps import (
    GapContext,
    GapSighting,
    daily_digest,
    issue_opened,
    raise_gap,
    writes_today,
)
from invoicebot.queue import outbox
from invoicebot.store.gaps import upsert_gap

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
#: From tests/synthetic_rucs.txt; no real RUC may appear here.
ISSUER_RUC = "20000000001"
ISSUER_RUC_INT = 20000000001
SUBJECT = f"RUC {ISSUER_RUC}"
GAP_FINGERPRINT = f"portal_missing:{ISSUER_RUC}"
PARSER_FINGERPRINT = f"parser_miss:{ISSUER_RUC}"
ISSUE_URL = "https://github.com/owner/mk-invoice-bot-ops/issues/7"
HINT_URL = "https://portal.example.pe/consulta"


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def clean_gap_tables(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> AsyncIterator[None]:
    """Start each test from empty gaps, links and outbox rows."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM invoice_gaps")
        await conn.execute("DELETE FROM gaps")
        await conn.execute("DELETE FROM outbox")
    yield


def _context(github_enabled: bool) -> GapContext:
    """Return a context with two admin profiles and one non-admin."""
    cfg = load_config(EXAMPLE)
    owner = cfg.profiles["yo"]
    profiles: dict[str, Profile] = {
        "yo": owner,
        "segundo": owner.model_copy(update={"name": "Segundo", "whatsapp": "51977777777"}),
        "asistente": cfg.profiles["asistente"],
    }
    github = cfg.gaps.github.model_copy(update={"enabled": github_enabled})
    return GapContext(profiles=profiles, gaps_cfg=cfg.gaps.model_copy(update={"github": github}))


def _fields(kind: str, kind_fingerprint: str) -> IssueFields:
    """Return the typed issue facts of a portal gap."""
    return IssueFields(
        kind=kind,
        fingerprint=kind_fingerprint,
        ruc_emisor=ISSUER_RUC_INT,
        razon_social="Tienda Example",
        tipo="01",
        reasons=(FetchReason.NO_PORTAL,),
        portal_key=None,
        hint_url=None,
    )


def _portal_sighting() -> GapSighting:
    """Return one PORTAL_MISSING sighting for the synthetic issuer."""
    return GapSighting(
        kind="PORTAL_MISSING",
        parts=(ISSUER_RUC,),
        subject=SUBJECT,
        invoice_id=None,
        fields=_fields("PORTAL_MISSING", GAP_FINGERPRINT),
    )


def _parser_sighting() -> GapSighting:
    """Return one PARSER_MISS sighting for the synthetic issuer."""
    return GapSighting(
        kind="PARSER_MISS",
        parts=(ISSUER_RUC,),
        subject=SUBJECT,
        invoice_id=None,
        fields=_fields("PARSER_MISS", PARSER_FINGERPRINT),
    )


async def _outbox_rows(conn: AsyncConnection[Any], channel: str) -> list[tuple[str, Any]]:
    """Return the recipient and payload text of a channel's outbox rows."""
    cur = await conn.execute(
        """SELECT "to", payload_json->>'text' FROM outbox WHERE channel = %s ORDER BY id""",
        (channel,),
    )
    return [(str(row[0]), row[1]) for row in await cur.fetchall()]


async def _rule_rows(conn: AsyncConnection[Any], rule_id: str) -> list[tuple[str, str]]:
    """Return the recipient and text of one rule's outbox rows."""
    cur = await conn.execute(
        """SELECT "to", payload_json->>'text' FROM outbox
           WHERE payload_json->>'rule_id' = %s ORDER BY "to" """,
        (rule_id,),
    )
    return [(str(row[0]), str(row[1])) for row in await cur.fetchall()]


async def _insert_raised_gap(
    conn: AsyncConnection[Any], kind: str, gap_fingerprint: str, subject: str
) -> None:
    """Insert an already-raised gap for the digest tests."""
    await conn.execute(
        """
        INSERT INTO gaps (fingerprint, kind, subject, state, raised_at)
        VALUES (%s, %s, %s, 'open', now())
        """,
        (gap_fingerprint, kind, subject),
    )


async def _github_ops(conn: AsyncConnection[Any]) -> list[str]:
    """Return the github outbox rows' ``op`` values, oldest first."""
    cur = await conn.execute("SELECT payload_json->>'op' FROM outbox WHERE channel = 'github' ORDER BY id")
    return [str(op) for (op,) in await cur.fetchall()]


async def _create_issue_payload(conn: AsyncConnection[Any]) -> dict[str, Any]:
    """Return the only github ``create_issue`` row's payload."""
    cur = await conn.execute(
        """
        SELECT payload_json FROM outbox
        WHERE channel = 'github' AND payload_json->>'op' = 'create_issue'
        """
    )
    row = await cur.fetchone()
    assert row is not None
    return dict(row[0])


async def test_raising_twice_queues_one_github_row(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.2: two raises of one fingerprint queue a single github create_issue row."""
    ctx = _context(github_enabled=True)
    async with db_pool.connection() as conn:
        await raise_gap(conn, ctx, _portal_sighting())
        second = await raise_gap(conn, ctx, _portal_sighting())
        cur = await conn.execute(
            "SELECT count(*) FROM outbox WHERE channel = 'github' AND payload_json->>'fingerprint' = %s",
            (GAP_FINGERPRINT,),
        )
        count_row = await cur.fetchone()

    assert second.count == 2
    assert count_row == (1,)


async def test_a_pending_comment_does_not_block_a_new_create_issue(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D118: a pending comment row for the fingerprint still lets a new sighting queue create_issue."""
    ctx = _context(github_enabled=True)
    async with db_pool.connection() as conn:
        await outbox.add(
            conn,
            "github",
            str(ctx.gaps_cfg.github.repo),
            {
                "op": "comment",
                "fingerprint": GAP_FINGERPRINT,
                "issue_url": ISSUE_URL,
                "hint_url": HINT_URL,
            },
        )
        await raise_gap(conn, ctx, _portal_sighting())
        ops = await _github_ops(conn)

    assert ops == ["comment", "create_issue"]


async def test_a_new_create_issue_carries_the_gaps_stored_hint(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D118: a gap that already has a hint_url puts it in the new create_issue fields."""
    ctx = _context(github_enabled=True)
    async with db_pool.connection() as conn:
        await _insert_raised_gap(conn, "PORTAL_MISSING", GAP_FINGERPRINT, SUBJECT)
        await conn.execute(
            "UPDATE gaps SET hint_url = %s WHERE fingerprint = %s",
            (HINT_URL, GAP_FINGERPRINT),
        )
        await raise_gap(conn, ctx, _portal_sighting())
        payload = await _create_issue_payload(conn)

    assert payload["fields"]["hint_url"] == HINT_URL


async def test_github_disabled_alerts_each_admin_once(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.8: GitHub disabled sends one GAP_admin per admin, and the second raise adds none."""
    ctx = _context(github_enabled=False)
    async with db_pool.connection() as conn:
        await raise_gap(conn, ctx, _portal_sighting())
        await raise_gap(conn, ctx, _portal_sighting())
        cur = await conn.execute(
            """SELECT "to" FROM outbox WHERE payload_json->>'rule_id' = 'GAP_admin' ORDER BY "to" """
        )
        recipients = [str(row[0]) for row in await cur.fetchall()]

    assert recipients == ["segundo", "yo"]


async def test_digest_only_parser_miss_queues_no_alert(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.8: a raised digest-only PARSER_MISS gap queues no owner alert."""
    ctx = _context(github_enabled=False)
    async with db_pool.connection() as conn:
        await raise_gap(conn, ctx, _parser_sighting())
        second = await raise_gap(conn, ctx, _parser_sighting())
        cur = await conn.execute("SELECT count(*) FROM outbox")
        count_row = await cur.fetchone()

    assert second.raised_now is True
    assert count_row == (0,)


async def test_daily_digest_lists_each_due_gap_once_per_admin(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.8: two PARSER_MISS and one UNROUTED_BUYER gap make one three-line digest per admin, once."""
    ctx = _context(github_enabled=False)
    async with db_pool.connection() as conn:
        await _insert_raised_gap(conn, "PARSER_MISS", "parser_miss:issuer-a", "Emisor A")
        await _insert_raised_gap(conn, "PARSER_MISS", "parser_miss:issuer-b", "Emisor B")
        await _insert_raised_gap(conn, "UNROUTED_BUYER", "unrouted:buyer-a", "Comprador A")

    await daily_digest(db_pool, ctx.profiles, ctx.gaps_cfg)
    async with db_pool.connection() as conn:
        first = await _rule_rows(conn, "GAP_DIGEST_admin")
    await daily_digest(db_pool, ctx.profiles, ctx.gaps_cfg)
    async with db_pool.connection() as conn:
        second = await _rule_rows(conn, "GAP_DIGEST_admin")

    assert [recipient for recipient, _ in first] == ["segundo", "yo"]
    assert all(text.count("•") == 3 for _, text in first)
    assert second == first


async def test_daily_digest_alerts_an_overdue_gap_without_a_link_once(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.11: a PORTAL_MISSING gap raised 25 h ago alerts each admin once, without a link."""
    ctx = _context(github_enabled=False)
    async with db_pool.connection() as conn:
        await _insert_raised_gap(conn, "PORTAL_MISSING", GAP_FINGERPRINT, SUBJECT)
        await conn.execute(
            "UPDATE gaps SET raised_at = now() - interval '25 hours' WHERE fingerprint = %s",
            (GAP_FINGERPRINT,),
        )

    await daily_digest(db_pool, ctx.profiles, ctx.gaps_cfg)
    async with db_pool.connection() as conn:
        first = await _rule_rows(conn, "GAP_admin")
        cur = await conn.execute(
            "SELECT last_alert_at IS NOT NULL FROM gaps WHERE fingerprint = %s",
            (GAP_FINGERPRINT,),
        )
        alerted = await cur.fetchone()
    await daily_digest(db_pool, ctx.profiles, ctx.gaps_cfg)
    async with db_pool.connection() as conn:
        second = await _rule_rows(conn, "GAP_admin")

    assert [recipient for recipient, _ in first] == ["segundo", "yo"]
    assert all("Issue:" not in text for _, text in first)
    assert alerted == (True,)
    assert second == first


async def test_issue_opened_reports_and_alerts_with_the_link(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.5: issue_opened stores the URL, sets reported and alerts with the link."""
    ctx = _context(github_enabled=True)
    async with db_pool.connection() as conn:
        await raise_gap(conn, ctx, _portal_sighting())

    await issue_opened(db_pool, ctx.profiles, ctx.gaps_cfg, GAP_FINGERPRINT, ISSUE_URL)

    async with db_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT state, issue_url FROM gaps WHERE fingerprint = %s",
            (GAP_FINGERPRINT,),
        )
        gap_row = await cur.fetchone()
        alerts = await _outbox_rows(conn, "whatsapp")

    assert gap_row == ("reported", ISSUE_URL)
    assert sorted(recipient for recipient, _ in alerts) == ["segundo", "yo"]
    assert all(ISSUE_URL in text for _, text in alerts)


async def test_issue_opened_on_an_ignored_gap_stores_the_url_without_alerting(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D139: an ignored gap with a queued issue keeps its state, stores the URL and alerts nobody."""
    ctx = _context(github_enabled=True)
    async with db_pool.connection() as conn:
        await raise_gap(conn, ctx, _portal_sighting())
        await conn.execute(
            "UPDATE gaps SET state = 'ignored' WHERE fingerprint = %s",
            (GAP_FINGERPRINT,),
        )

    await issue_opened(db_pool, ctx.profiles, ctx.gaps_cfg, GAP_FINGERPRINT, ISSUE_URL)

    async with db_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT state, issue_url FROM gaps WHERE fingerprint = %s",
            (GAP_FINGERPRINT,),
        )
        gap_row = await cur.fetchone()
        alerts = await _rule_rows(conn, "GAP_admin")

    assert gap_row == ("ignored", ISSUE_URL)
    assert alerts == []


async def test_a_realdert_after_the_window_alerts_again(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.8: a raise 8 days after the last alert alerts again when realert_days is 7."""
    ctx = _context(github_enabled=False)
    assert ctx.gaps_cfg.realert_days == 7
    async with db_pool.connection() as conn:
        await raise_gap(conn, ctx, _portal_sighting())
        await conn.execute(
            "UPDATE gaps SET last_alert_at = now() - interval '8 days' WHERE fingerprint = %s",
            (GAP_FINGERPRINT,),
        )
        await raise_gap(conn, ctx, _portal_sighting())
        cur = await conn.execute(
            """
            SELECT "to", count(*) FROM outbox
            WHERE payload_json->>'rule_id' = 'GAP_admin'
            GROUP BY "to"
            """
        )
        counts = {str(row[0]): int(row[1]) for row in await cur.fetchall()}

    assert counts == {"segundo": 2, "yo": 2}


async def test_a_realert_with_github_enabled_carries_the_issue_link(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.8: with an open issue, a raise 8 days after the last alert alerts again with its link."""
    ctx = _context(github_enabled=True)
    async with db_pool.connection() as conn:
        await raise_gap(conn, ctx, _portal_sighting())
    await issue_opened(db_pool, ctx.profiles, ctx.gaps_cfg, GAP_FINGERPRINT, ISSUE_URL)
    async with db_pool.connection() as conn:
        await conn.execute(
            "UPDATE gaps SET last_alert_at = now() - interval '8 days' WHERE fingerprint = %s",
            (GAP_FINGERPRINT,),
        )
        await raise_gap(conn, ctx, _portal_sighting())
        alerts = await _rule_rows(conn, "GAP_admin")
        github_rows = await _outbox_rows(conn, "github")

    assert [recipient for recipient, _ in alerts] == ["segundo", "segundo", "yo", "yo"]
    assert all(ISSUE_URL in text for _, text in alerts)
    assert len(github_rows) == 1


async def test_a_repeated_issue_callback_alerts_once(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.5: issue_opened called again with the stored URL queues no second alert."""
    ctx = _context(github_enabled=True)
    async with db_pool.connection() as conn:
        await raise_gap(conn, ctx, _portal_sighting())

    await issue_opened(db_pool, ctx.profiles, ctx.gaps_cfg, GAP_FINGERPRINT, ISSUE_URL)
    await issue_opened(db_pool, ctx.profiles, ctx.gaps_cfg, GAP_FINGERPRINT, ISSUE_URL)

    async with db_pool.connection() as conn:
        alerts = await _rule_rows(conn, "GAP_admin")

    assert [recipient for recipient, _ in alerts] == ["segundo", "yo"]


async def test_ignored_gap_is_counted_but_queues_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.2: an ignored gap still counts the sighting and queues no alert."""
    ctx = _context(github_enabled=False)
    async with db_pool.connection() as conn:
        await upsert_gap(conn, GAP_KINDS["PORTAL_MISSING"], GAP_FINGERPRINT, SUBJECT, None)
        await conn.execute("UPDATE gaps SET state = 'ignored' WHERE fingerprint = %s", (GAP_FINGERPRINT,))
        gap = await raise_gap(conn, ctx, _portal_sighting())
        cur = await conn.execute("SELECT count(*) FROM outbox")
        count_row = await cur.fetchone()

    assert gap.state == "ignored"
    assert gap.count == 2
    assert count_row == (0,)


async def test_writes_today_counts_only_github_rows_since_local_midnight(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.10: writes_today counts sent github rows since local midnight, not older or other channels."""
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO outbox (channel, "to", payload_json, state, sent_at)
            VALUES ('github', 'owner/repo', '{}'::jsonb, 'sent', now()),
                   ('github', 'owner/repo', '{}'::jsonb, 'sent', now() - interval '2 days'),
                   ('whatsapp', 'yo', '{}'::jsonb, 'sent', now())
            """
        )

    assert await writes_today(db_pool, "America/Lima") == 1
