"""Integration tests for payment persistence (R-7.12, D107)."""

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import ChannelName
from invoicebot.extract.fields import Field, Reading
from invoicebot.states import BATCHED, DONE
from invoicebot.store.flow_b import open_batch, set_batch_state
from invoicebot.store.invoices import create_invoice, save_reading, set_flow, set_state
from invoicebot.store.payments import (
    AlreadyPaid,
    add_proposal,
    candidates,
    close_proposal,
    get_proposal,
    record_payment,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

CHANNEL: ChannelName = "whatsapp"
PAID_ON = date(2026, 11, 15)


def _unique(prefix: str) -> str:
    """Return an id no other test uses, so tests cannot interfere."""
    return f"{prefix}-{uuid4().hex}"


def _reading(total: str) -> Reading:
    """Return a synthetic reading whose only amount is the invoice total."""
    return Reading(
        fields={"total": Field(value=total, source="qr", confidence=1.0)},
        document_url=None,
    )


async def _invoice(
    conn: AsyncConnection[Any],
    profile_id: str,
    flow_id: str,
    *,
    total: str = "100.00",
    state: str = DONE,
    batch_state: str | None = "sent",
) -> int:
    """Create an invoice in flow_id with a reading, a state and a batch."""
    invoice_id = await create_invoice(conn, profile_id, CHANNEL)
    await set_flow(conn, invoice_id, flow_id)
    await save_reading(conn, invoice_id, _reading(total))
    await set_state(conn, invoice_id, state)
    if batch_state is not None:
        batch_id = await open_batch(conn, invoice_id, flow_id, "prepared")
        await set_batch_state(conn, batch_id, batch_state)
    return invoice_id


async def test_candidates_skip_old_paid_undone_and_unsent(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.12: only the DONE, sent, unpaid invoice of the last 180 days is a candidate."""
    profile_id = _unique("profile")
    flow_id = _unique("flow")
    async with db_pool.connection() as conn:
        old = await _invoice(conn, profile_id, flow_id)
        await conn.execute(
            "UPDATE invoices SET created_at = now() - interval '181 days' WHERE id = %s",
            (old,),
        )
        paid = await _invoice(conn, profile_id, flow_id)
        await record_payment(conn, flow_id, profile_id, PAID_ON, Decimal("100.00"), [paid])
        await _invoice(conn, profile_id, flow_id, state=BATCHED)
        await _invoice(conn, profile_id, flow_id, batch_state="prepared")
        eligible = await _invoice(conn, profile_id, flow_id)

        found = await candidates(conn, profile_id, flow_id, None)

    assert [candidate.invoice_id for candidate in found] == [eligible]


async def test_candidates_keep_the_twenty_oldest(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.12: 21 eligible invoices give 20 candidates, oldest first."""
    profile_id = _unique("profile")
    flow_id = _unique("flow")
    async with db_pool.connection() as conn:
        invoice_ids = [await _invoice(conn, profile_id, flow_id) for _ in range(21)]
        for index, invoice_id in enumerate(invoice_ids):
            await conn.execute(
                "UPDATE invoices SET created_at = now() - make_interval(hours => %s) WHERE id = %s",
                (len(invoice_ids) - index, invoice_id),
            )

        found = await candidates(conn, profile_id, flow_id, None)

    assert [candidate.invoice_id for candidate in found] == invoice_ids[:20]


async def test_candidates_cap_the_claimed_amount(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.12: an invoice whose total is above the cap claims the cap (R-7.13)."""
    profile_id = _unique("profile")
    flow_id = _unique("flow")
    async with db_pool.connection() as conn:
        invoice_id = await _invoice(conn, profile_id, flow_id, total="150.00")

        found = await candidates(conn, profile_id, flow_id, Decimal("120.00"))

    assert [(candidate.invoice_id, candidate.claimed_cents) for candidate in found] == [(invoice_id, 12000)]


async def test_record_payment_rejects_an_already_paid_invoice_and_rolls_back(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.12: paying an already paid invoice raises AlreadyPaid and leaves no payment row."""
    profile_id = _unique("profile")
    flow_id = _unique("flow")
    async with db_pool.connection() as conn:
        invoice_id = await _invoice(conn, profile_id, flow_id)
        first = await record_payment(conn, flow_id, profile_id, PAID_ON, Decimal("100.00"), [invoice_id])
        with pytest.raises(AlreadyPaid, match=str(invoice_id)):
            async with conn.transaction():
                await record_payment(conn, flow_id, profile_id, PAID_ON, Decimal("100.00"), [invoice_id])
        cur = await conn.execute("SELECT id FROM payments WHERE profile_id = %s", (profile_id,))
        payment_ids = [int(row[0]) for row in await cur.fetchall()]

    assert payment_ids == [first]


async def test_record_payment_never_links_another_profiles_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.12: record_payment never links another profile's invoice."""
    profile_id = _unique("profile")
    other_profile_id = _unique("profile")
    flow_id = _unique("flow")
    async with db_pool.connection() as conn:
        other_invoice = await _invoice(conn, other_profile_id, flow_id)
        with pytest.raises(AlreadyPaid):
            async with conn.transaction():
                await record_payment(conn, flow_id, profile_id, PAID_ON, Decimal("100.00"), [other_invoice])
        cur = await conn.execute("SELECT payment_id FROM invoices WHERE id = %s", (other_invoice,))
        row = await cur.fetchone()

    assert row is not None
    assert row[0] is None


async def test_proposal_round_trips_sets_and_closes(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.12: a proposal keeps its invoice-id sets and close_proposal changes its state."""
    profile_id = _unique("profile")
    flow_id = _unique("flow")
    async with db_pool.connection() as conn:
        proposal_id = await add_proposal(
            conn, profile_id, flow_id, PAID_ON, Decimal("144.60"), [(1, 2), (3,)]
        )
        stored = await get_proposal(conn, proposal_id)
        await close_proposal(conn, proposal_id, "confirmed")
        closed = await get_proposal(conn, proposal_id)

    assert stored is not None
    assert stored.profile_id == profile_id
    assert stored.flow_id == flow_id
    assert stored.paid_on == PAID_ON
    assert stored.amount == Decimal("144.60")
    assert stored.sets == ((1, 2), (3,))
    assert stored.state == "pending"
    assert closed is not None
    assert closed.state == "confirmed"
