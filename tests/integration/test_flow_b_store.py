"""Integration tests for flow B persistence (R-7.1, R-7.5, R-7.8, R-9.7, D88)."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import ChannelName
from invoicebot.store.flow_b import (
    claim_seq,
    get_batch_state,
    load_flow_data,
    merge_flow_data,
    open_batch,
    set_batch_state,
    take_photo_paths,
)
from invoicebot.store.invoices import FileRecord, add_file, create_invoice, set_flow

pytestmark = pytest.mark.asyncio(loop_scope="session")

PROFILE = "yo"
CHANNEL: ChannelName = "whatsapp"
YEAR = 2026
MONTH = 11
OTHER_MONTH = 12


async def _invoice(conn: AsyncConnection[Any], flow_id: str | None = None) -> int:
    """Create a synthetic invoice, optionally routed to flow_id."""
    invoice_id = await create_invoice(conn, PROFILE, CHANNEL)
    if flow_id is not None:
        await set_flow(conn, invoice_id, flow_id)
    return invoice_id


async def test_load_flow_data_starts_empty_and_merges_patches(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D88: flow data starts empty and merge_flow_data adds keys without dropping the others."""
    async with db_pool.connection() as conn:
        invoice_id = await _invoice(conn, "flow-data")
        before = await load_flow_data(conn, invoice_id)
        await merge_flow_data(conn, invoice_id, {"folder": "2026-11-Support_1_X"})
        await merge_flow_data(conn, invoice_id, {"seq": 2})
        after = await load_flow_data(conn, invoice_id)

    assert before == {}
    assert after == {"folder": "2026-11-Support_1_X", "seq": 2}


async def test_claim_seq_numbers_two_invoices_in_the_same_month(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D88: two invoices in one flow and month claim 1 and 2."""
    async with db_pool.connection() as conn:
        first = await _invoice(conn, "seq-two")
        second = await _invoice(conn, "seq-two")
        first_seq = await claim_seq(conn, "seq-two", YEAR, MONTH, 0, first)
        second_seq = await claim_seq(conn, "seq-two", YEAR, MONTH, 0, second)

    assert (first_seq, second_seq) == (1, 2)


async def test_claim_seq_keeps_the_invoice_number_on_a_second_claim(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D88: a second claim for the same invoice returns its first number."""
    async with db_pool.connection() as conn:
        invoice_id = await _invoice(conn, "seq-repeat")
        first_seq = await claim_seq(conn, "seq-repeat", YEAR, MONTH, 0, invoice_id)
        second_seq = await claim_seq(conn, "seq-repeat", YEAR, MONTH, 9, invoice_id)

    assert first_seq == 1
    assert second_seq == first_seq


async def test_claim_seq_starts_above_the_listed_max(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D88: listed_max = 4 makes the next claim 5."""
    async with db_pool.connection() as conn:
        invoice_id = await _invoice(conn, "seq-listed")
        seq = await claim_seq(conn, "seq-listed", YEAR, MONTH, 4, invoice_id)

    assert seq == 5


async def test_claim_seq_restarts_for_another_month(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D88: another month starts again from its listed max."""
    async with db_pool.connection() as conn:
        first = await _invoice(conn, "seq-months")
        second = await _invoice(conn, "seq-months")
        third = await _invoice(conn, "seq-months")
        assert await claim_seq(conn, "seq-months", YEAR, MONTH, 0, first) == 1
        assert await claim_seq(conn, "seq-months", YEAR, MONTH, 0, second) == 2
        other_month = await claim_seq(conn, "seq-months", YEAR, OTHER_MONTH, 3, third)

    assert other_month == 4


async def test_claim_seq_serializes_parallel_claims(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D88: two claims on separate connections get different numbers and the second waits."""
    flow_id = "seq-parallel"
    async with db_pool.connection() as conn:
        first = await _invoice(conn, flow_id)
        second = await _invoice(conn, flow_id)

    started = asyncio.Event()

    async def claim_on_its_own_connection(invoice_id: int) -> int:
        async with db_pool.connection() as conn:
            started.set()
            return await claim_seq(conn, flow_id, YEAR, MONTH, 0, invoice_id)

    async with db_pool.connection() as first_conn, first_conn.transaction():
        first_seq = await claim_seq(first_conn, flow_id, YEAR, MONTH, 0, first)
        pending = asyncio.create_task(claim_on_its_own_connection(second))
        await started.wait()
        await asyncio.sleep(0.1)
        blocked = not pending.done()
    second_seq = await pending

    assert blocked
    assert (first_seq, second_seq) == (1, 2)


async def test_open_batch_twice_returns_the_same_batch(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.5: opening a batch twice returns the same batch, linked from the invoice."""
    async with db_pool.connection() as conn:
        invoice_id = await _invoice(conn, "batch-one")
        first = await open_batch(conn, invoice_id, "batch-one", "prepared")
        second = await open_batch(conn, invoice_id, "batch-one", "sending")
        state = await get_batch_state(conn, invoice_id)

    assert first == second
    assert state == "prepared"


async def test_open_batch_rejects_an_unknown_state(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.5: open_batch rejects a state outside the Literal before touching the database."""
    async with db_pool.connection() as conn:
        invoice_id = await _invoice(conn, "batch-invalid")
        with pytest.raises(ValueError, match="unknown batch state"):
            await open_batch(conn, invoice_id, "batch-invalid", "sent")

        assert await get_batch_state(conn, invoice_id) is None


async def test_get_batch_state_is_none_without_a_batch(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.5: get_batch_state returns None for an invoice without a batch."""
    async with db_pool.connection() as conn:
        invoice_id = await _invoice(conn, "batch-none")

        assert await get_batch_state(conn, invoice_id) is None


async def test_set_batch_state_sent_stamps_sent_at(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.8: sent sets sent_at, and moving to another state keeps it."""
    async with db_pool.connection() as conn:
        invoice_id = await _invoice(conn, "batch-sent")
        batch_id = await open_batch(conn, invoice_id, "batch-sent", "sending")
        await set_batch_state(conn, batch_id, "sent")
        cur = await conn.execute("SELECT state, sent_at FROM batches WHERE id = %s", (batch_id,))
        sent = await cur.fetchone()
        await set_batch_state(conn, batch_id, "failed")
        cur = await conn.execute("SELECT state, sent_at FROM batches WHERE id = %s", (batch_id,))
        failed = await cur.fetchone()

    assert sent is not None
    assert sent[0] == "sent"
    assert sent[1] is not None
    assert failed is not None
    assert failed[0] == "failed"
    assert failed[1] == sent[1]


async def test_take_photo_paths_returns_the_path_once(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.7: take_photo_paths returns the path once and [] the second time."""
    photo = Path("/synthetic/photos/invoice.png")
    async with db_pool.connection() as conn:
        invoice_id = await _invoice(conn, "photos")
        await add_file(
            conn,
            invoice_id,
            FileRecord(
                kind="photo",
                local_path=str(photo),
                remote_ref=None,
                sha256="0" * 64,
                size=10,
            ),
        )
        first = await take_photo_paths(conn, invoice_id)
        second = await take_photo_paths(conn, invoice_id)

    assert first == [photo]
    assert second == []
