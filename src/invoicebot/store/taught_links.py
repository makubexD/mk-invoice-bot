"""Taught links: the portal a buyer taught for an issuer (BR-F02, BR-S06).

A link is stored per issuer RUC and moves through ``proposed``, ``approved`` or
``rejected``; this module holds only the lookup the fetch step needs. Every
function takes the caller's connection and never commits: the caller owns the
transaction.
"""

from typing import Any

from psycopg import AsyncConnection


async def exists(conn: AsyncConnection[Any], ruc_emisor: int) -> bool:
    """Return True when any taught link row exists for ``ruc_emisor`` (BR-F02); does not commit."""
    cur = await conn.execute(
        "SELECT EXISTS (SELECT 1 FROM taught_links WHERE ruc_emisor = %s)",
        (ruc_emisor,),
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("SELECT EXISTS over taught_links yielded no row")
    return bool(row[0])
