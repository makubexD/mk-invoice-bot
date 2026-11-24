"""Integration tests for parking unanswered questions and telling the sender (BR-S08, R-3.7)."""

from typing import Any

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import ChannelName
from invoicebot.pipeline.park import park_stale_questions
from invoicebot.rules.messages_es import MESSAGES
from invoicebot.store.invoices import create_invoice, set_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

CHANNEL: ChannelName = "whatsapp"


async def test_park_stale_questions_queues_br_s08_for_the_parked_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S08: a question unanswered for 25 h is parked and BR-S08 is queued with its invoice id."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await set_state(conn, invoice_id, "CONFIRMING", rule_id="BR-R03")
        await conn.execute(
            "UPDATE invoices SET updated_at = now() - interval '25 hours' WHERE id = %s",
            (invoice_id,),
        )

    parked = await park_stale_questions(db_pool, 24)

    async with db_pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT payload_json->>'rule_id', payload_json->>'text', invoice_id, "to"
            FROM outbox
            WHERE invoice_id = %s
            """,
            (invoice_id,),
        )
        row = await cur.fetchone()
        cur = await conn.execute(
            "SELECT state, waiting_state FROM invoices WHERE id = %s",
            (invoice_id,),
        )
        state = await cur.fetchone()

    assert parked == 1
    assert row == ("BR-S08", MESSAGES["BR-S08"].format(id=invoice_id), invoice_id, "yo")
    assert state == ("PENDING_USER", "CONFIRMING")


async def test_park_stale_questions_br_s08_carries_the_pendientes_button(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D190: BR-S08's parked-invoice message carries the [📋 Pendientes] button that runs /pendientes."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await set_state(conn, invoice_id, "CONFIRMING", rule_id="BR-R03")
        await conn.execute(
            "UPDATE invoices SET updated_at = now() - interval '25 hours' WHERE id = %s",
            (invoice_id,),
        )

    await park_stale_questions(db_pool, 24)

    async with db_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT payload_json->'buttons' FROM outbox WHERE invoice_id = %s",
            (invoice_id,),
        )
        row = await cur.fetchone()

    assert row == ([{"id": "/pendientes", "title": "📋 Pendientes"}],)
