"""laptop_outlook mail jobs (R-7.14, D108).

Every function takes the caller's connection and never commits: the caller owns
the transaction.
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection


async def add_mail_job(conn: AsyncConnection[Any], job_id: str, invoice_id: int) -> None:
    """Store a laptop mail job for invoice_id; does not commit (R-7.14)."""
    await conn.execute(
        "INSERT INTO mail_jobs (job_id, invoice_id) VALUES (%s, %s)",
        (job_id, invoice_id),
    )
