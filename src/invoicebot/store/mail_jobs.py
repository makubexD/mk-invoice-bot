"""laptop_outlook mail jobs (R-7.14, D108).

Every function takes the caller's connection and never commits: the caller owns
the transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from psycopg import AsyncConnection


@dataclass(frozen=True)
class MailJobState:
    """A stored laptop job: its invoice and whether a receipt already arrived (R-7.14)."""

    invoice_id: int
    has_receipt: bool


@dataclass(frozen=True)
class OpenMailJob:
    """An unfinished laptop job with the database clock's answers about its age (R-7.8, R-7.14)."""

    job_id: str
    invoice_id: int
    #: Whole hours since the job was written, computed by the database.
    age_hours: int
    #: Whether the job is past ``agent_stale_after``.
    is_stale: bool
    #: Whether the job is past ``confirm_timeout``.
    is_overdue: bool


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


async def list_open_jobs(
    conn: AsyncConnection[Any],
    stale_after: timedelta,
    confirm_timeout: timedelta,
) -> list[OpenMailJob]:
    """Return every unreceipted job whose batch is still prepared, oldest first; does not commit.

    The age and both thresholds are compared by the database clock (R-7.8, R-7.14).
    """
    cur = await conn.execute(
        """
        SELECT m.job_id,
               m.invoice_id,
               floor(extract(epoch FROM now() - m.created_at) / 3600)::int,
               m.created_at < now() - %s,
               m.created_at < now() - %s
        FROM mail_jobs AS m
        JOIN invoices AS i ON i.id = m.invoice_id
        JOIN batches AS b ON b.id = i.batch_id
        WHERE m.receipt_at IS NULL
          AND m.receipt_error IS NULL
          AND b.state = 'prepared'
        ORDER BY m.created_at
        """,
        (stale_after, confirm_timeout),
    )
    rows = await cur.fetchall()
    return [
        OpenMailJob(
            job_id=str(job_id),
            invoice_id=int(invoice_id),
            age_hours=int(age_hours),
            is_stale=bool(is_stale),
            is_overdue=bool(is_overdue),
        )
        for job_id, invoice_id, age_hours, is_stale, is_overdue in rows
    ]


async def claim_stale_alert(conn: AsyncConnection[Any], job_id: str) -> bool:
    """Mark the job's stale alert sent, returning whether this call won; does not commit (R-7.14)."""
    cur = await conn.execute(
        """
        UPDATE mail_jobs
        SET stale_alerted_at = now()
        WHERE job_id = %s AND stale_alerted_at IS NULL
        RETURNING job_id
        """,
        (job_id,),
    )
    return await cur.fetchone() is not None


async def claim_overdue_alert(conn: AsyncConnection[Any], job_id: str) -> bool:
    """Mark the job's overdue alert sent, returning whether this call won; does not commit (R-7.8)."""
    cur = await conn.execute(
        """
        UPDATE mail_jobs
        SET overdue_alerted_at = now()
        WHERE job_id = %s AND overdue_alerted_at IS NULL
        RETURNING job_id
        """,
        (job_id,),
    )
    return await cur.fetchone() is not None


async def has_mail_job(conn: AsyncConnection[Any], invoice_id: int) -> bool:
    """Return whether invoice_id went through a laptop job; does not commit (R-7.14)."""
    cur = await conn.execute(
        "SELECT EXISTS (SELECT 1 FROM mail_jobs WHERE invoice_id = %s)",
        (invoice_id,),
    )
    row = await cur.fetchone()
    return bool(row[0]) if row is not None else False


async def set_receipt_at(conn: AsyncConnection[Any], job_id: str) -> None:
    """Stamp the job's receipt time from the database clock; does not commit (R-7.14)."""
    await conn.execute("UPDATE mail_jobs SET receipt_at = now() WHERE job_id = %s", (job_id,))


async def set_receipt_error(conn: AsyncConnection[Any], job_id: str, error: str) -> None:
    """Store the error the laptop reported for the job; does not commit (R-7.14)."""
    await conn.execute("UPDATE mail_jobs SET receipt_error = %s WHERE job_id = %s", (error, job_id))
