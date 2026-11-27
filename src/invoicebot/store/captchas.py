"""Store the one pending captcha of an invoice (D209)."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

#: The upsert: a new ask replaces the old one and clears any answer (D209).
PUT_SQL = """
    INSERT INTO portal_captchas (invoice_id, portal_key, form_json, image_path, expires_at)
    VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s))
    ON CONFLICT (invoice_id) DO UPDATE
    SET portal_key = EXCLUDED.portal_key, form_json = EXCLUDED.form_json,
        image_path = EXCLUDED.image_path, answer = NULL, asked_at = now(),
        expires_at = EXCLUDED.expires_at
"""

#: The read; ``expired`` is decided by the database clock (D209).
GET_SQL = """
    SELECT invoice_id, portal_key, form_json, image_path, answer, asked_at, expires_at,
           expires_at <= now()
    FROM portal_captchas WHERE invoice_id = %s
"""

#: Stores the answer once, and only while the ask is still live (D209).
SET_ANSWER_SQL = """
    UPDATE portal_captchas SET answer = %s
    WHERE invoice_id = %s AND answer IS NULL AND expires_at > now()
"""

DELETE_SQL = "DELETE FROM portal_captchas WHERE invoice_id = %s"


@dataclass(frozen=True)
class PendingCaptcha:
    """One row of ``portal_captchas`` (D209)."""

    invoice_id: int
    portal_key: str
    form: dict[str, Any]
    image_path: str
    answer: str | None
    asked_at: datetime
    expires_at: datetime
    expired: bool


async def put(
    conn: AsyncConnection[Any],
    invoice_id: int,
    portal_key: str,
    form: Mapping[str, Any],
    image_path: str,
    timeout_s: float,
) -> None:
    """Store the pending captcha of ``invoice_id``, replacing any earlier one (D209).

    The answer is cleared and the ask expires ``timeout_s`` seconds from now. Does not commit.
    """
    await conn.execute(PUT_SQL, (invoice_id, portal_key, Jsonb(dict(form)), image_path, timeout_s))


async def get(conn: AsyncConnection[Any], invoice_id: int) -> PendingCaptcha | None:
    """Return the pending captcha of ``invoice_id``, or None (D209).

    Does not commit.
    """
    cur = await conn.execute(GET_SQL, (invoice_id,))
    row = await cur.fetchone()
    if row is None:
        return None
    return PendingCaptcha(
        invoice_id=row[0],
        portal_key=row[1],
        form=row[2],
        image_path=row[3],
        answer=row[4],
        asked_at=row[5],
        expires_at=row[6],
        expired=row[7],
    )


async def set_answer(conn: AsyncConnection[Any], invoice_id: int, answer: str) -> bool:
    """Record the owner's answer; False when there is no live, unanswered ask (D209).

    Does not commit.
    """
    cur = await conn.execute(SET_ANSWER_SQL, (answer, invoice_id))
    return cur.rowcount == 1


async def delete(conn: AsyncConnection[Any], invoice_id: int) -> None:
    """Remove the pending captcha of ``invoice_id`` (D209).

    Does not commit.
    """
    await conn.execute(DELETE_SQL, (invoice_id,))
