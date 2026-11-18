"""Reading SUNAT's padrón and importing it into the database (R-2.5, D123).

The padrón ZIP holds one ``|``-separated text file in ISO-8859-1 with a header
row. Its first four columns are RUC, razón social, estado and condición; the
address columns after them are ignored. ``import_padron`` streams those lines
into a staging table and swaps it in without holding them in memory.
"""

from __future__ import annotations

import io
import itertools
import re
import zipfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from psycopg import AsyncConnection

from invoicebot.channels.media_limits import check_zip

_ENCODING = "iso-8859-1"
_COLUMN_COUNT = 4
_TEXT_SUFFIX = ".txt"
#: SUNAT ships one text member compressed at about 4:1; the caps mirror the media limits.
_MAX_ZIP_ENTRIES = 20
_MAX_ZIP_RATIO = 50
_RUC_PATTERN = re.compile(r"[0-9]{11}")
#: psycopg 3.3 never flushes libpq's send buffer during one COPY on Linux, so each batch is its own COPY.
_COPY_BATCH_ROWS = 10_000
#: A new padrón smaller than this share of the current one is refused, so a truncated or
#: header-only file never empties the RUC lookup (R-2.5).
MIN_KEPT_SHARE = 0.5


class PadronShrankError(ValueError):
    """The new padrón has under ``MIN_KEPT_SHARE`` of the current rows; the old one stays (R-2.5)."""


@dataclass
class Skipped:
    """Mutable count of the malformed padrón lines ``rows`` skipped (R-2.5)."""

    count: int = 0


@dataclass(frozen=True)
class ImportResult:
    """The padrón rows written and the malformed lines skipped (R-2.5)."""

    rows: int
    skipped: int


def parse_line(line: str) -> tuple[str, str, str, str] | None:
    """Return the RUC, razón social, estado and condición of ``line`` (R-2.5).

    Returns ``None`` when the line has fewer than four columns or its first
    column is not an 11-digit RUC.
    """
    columns = line.split("|")
    if len(columns) < _COLUMN_COUNT:
        return None
    ruc, razon_social, estado, condicion = (column.strip() for column in columns[:_COLUMN_COUNT])
    if _RUC_PATTERN.fullmatch(ruc) is None:
        return None
    return ruc, razon_social, estado, condicion


def rows(lines: Iterable[str], skipped: Skipped) -> Iterator[tuple[str, str, str, str]]:
    """Yield the padrón rows of ``lines`` and count the malformed ones (R-2.5).

    The first line is the header. A line ``parse_line`` rejects increments
    ``skipped.count``.
    """
    for index, line in enumerate(lines):
        if index == 0:
            continue
        parsed = parse_line(line)
        if parsed is None:
            skipped.count += 1
            continue
        yield parsed


@contextmanager
def open_member(zip_path: Path) -> Iterator[TextIO]:
    """Open the padrón ZIP's single ``.txt`` member as ISO-8859-1 text (R-2.5).

    A ZIP with too many entries or too much compression is rejected before it
    is read; one with zero or several ``.txt`` members raises ``ValueError``.
    Closing the text wrapper closes the ZIP member and the archive.
    """
    check_zip(zip_path, _MAX_ZIP_ENTRIES, _MAX_ZIP_RATIO)
    with zipfile.ZipFile(zip_path) as archive:
        members = [name for name in archive.namelist() if name.endswith(_TEXT_SUFFIX)]
        if len(members) != 1:
            raise ValueError(f"the padrón ZIP must hold one .txt member, found {len(members)}")
        with archive.open(members[0]) as raw, io.TextIOWrapper(raw, encoding=_ENCODING, newline="") as text:
            yield text


async def import_padron(conn: AsyncConnection[Any], zip_path: Path) -> ImportResult:
    """Stream ``zip_path`` into ``padron`` through a staging table (R-2.5).

    The staging table is dropped and recreated from ``padron``, so this replaces
    every row. The caller's connection must be in autocommit: the COPY commits on
    its own, and the swap runs in one transaction. A failure while copying leaves
    the old padrón in place, and so does a new one under ``MIN_KEPT_SHARE`` of the
    current rows, which raises :class:`PadronShrankError`. Each batch of rows is its own ``COPY``, whose end
    flushes libpq's send buffer, so memory stays flat.
    """
    skipped = Skipped()
    imported = 0
    await conn.execute("DROP TABLE IF EXISTS padron_staging")
    await conn.execute("CREATE TABLE padron_staging (LIKE padron INCLUDING ALL)")
    with open_member(zip_path) as lines:
        for batch in itertools.batched(rows(lines, skipped), _COPY_BATCH_ROWS):
            async with conn.cursor().copy(
                "COPY padron_staging (ruc, razon_social, estado, condicion) FROM STDIN"
            ) as copy:
                for ruc, razon_social, estado, condicion in batch:
                    await copy.write_row((int(ruc), razon_social, estado, condicion))
                    imported += 1
    await _refuse_if_shrunk(conn, imported)
    async with conn.transaction():
        await conn.execute("DROP TABLE padron")
        await conn.execute("ALTER TABLE padron_staging RENAME TO padron")
        await conn.execute("ALTER INDEX padron_staging_pkey RENAME TO padron_pkey")
    return ImportResult(rows=imported, skipped=skipped.count)


async def _refuse_if_shrunk(conn: AsyncConnection[Any], imported: int) -> None:
    """Drop the staging table and raise when ``imported`` is under the kept share (R-2.5)."""
    cur = await conn.execute("SELECT count(*) FROM padron")
    row = await cur.fetchone()
    current = int(row[0]) if row is not None else 0
    if imported < current * MIN_KEPT_SHARE:
        await conn.execute("DROP TABLE padron_staging")
        raise PadronShrankError(f"new padron has {imported} rows, current has {current}")
