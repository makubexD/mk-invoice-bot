"""Integration tests for the dead-job notice (D190, BR-S03)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.buttons import command_button_id
from invoicebot.config.loader import load_config
from invoicebot.pipeline.failed import notify_dead
from invoicebot.rules.messages_es import BUTTON_RETRY, MESSAGES
from invoicebot.store.invoices import create_invoice

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
SENDER = "asistente"
ADMIN = "yo"
STEP = "fetch"
#: A first word longer than 40 characters, so the admin alert proves its cut.
ERROR = "ConnectTimeoutError_the_portal_did_not_answer after 30 s"
ERROR_CODE = "ConnectTimeoutError_the_portal_did_not_a"
#: No identity reaches this, so it names no invoice.
MISSING_INVOICE_ID = 2**31 - 1


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def clean_outbox(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> AsyncIterator[None]:
    """Start each test from empty outbox rows."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM outbox")
    yield


async def _outbox(
    conn: AsyncConnection[Any],
    recipient: str,
) -> list[tuple[str, str | None, list[dict[str, str]]]]:
    """Return a recipient's outbox ``(text, rule_id, buttons)`` rows, oldest first."""
    cur = await conn.execute(
        """
        SELECT payload_json->>'text', payload_json->>'rule_id', payload_json->'buttons'
        FROM outbox
        WHERE "to" = %s
        ORDER BY id
        """,
        (recipient,),
    )
    rows = await cur.fetchall()
    return [(str(text), rule_id, buttons) for text, rule_id, buttons in rows]


async def test_dead_job_notifies_the_sender_and_the_admins(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D190, BR-S03: a dead job tells the sender and the admins, with a retry button."""
    cfg = load_config(EXAMPLE)
    async with db_pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, SENDER, "whatsapp")
        await notify_dead(conn, invoice_id, STEP, ERROR, cfg.profiles)

    async with db_pool.connection() as conn:
        sender = await _outbox(conn, SENDER)
        admins = await _outbox(conn, ADMIN)

    assert sender == [
        (
            MESSAGES["BR-S03"].format(id=invoice_id),
            "BR-S03",
            [{"id": command_button_id("reprocesar", str(invoice_id)), "title": BUTTON_RETRY}],
        )
    ]
    assert admins == [
        (
            MESSAGES["BR-S03_admin"].format(step=STEP, id=invoice_id, error_code=ERROR_CODE),
            "BR-S03_admin",
            [],
        )
    ]


async def test_unknown_invoice_notifies_nobody(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D190, BR-S03: an invoice id with no row queues no message."""
    cfg = load_config(EXAMPLE)
    async with db_pool.connection() as conn, conn.transaction():
        await notify_dead(conn, MISSING_INVOICE_ID, STEP, ERROR, cfg.profiles)

    async with db_pool.connection() as conn:
        sender = await _outbox(conn, SENDER)
        admins = await _outbox(conn, ADMIN)
    assert sender == []
    assert admins == []
