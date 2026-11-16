"""Integration tests for laptop mail job persistence (R-7.14, D108)."""

from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import ChannelName
from invoicebot.store.invoices import create_invoice
from invoicebot.store.mail_jobs import add_mail_job

pytestmark = pytest.mark.asyncio(loop_scope="session")

CHANNEL: ChannelName = "whatsapp"


def _unique(prefix: str) -> str:
    """Return an id no other test uses, so tests cannot interfere."""
    return f"{prefix}-{uuid4().hex}"


async def test_add_mail_job_stores_the_row_with_the_database_clock(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.14: the row keeps the job id, the invoice id and a created_at from the transaction now()."""
    profile_id = _unique("profile")
    job_id = _unique("job")
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, profile_id, CHANNEL)

        await add_mail_job(conn, job_id, invoice_id)

        cur = await conn.execute(
            """
            SELECT job_id, invoice_id, created_at, now(), receipt_at, receipt_error
            FROM mail_jobs
            WHERE job_id = %s
            """,
            (job_id,),
        )
        row = await cur.fetchone()

    assert row is not None
    assert row[0] == job_id
    assert row[1] == invoice_id
    assert row[2] == row[3]
    assert row[4] is None
    assert row[5] is None


async def test_add_mail_job_rejects_a_duplicate_job_id(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.14: a second insert of the same job id raises UniqueViolation."""
    profile_id = _unique("profile")
    job_id = _unique("job")
    async with db_pool.connection() as conn:
        invoice_id = await create_invoice(conn, profile_id, CHANNEL)
        await add_mail_job(conn, job_id, invoice_id)

        with pytest.raises(UniqueViolation):
            async with conn.transaction():
                await add_mail_job(conn, job_id, invoice_id)
