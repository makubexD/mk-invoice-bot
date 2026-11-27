"""Integration tests for the [Ver documentos] job (D206, BR-D24, BR-D25)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.pipeline.chat_docs import keep_documents
from invoicebot.pipeline.chat_docs_post import make_send_documents_handler
from invoicebot.pipeline.deliver import FOLDER_URL_KEY
from invoicebot.pipeline.steps import SEND_DOCUMENTS_STEP
from invoicebot.queue.jobs import Job
from invoicebot.rules.messages_es import DOCS_GONE, LINK_TITLE_FOLDER
from invoicebot.store.flow_b import merge_flow_data
from invoicebot.store.invoices import create_invoice, set_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

FOLDER_URL = "https://drive.example.com/folder/abc"


async def _done_invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]], profile_id: str, flow_data: dict[str, Any] | None = None
) -> int:
    """Create a DONE invoice, with ``flow_data`` merged when given."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await set_state(conn, invoice_id, "DONE", rule_id="BR-D10")
        if flow_data:
            await merge_flow_data(conn, invoice_id, flow_data)
    return invoice_id


async def _run(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int, data_dir: Path) -> None:
    """Run the handler once for ``invoice_id``."""
    await make_send_documents_handler(pool, data_dir)(
        Job(
            id=0,
            invoice_id=invoice_id,
            step=SEND_DOCUMENTS_STEP,
            attempts=0,
            crashes=0,
            lease_until=datetime.now(UTC),
        )
    )


async def _rows(pool: AsyncConnectionPool[AsyncConnection[Any]], profile_id: str) -> list[dict[str, Any]]:
    """Return the outbox payloads for ``profile_id``, oldest first."""
    async with pool.connection() as conn:
        cur = await conn.execute('SELECT payload_json FROM outbox WHERE "to" = %s ORDER BY id', (profile_id,))
        return [row[0] for row in await cur.fetchall()]


async def test_kept_files_are_sent_in_name_order(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D206: two kept files queue two BR-D24 document rows, captioned with their names, in name order."""
    profile_id = f"yo-{uuid4().hex}"
    invoice_id = await _done_invoice(db_pool, profile_id)
    keep_documents(tmp_path, invoice_id, [("b.xml", b"<x/>"), ("a.pdf", b"%PDF")])

    await _run(db_pool, invoice_id, tmp_path)

    rows = await _rows(db_pool, profile_id)
    assert [row["rule_id"] for row in rows] == ["BR-D24", "BR-D24"]
    assert [row["text"] for row in rows] == ["a.pdf", "b.xml"]
    assert [row["document"]["filename"] for row in rows] == ["a.pdf", "b.xml"]


async def test_gone_folder_names_the_invoice_with_the_folder_link(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D206: with the kept files gone, one BR-D25 row names #id and carries the saved folder link."""
    profile_id = f"yo-{uuid4().hex}"
    invoice_id = await _done_invoice(db_pool, profile_id, {FOLDER_URL_KEY: FOLDER_URL})

    await _run(db_pool, invoice_id, tmp_path)

    rows = await _rows(db_pool, profile_id)
    assert len(rows) == 1
    assert rows[0]["rule_id"] == "BR-D25"
    assert rows[0]["text"] == DOCS_GONE.format(id=invoice_id)
    assert f"#{invoice_id}" in rows[0]["text"]
    assert rows[0]["links"] == [{"title": LINK_TITLE_FOLDER, "url": FOLDER_URL}]


async def test_gone_folder_without_a_link_sends_no_link(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D206: with no kept files and no saved folder link, BR-D25 goes out with no link."""
    profile_id = f"yo-{uuid4().hex}"
    invoice_id = await _done_invoice(db_pool, profile_id)

    await _run(db_pool, invoice_id, tmp_path)

    rows = await _rows(db_pool, profile_id)
    assert [row["rule_id"] for row in rows] == ["BR-D25"]
    assert rows[0]["links"] == []
