"""Integration tests for the fetch progress note (D206, BR-F17)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.pipeline.fetch_progress import make_fetch_progress_handler
from invoicebot.pipeline.steps import FETCH_PROGRESS_STEP
from invoicebot.pipeline.summary_post import SUMMARY_KEY
from invoicebot.queue.jobs import Job
from invoicebot.rules.messages_es import FETCH_PROGRESS
from invoicebot.store.flow_b import merge_flow_data
from invoicebot.store.invoices import create_invoice, set_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

RULE_ID = "BR-R02"


async def _invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]], profile_id: str, *, state: str, posted: bool
) -> int:
    """Create an invoice in ``state`` with saved summary inputs, posted or not."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await set_state(conn, invoice_id, state, rule_id=RULE_ID)
        summary: dict[str, Any] = {"rule_id": RULE_ID, "extra_lines": []}
        if posted:
            summary["posted"] = True
        await merge_flow_data(conn, invoice_id, {SUMMARY_KEY: summary})
    return invoice_id


async def _run(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int) -> None:
    """Run the handler once for ``invoice_id``."""
    handler = make_fetch_progress_handler(pool)
    await handler(
        Job(
            id=0,
            invoice_id=invoice_id,
            step=FETCH_PROGRESS_STEP,
            attempts=0,
            crashes=0,
            lease_until=datetime.now(UTC),
        )
    )


async def _texts(pool: AsyncConnectionPool[AsyncConnection[Any]], profile_id: str) -> list[str]:
    """Return the outbox texts for ``profile_id``, oldest first."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT payload_json->>'text' FROM outbox WHERE "to" = %s ORDER BY id""", (profile_id,)
        )
        return [str(row[0]) for row in await cur.fetchall()]


async def test_unposted_summary_queues_the_note(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """D206: in FETCHING_DOCS with an unposted summary, one BR-F17 note naming the invoice is queued."""
    profile_id = f"yo-{uuid4().hex}"
    invoice_id = await _invoice(db_pool, profile_id, state="FETCHING_DOCS", posted=False)

    await _run(db_pool, invoice_id)

    assert await _texts(db_pool, profile_id) == [FETCH_PROGRESS.format(id=invoice_id)]
    assert f"#{invoice_id}" in FETCH_PROGRESS.format(id=invoice_id)


async def test_running_twice_queues_one_note(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """D206: a second run queues nothing more."""
    profile_id = f"yo-{uuid4().hex}"
    invoice_id = await _invoice(db_pool, profile_id, state="FETCHING_DOCS", posted=False)

    await _run(db_pool, invoice_id)
    await _run(db_pool, invoice_id)

    assert len(await _texts(db_pool, profile_id)) == 1


async def test_posted_summary_queues_nothing(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """D206: once the summary is posted, no progress note is sent."""
    profile_id = f"yo-{uuid4().hex}"
    invoice_id = await _invoice(db_pool, profile_id, state="FETCHING_DOCS", posted=True)

    await _run(db_pool, invoice_id)

    assert await _texts(db_pool, profile_id) == []


@pytest.mark.parametrize("state", ["DELIVERING", "CANCELLED"])
async def test_other_states_queue_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], state: str
) -> None:
    """D206: an invoice that left FETCHING_DOCS gets no progress note."""
    profile_id = f"yo-{uuid4().hex}"
    invoice_id = await _invoice(db_pool, profile_id, state=state, posted=False)

    await _run(db_pool, invoice_id)

    assert await _texts(db_pool, profile_id) == []
