"""The one URL BR-F14 last offered per invoice (D191).

[Sí] must teach exactly the URL the sender saw, so the offer is stored before
the question goes out. Every function takes the caller's connection and never
commits: the caller owns the transaction.
"""

from typing import Any, cast

from psycopg import AsyncConnection

#: The upsert: one candidate per invoice, stamped on the database clock (D191).
SAVE_SQL = """
    INSERT INTO portal_candidates (invoice_id, url)
    VALUES (%s, %s)
    ON CONFLICT (invoice_id) DO UPDATE
    SET url = EXCLUDED.url, updated_at = now()
"""

#: The read: the invoice's candidate, or no row when BR-F14 never offered one (D191).
GET_SQL = "SELECT url FROM portal_candidates WHERE invoice_id = %s"


async def save(conn: AsyncConnection[Any], invoice_id: int, url: str) -> None:
    """Store ``url`` as ``invoice_id``'s candidate, replacing any earlier one (D191).

    Does not commit.
    """
    await conn.execute(SAVE_SQL, (invoice_id, url))


async def get(conn: AsyncConnection[Any], invoice_id: int) -> str | None:
    """Return the URL BR-F14 last offered for ``invoice_id``, or None (D191).

    Does not commit.
    """
    cur = await conn.execute(GET_SQL, (invoice_id,))
    row = await cur.fetchone()
    return None if row is None else cast(str, row[0])
