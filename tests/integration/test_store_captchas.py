"""Integration tests for the pending captcha store (D209)."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import ChannelName
from invoicebot.store.captchas import delete, get, put, set_answer
from invoicebot.store.invoices import create_invoice

pytestmark = pytest.mark.asyncio(loop_scope="session")

CHANNEL: ChannelName = "whatsapp"
FORM = {"ruc": "synthetic", "fields": ["a", "b"]}
TIMEOUT_S = 300


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def clean_captchas(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> AsyncIterator[None]:
    """Start each test from an empty portal_captchas table."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM portal_captchas")
    yield


async def test_put_then_get_returns_the_values(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D209: a stored captcha reads back as given, unanswered and not expired."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await put(conn, invoice_id, "portal-a", FORM, "captchas/a.png", TIMEOUT_S)
        row = await get(conn, invoice_id)

    assert row is not None
    assert (row.invoice_id, row.portal_key, row.form) == (invoice_id, "portal-a", FORM)
    assert (row.image_path, row.answer, row.expired) == ("captchas/a.png", None, False)


async def test_second_put_replaces_the_first_and_resets_the_answer(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D209: asking again swaps the portal and form and clears an earlier answer."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await put(conn, invoice_id, "portal-a", FORM, "captchas/a.png", TIMEOUT_S)
        assert await set_answer(conn, invoice_id, "xyz")
        await put(conn, invoice_id, "portal-b", {"other": 1}, "captchas/b.png", TIMEOUT_S)
        row = await get(conn, invoice_id)

    assert row is not None
    assert (row.portal_key, row.form, row.image_path) == ("portal-b", {"other": 1}, "captchas/b.png")
    assert row.answer is None


async def test_set_answer_works_once_on_a_live_ask(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D209: the first answer is stored; a second one is refused."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await put(conn, invoice_id, "portal-a", FORM, "captchas/a.png", TIMEOUT_S)
        first = await set_answer(conn, invoice_id, "abc")
        second = await set_answer(conn, invoice_id, "def")
        row = await get(conn, invoice_id)

    assert (first, second) == (True, False)
    assert row is not None
    assert row.answer == "abc"


async def test_set_answer_after_expiry_is_refused(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D209: an expired ask reads as expired and takes no answer."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await put(conn, invoice_id, "portal-a", FORM, "captchas/a.png", 0)
        accepted = await set_answer(conn, invoice_id, "abc")
        row = await get(conn, invoice_id)

    assert accepted is False
    assert row is not None
    assert (row.expired, row.answer) == (True, None)


async def test_delete_removes_the_captcha(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D209: a deleted captcha is gone."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await put(conn, invoice_id, "portal-a", FORM, "captchas/a.png", TIMEOUT_S)
        await delete(conn, invoice_id)
        row = await get(conn, invoice_id)

    assert row is None


async def test_deleting_the_invoice_cascades(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D209: removing the invoice removes its pending captcha."""
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, "yo", CHANNEL)
        await put(conn, invoice_id, "portal-a", FORM, "captchas/a.png", TIMEOUT_S)
        await conn.execute("DELETE FROM invoices WHERE id = %s", (invoice_id,))
        row = await get(conn, invoice_id)

    assert row is None
