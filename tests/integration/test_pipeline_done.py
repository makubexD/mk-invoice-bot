"""Integration tests for BR-D10's text, built from the stored reading and flow data (D195)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.extract.fields import Field, Reading
from invoicebot.pipeline.done import MAIL_INVOICE_KEY, MAIL_TO_KEY, done_text
from invoicebot.store.flow_b import merge_flow_data
from invoicebot.store.invoices import create_invoice, save_reading

pytestmark = pytest.mark.asyncio(loop_scope="session")

FOLDER = "F"
PREFIX = "✅ F001-123 archivada en F. Outlook aceptó el correo para "


def _reading() -> Reading:
    """Build a synthetic reading with the serie and numero BR-D10 names."""
    return Reading(
        fields={
            "serie": Field(value="F001", source="qr", confidence=1.0),
            "numero": Field(value="123", source="qr", confidence=1.0),
        },
        document_url=None,
    )


async def _invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    extra: dict[str, Any],
    *,
    with_reading: bool = True,
) -> int:
    """Create an invoice with flow data ``folder`` plus ``extra``, and optionally a reading."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, f"done-{uuid4().hex}", "whatsapp")
        if with_reading:
            await save_reading(conn, invoice_id, _reading())
        await merge_flow_data(conn, invoice_id, {"folder": FOLDER, **extra})
    return invoice_id


async def _text(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int) -> str:
    """Return done_text for the invoice."""
    async with pool.connection() as conn:
        return await done_text(conn, invoice_id)


async def test_done_text_lists_the_mail_recipients(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-D10, D195: the To addresses are joined with a comma."""
    invoice_id = await _invoice(db_pool, {MAIL_TO_KEY: ["a@example.com", "b@example.com"]})
    assert await _text(db_pool, invoice_id) == f"{PREFIX}a@example.com, b@example.com."


async def test_done_text_without_mail_to_names_the_recipient_generically(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-D10, D195: no mail_to key falls back to "el destinatario"."""
    invoice_id = await _invoice(db_pool, {})
    assert await _text(db_pool, invoice_id) == f"{PREFIX}el destinatario."


async def test_done_text_with_an_empty_mail_to_names_the_recipient_generically(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-D10, D195: an empty mail_to list falls back to "el destinatario"."""
    invoice_id = await _invoice(db_pool, {MAIL_TO_KEY: []})
    assert await _text(db_pool, invoice_id) == f"{PREFIX}el destinatario."


async def test_done_text_prefers_the_serie_numero_saved_with_the_mail(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-D10, R-7.4, D195: the [serie, numero] deliver saved wins over the photo's reading."""
    invoice_id = await _invoice(db_pool, {MAIL_INVOICE_KEY: ["E001", "9"], MAIL_TO_KEY: ["a@example.com"]})
    assert await _text(db_pool, invoice_id) == (
        "✅ E001-9 archivada en F. Outlook aceptó el correo para a@example.com."
    )


async def test_done_text_ignores_a_malformed_saved_serie_numero(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-D10, D195: a saved value that is not a pair of strings falls back to the photo's reading."""
    invoice_id = await _invoice(db_pool, {MAIL_INVOICE_KEY: ["E001"], MAIL_TO_KEY: ["a@example.com"]})
    assert await _text(db_pool, invoice_id) == f"{PREFIX}a@example.com."


async def test_done_text_for_an_invoice_without_a_reading_raises(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-D10, D195: an invoice with no reading cannot be reported as done."""
    invoice_id = await _invoice(db_pool, {}, with_reading=False)
    with pytest.raises(RuntimeError):
        await _text(db_pool, invoice_id)
