"""Integration tests for reevaluating gaps and resuming their invoices (R-11.9, D157)."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.config.digest import config_digest
from invoicebot.config.loader import load_config
from invoicebot.config.models import Brand, Config, Portal, Store, StoreDirectory
from invoicebot.gaps.kinds import fingerprint
from invoicebot.pipeline.reevaluate import reevaluate
from invoicebot.pipeline.steps import DELIVER_STEP, FETCH_STEP
from invoicebot.rules.messages_es import MESSAGES
from invoicebot.states import DELIVERING, DONE, FETCHING_DOCS, MANUAL_REQUIRED
from invoicebot.store.invoices import create_invoice, set_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
# From tests/synthetic_rucs.txt; no real RUC may appear here.
ISSUER_RUC = "20000000001"
PORTAL_KEY = "synthetic_portal"
FLOW = "work_microsoft"
SINK = "excel_template"
ISSUE_URL = "https://github.com/owner/mk-invoice-bot-ops/issues/7"

PORTAL_MISSING_FP = fingerprint("PORTAL_MISSING", ISSUER_RUC)
PORTAL_BROKEN_FP = fingerprint("PORTAL_BROKEN", PORTAL_KEY)
TEMPLATE_INVALID_FP = fingerprint("TEMPLATE_INVALID", FLOW, SINK)
PARSER_MISS_FP = fingerprint("PARSER_MISS", ISSUER_RUC)
UNROUTED_FP = fingerprint("UNROUTED_BUYER", "buyer-a")


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _clean_reevaluate_state(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> AsyncIterator[None]:
    """Start each test from empty gaps, links and outbox rows."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM invoice_gaps")
        await conn.execute("DELETE FROM gaps")
        await conn.execute("DELETE FROM outbox")
    yield


def _config() -> Config:
    """Return the example config."""
    return load_config(EXAMPLE)


def _directory(*, portal_status: str = "active") -> StoreDirectory:
    """Return a store directory with the synthetic RUC's portal in ``portal_status``."""
    portal = Portal(key=PORTAL_KEY, adapter="generic_form", status=portal_status)
    store = Store(
        ruc=ISSUER_RUC,
        razon_social="Comercio Sintético",
        tipo="01",
        brands=[Brand(comercio="Tienda Sintética")],
        portal=PORTAL_KEY,
    )
    return StoreDirectory(stores={ISSUER_RUC: store}, portals={PORTAL_KEY: portal})


async def _manual_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    *,
    rule_id: str = "BR-F07",
) -> int:
    """Create one invoice in MANUAL_REQUIRED and return its id."""
    async with db_pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await set_state(conn, invoice_id, MANUAL_REQUIRED, rule_id=rule_id)
    return invoice_id


async def _done_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
) -> int:
    """Create one invoice in DONE and return its id."""
    async with db_pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await set_state(conn, invoice_id, DONE)
    return invoice_id


