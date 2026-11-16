"""laptop_outlook mail jobs (R-7.14, D108).

Every function takes the caller's connection and never commits: the caller owns
the transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection


@dataclass(frozen=True)
class MailJobState:
    """A stored laptop job: its invoice and whether a receipt already arrived (R-7.14)."""

    invoice_id: int
    has_receipt: bool


async def add_mail_job(conn: AsyncConnection[Any], job_id: str, invoice_id: int) -> None:
    """Store a laptop mail job for invoice_id; does not commit (R-7.14)."""
    await conn.execute(
        "INSERT INTO mail_jobs (job_id, invoice_id) VALUES (%s, %s)",
        (job_id, invoice_id),
    )


async def get_mail_job(conn: AsyncConnection[Any], job_id: str) -> MailJobState | None:
    """Lock the job and return its invoice and receipt state, or None when unknown; does not commit."""
    cur = await conn.execute(
        """
        SELECT invoice_id, receipt_at IS NOT NULL OR receipt_error IS NOT NULL
        FROM mail_jobs
        WHERE job_id = %s
        FOR UPDATE
        """,
        (job_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return MailJobState(invoice_id=int(row[0]), has_receipt=bool(row[1]))


async def set_receipt_at(conn: AsyncConnection[Any], job_id: str) -> None:
    """Stamp the job's receipt time from the database clock; does not commit (R-7.14)."""
    await conn.execute("UPDATE mail_jobs SET receipt_at = now() WHERE job_id = %s", (job_id,))


async def set_receipt_error(conn: AsyncConnection[Any], job_id: str, error: str) -> None:
    """Store the error the laptop reported for the job; does not commit (R-7.14)."""
    await conn.execute("UPDATE mail_jobs SET receipt_error = %s WHERE job_id = %s", (error, job_id))
