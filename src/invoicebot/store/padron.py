"""Padrón lookup: the issuer's razón social and SUNAT state by RUC (R-2.5, D123).

The padrón is a full SUNAT RUC table imported by hand; one lookup is a
primary-key read. Every function takes the caller's connection and never
commits: the caller owns the transaction.
"""

import re
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection

#: An 11-digit RUC, ASCII digits only; valid RUCs never start with 0.
_RUC_PATTERN = re.compile(r"[0-9]{11}")

#: The primary-key lookup the ``padron_pkey`` index serves (R-2.5).
LOOKUP_SQL = """
    SELECT ruc, razon_social, estado, condicion
    FROM padron
    WHERE ruc = %s
"""


@dataclass(frozen=True)
class PadronRow:
    """The padrón columns the pipeline reads for an issuer (R-2.5)."""

    ruc: str
    razon_social: str
    estado: str
    condicion: str


async def lookup(conn: AsyncConnection[Any], ruc: str) -> PadronRow | None:
    """Return the padrón row for ``ruc``, or None when the padrón has no such RUC (R-2.5).

    A value that is not 11 ASCII digits is not a RUC, so it returns None without a
    query. Does not commit.
    """
    if _RUC_PATTERN.fullmatch(ruc) is None:
        return None
    cur = await conn.execute(LOOKUP_SQL, (int(ruc),))
    row = await cur.fetchone()
    if row is None:
        return None
    return PadronRow(
        ruc=str(row[0]),
        razon_social=str(row[1]),
        estado=str(row[2]),
        condicion=str(row[3]),
    )