async def _seed_gap(
    conn: AsyncConnection[Any],
    kind: str,
    gap_fingerprint: str,
    *,
    state: str = "open",
    raised_with: str | None = None,
    issue_url: str | None = None,
) -> None:
    """Insert one gap row with the given state and optional digest and issue URL."""
    await conn.execute(
        """
        INSERT INTO gaps (fingerprint, kind, subject, state, raised_with, issue_url)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (gap_fingerprint, kind, gap_fingerprint, state, raised_with, issue_url),
    )


async def _link(conn: AsyncConnection[Any], invoice_id: int, gap_fingerprint: str) -> None:
    """Link one invoice to one gap."""
    await conn.execute(
        "INSERT INTO invoice_gaps (invoice_id, fingerprint) VALUES (%s, %s)",
        (invoice_id, gap_fingerprint),
    )


async def _gap_state(conn: AsyncConnection[Any], gap_fingerprint: str) -> str | None:
    cur = await conn.execute("SELECT state FROM gaps WHERE fingerprint = %s", (gap_fingerprint,))
    row = await cur.fetchone()
    return None if row is None else str(row[0])


async def _invoice_state(conn: AsyncConnection[Any], invoice_id: int) -> str | None:
    cur = await conn.execute("SELECT state FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    return None if row is None else str(row[0])


async def _job_count(conn: AsyncConnection[Any], invoice_id: int, step: str) -> int:
    cur = await conn.execute(
        "SELECT count(*) FROM jobs WHERE invoice_id = %s AND step = %s",
        (invoice_id, step),
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _sender_outbox(conn: AsyncConnection[Any], profile_id: str) -> list[tuple[str, str, str]]:
    """Return each sender row's rule id, text and channel, oldest first."""
    cur = await conn.execute(
        """
        SELECT payload_json->>'rule_id', payload_json->>'text', channel
        FROM outbox
        WHERE "to" = %s
        ORDER BY id
        """,
        (profile_id,),
    )
    return [(str(rule_id), str(text), str(channel)) for rule_id, text, channel in await cur.fetchall()]


async def _github_rows(conn: AsyncConnection[Any]) -> list[tuple[str, str | None]]:
    """Return each github row's op and issue_url, oldest first."""
    cur = await conn.execute(
        """
        SELECT payload_json->>'op', payload_json->>'issue_url'
        FROM outbox
        WHERE channel = 'github'
        ORDER BY id
        """,
    )
    return [(str(op), issue_url) for op, issue_url in await cur.fetchall()]


async def test_portal_missing_with_an_active_portal_fixes_and_resumes(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.9: a PORTAL_MISSING gap whose RUC now has an active portal fixes and resumes the invoice."""
    profile_id = f"reeval-{uuid4().hex}"
    invoice_id = await _manual_invoice(db_pool, profile_id)
    async with db_pool.connection() as conn, conn.transaction():
        await _seed_gap(conn, "PORTAL_MISSING", PORTAL_MISSING_FP)
        await _link(conn, invoice_id, PORTAL_MISSING_FP)

    await reevaluate(db_pool, _config(), _directory())

    async with db_pool.connection() as conn:
        assert await _gap_state(conn, PORTAL_MISSING_FP) == "fixed"
        assert await _invoice_state(conn, invoice_id) == FETCHING_DOCS
        assert await _job_count(conn, invoice_id, FETCH_STEP) == 1
        assert await _sender_outbox(conn, profile_id) == [
            ("BR-F12", MESSAGES["BR-F12"].format(id=invoice_id), "whatsapp")
        ]


async def test_portal_missing_with_a_todo_portal_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D157: a PORTAL_MISSING gap whose portal is still todo is not fixed."""
    profile_id = f"reeval-{uuid4().hex}"
    invoice_id = await _manual_invoice(db_pool, profile_id)
    async with db_pool.connection() as conn, conn.transaction():
        await _seed_gap(conn, "PORTAL_MISSING", PORTAL_MISSING_FP)
        await _link(conn, invoice_id, PORTAL_MISSING_FP)

    await reevaluate(db_pool, _config(), _directory(portal_status="todo"))

    async with db_pool.connection() as conn:
        assert await _gap_state(conn, PORTAL_MISSING_FP) == "open"
        assert await _invoice_state(conn, invoice_id) == MANUAL_REQUIRED
        assert await _job_count(conn, invoice_id, FETCH_STEP) == 0
        assert await _sender_outbox(conn, profile_id) == []


async def test_portal_broken_fixes_only_once_the_portal_changed(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D157: a PORTAL_BROKEN gap fixes only when the active portal's digest changed."""
    profile_id = f"reeval-{uuid4().hex}"
    invoice_id = await _manual_invoice(db_pool, profile_id)
    directory = _directory()
    digest = config_digest(directory.portals[PORTAL_KEY])
    async with db_pool.connection() as conn, conn.transaction():
        await _seed_gap(conn, "PORTAL_BROKEN", PORTAL_BROKEN_FP, raised_with=digest)
        await _link(conn, invoice_id, PORTAL_BROKEN_FP)

    await reevaluate(db_pool, _config(), directory)

    async with db_pool.connection() as conn:
        assert await _gap_state(conn, PORTAL_BROKEN_FP) == "open"
        assert await _invoice_state(conn, invoice_id) == MANUAL_REQUIRED
        assert await _sender_outbox(conn, profile_id) == []

    async with db_pool.connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE gaps SET raised_with = %s WHERE fingerprint = %s",
            ("changed-digest", PORTAL_BROKEN_FP),
        )

    await reevaluate(db_pool, _config(), directory)

    async with db_pool.connection() as conn:
        assert await _gap_state(conn, PORTAL_BROKEN_FP) == "fixed"
        assert await _invoice_state(conn, invoice_id) == FETCHING_DOCS
        assert await _job_count(conn, invoice_id, FETCH_STEP) == 1
        assert await _sender_outbox(conn, profile_id) == [
            ("BR-F12", MESSAGES["BR-F12"].format(id=invoice_id), "whatsapp")
        ]


async def test_template_invalid_with_a_changed_sink_resumes_delivery(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D157: a TEMPLATE_INVALID gap whose sink changed resumes the invoice to DELIVERING."""
    profile_id = f"reeval-{uuid4().hex}"
    invoice_id = await _manual_invoice(db_pool, profile_id, rule_id="BR-D02")
    async with db_pool.connection() as conn, conn.transaction():
        await _seed_gap(
            conn,
            "TEMPLATE_INVALID",
            TEMPLATE_INVALID_FP,
            raised_with="changed-digest",
        )
        await _link(conn, invoice_id, TEMPLATE_INVALID_FP)

    await reevaluate(db_pool, _config(), StoreDirectory(stores={}, portals={}))

    async with db_pool.connection() as conn:
        assert await _gap_state(conn, TEMPLATE_INVALID_FP) == "fixed"
        assert await _invoice_state(conn, invoice_id) == DELIVERING
        assert await _job_count(conn, invoice_id, DELIVER_STEP) == 1
        assert await _sender_outbox(conn, profile_id) == [
            ("BR-F12", MESSAGES["BR-F12"].format(id=invoice_id), "whatsapp")
        ]


async def test_a_fixed_gap_with_no_waiting_invoice_closes_at_once(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D157: a fixed gap whose only linked invoice is DONE closes and queues close_issue."""
    profile_id = f"reeval-{uuid4().hex}"
    invoice_id = await _done_invoice(db_pool, profile_id)
    async with db_pool.connection() as conn, conn.transaction():
        await _seed_gap(
            conn,
            "PORTAL_MISSING",
            PORTAL_MISSING_FP,
            state="reported",
            issue_url=ISSUE_URL,
        )
        await _link(conn, invoice_id, PORTAL_MISSING_FP)

    await reevaluate(db_pool, _config(), _directory())

    async with db_pool.connection() as conn:
        assert await _gap_state(conn, PORTAL_MISSING_FP) == "closed"
        assert await _github_rows(conn) == [("close_issue", ISSUE_URL)]


async def test_parser_miss_and_unrouted_buyer_are_untouched(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D157: PARSER_MISS and UNROUTED_BUYER gaps never auto-resume, so they stay open."""
    async with db_pool.connection() as conn, conn.transaction():
        await _seed_gap(conn, "PARSER_MISS", PARSER_MISS_FP)
        await _seed_gap(conn, "UNROUTED_BUYER", UNROUTED_FP)

    await reevaluate(db_pool, _config(), _directory())

    async with db_pool.connection() as conn:
        assert await _gap_state(conn, PARSER_MISS_FP) == "open"
        assert await _gap_state(conn, UNROUTED_FP) == "open"


async def test_an_ignored_gap_is_untouched(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D157: an ignored gap is not fixed and its invoice is not resumed."""
    profile_id = f"reeval-{uuid4().hex}"
    invoice_id = await _manual_invoice(db_pool, profile_id)
    async with db_pool.connection() as conn, conn.transaction():
        await _seed_gap(conn, "PORTAL_MISSING", PORTAL_MISSING_FP, state="ignored")
        await _link(conn, invoice_id, PORTAL_MISSING_FP)

    await reevaluate(db_pool, _config(), _directory())

    async with db_pool.connection() as conn:
        assert await _gap_state(conn, PORTAL_MISSING_FP) == "ignored"
        assert await _invoice_state(conn, invoice_id) == MANUAL_REQUIRED
        assert await _job_count(conn, invoice_id, FETCH_STEP) == 0
        assert await _sender_outbox(conn, profile_id) == []
