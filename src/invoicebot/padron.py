"""Reading SUNAT's padrón text, one line at a time (R-2.5, D123).

The padrón ZIP holds one ``|``-separated text file in ISO-8859-1 with a header
row. Its first four columns are RUC, razón social, estado and condición; the
address columns after them are ignored. Nothing here touches the database.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from invoicebot.channels.media_limits import check_zip

_ENCODING = "iso-8859-1"
_COLUMN_COUNT = 4
_TEXT_SUFFIX = ".txt"
#: SUNAT ships one text member compressed at about 4:1; the caps mirror the media limits.
_MAX_ZIP_ENTRIES = 20
_MAX_ZIP_RATIO = 50
_RUC_PATTERN = re.compile(r"[0-9]{11}")


@dataclass
class Skipped:
    """Mutable count of the malformed padrón lines ``rows`` skipped (R-2.5)."""

    count: int = 0


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
