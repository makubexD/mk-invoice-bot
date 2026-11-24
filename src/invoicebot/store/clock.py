"""The owner's date, read from the database clock."""

from datetime import date
from typing import Any, cast

from psycopg import AsyncConnection


async def owner_today(conn: AsyncConnection[Any], timezone: str) -> date:
    """Return today's date in ``timezone`` on the database clock, not Python's."""
    cur = await conn.execute("SELECT (now() AT TIME ZONE %s)::date", (timezone,))
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("the database returned no date")
    return cast(date, row[0])
