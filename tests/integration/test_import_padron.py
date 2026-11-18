"""Integration tests for the padrón import and swap (R-2.5, D123)."""

from __future__ import annotations

import asyncio
import os
import sys
import tracemalloc
import zipfile
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot import padron
from invoicebot.padron import ImportResult, import_padron
from invoicebot.store.padron import PadronRow, lookup

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: The one text member a padrón ZIP may hold.
MEMBER = "padron_reducido_ruc.txt"
HEADER = "RUC|RAZON SOCIAL|ESTADO|CONDICION"
#: Synthetic RUCs, in the 11-digit space no seeded block uses.
OLD_RUC = "20000000001"
NEW_RUC = "20000000002"
BROKEN_RUC = "20000000003"
#: Base of the streaming test's rows: unique 11-digit RUCs, one per line.
LARGE_BASE = 20000000000
#: Lines of ~1000 bytes, enough to pass 20 MB of uncompressed text.
LARGE_ROWS = 21_000
_LINE_PADDING = "X" * 960
_MIN_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
#: Rows buffered before each write to the archive, so the test itself stays small.
_CHUNK_ROWS = 500
#: The import must not hold the 20 MB text in memory (R-2.5).
_MAX_PEAK_BYTES = 10 * 1024 * 1024
#: Two rows per COPY proves every batch, not just the first, reaches the staging table (R-2.5).
_TINY_BATCH_ROWS = 2
_TINY_BATCH_ROWS_COUNT = 5
#: The child RSS test's rows: unique 11-digit RUCs, one per line.
CHILD_BASE = 30000000000
CHILD_ROWS = 150_000
#: 900 characters of razón social put about 135 MB through COPY (R-2.5).
_RAZON_SOCIAL_CHARS = 900
#: The child's peak RSS must stay under 100 MB; one COPY grows libpq's buffer past it (R-2.5).
_MAX_CHILD_RSS_KB = 100 * 1024
#: Run as `python -c` with the ZIP path as argv[1]; the DSN comes from the environment, never argv.
#: `ru_maxrss` survives exec, so it would report the pytest parent's high-water mark; `VmHWM`
#: belongs to the child's own address space, so the child prints that, in KB.
_CHILD_IMPORT = """
import asyncio
import os
import sys
from pathlib import Path

from psycopg import AsyncConnection

from invoicebot.padron import import_padron


async def main() -> None:
    conn = await AsyncConnection.connect(os.environ["DATABASE_URL"], autocommit=True)
    async with conn:
        await import_padron(conn, Path(sys.argv[1]))
    status = Path("/proc/self/status").read_text(encoding="ascii")
    print(next(line.split()[1] for line in status.splitlines() if line.startswith("VmHWM:")))


asyncio.run(main())
"""


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def empty_padron(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> AsyncIterator[None]:
    """Start each test from an empty padrón, with no staging table left over."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM padron")
        await conn.execute("DROP TABLE IF EXISTS padron_staging")
    yield


def _write_zip(path: Path, lines: Sequence[str]) -> Path:
    """Write a stored (uncompressed) padrón ZIP with ``lines`` after the header."""
    text = "\n".join([HEADER, *lines]) + "\n"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(MEMBER, text.encode("iso-8859-1"))
    return path


def _write_large_zip(path: Path) -> None:
    """Write a >= 20 MB uncompressed padrón member row by row, in chunks."""
    with (
        zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive,
        archive.open(MEMBER, "w") as member,
    ):
        member.write(f"{HEADER}\n".encode("iso-8859-1"))
        chunk = bytearray()
        for index in range(LARGE_ROWS):
            ruc = LARGE_BASE + index
            line = f"{ruc}|EMPRESA {index}|ACTIVO|HABIDO|{_LINE_PADDING}\n"
            chunk += line.encode("iso-8859-1")
            if (index + 1) % _CHUNK_ROWS == 0:
                member.write(chunk)
                chunk.clear()
        member.write(chunk)


def _write_child_zip(path: Path) -> None:
    """Write the ~135 MB uncompressed padrón member the child RSS test imports, in chunks."""
    razon_social = "X" * _RAZON_SOCIAL_CHARS
    with (
        zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive,
        archive.open(MEMBER, "w") as member,
    ):
        member.write(f"{HEADER}\n".encode("iso-8859-1"))
        chunk = bytearray()
        for index in range(CHILD_ROWS):
            ruc = CHILD_BASE + index
            chunk += f"{ruc}|{razon_social}|ACTIVO|HABIDO\n".encode("iso-8859-1")
            if (index + 1) % _CHUNK_ROWS == 0:
                member.write(chunk)
                chunk.clear()
        member.write(chunk)


@asynccontextmanager
async def _autocommit(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> AsyncIterator[AsyncConnection[Any]]:
    """Lend a pooled connection in autocommit, as the script uses it, and restore it after."""
    async with db_pool.connection() as conn:
        await conn.set_autocommit(True)
        try:
            yield conn
        finally:
            await conn.set_autocommit(False)


async def _import(zip_path: Path, db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> ImportResult:
    """Run one import in autocommit, as the script does (R-2.5)."""
    async with _autocommit(db_pool) as conn:
        return await import_padron(conn, zip_path)


async def test_import_writes_rows_and_lookup_finds_one(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """R-2.5: the imported rows reach the padrón and the lookup reads one back."""
    zip_path = _write_zip(
        tmp_path / "padron.zip",
        [
            f"{OLD_RUC}|EMPRESA UNO|ACTIVO|HABIDO|AV. SIEMPRE VIVA 123",
            f"{NEW_RUC}|EMPRESA DOS|ACTIVO|HABIDO|AV. SIEMPRE VIVA 456",
            "NO ES UN RUC|EMPRESA TRES|ACTIVO|HABIDO",
        ],
    )

    result = await _import(zip_path, db_pool)

    assert result == ImportResult(rows=2, skipped=1)
    async with db_pool.connection() as conn:
        found = await lookup(conn, OLD_RUC)
    assert found == PadronRow(
        ruc=OLD_RUC,
        razon_social="EMPRESA UNO",
        estado="ACTIVO",
        condicion="HABIDO",
    )


async def test_second_import_replaces_the_first(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """R-2.5: a second import swaps in its rows and drops the old RUC."""
    first = _write_zip(tmp_path / "first.zip", [f"{OLD_RUC}|EMPRESA VIEJA|ACTIVO|HABIDO"])
    second = _write_zip(tmp_path / "second.zip", [f"{NEW_RUC}|EMPRESA NUEVA|ACTIVO|HABIDO"])

    async with _autocommit(db_pool) as conn:
        await import_padron(conn, first)
        await import_padron(conn, second)

    async with db_pool.connection() as conn:
        assert await lookup(conn, OLD_RUC) is None
        found = await lookup(conn, NEW_RUC)
    assert found == PadronRow(
        ruc=NEW_RUC,
        razon_social="EMPRESA NUEVA",
        estado="ACTIVO",
        condicion="HABIDO",
    )


async def test_failed_copy_keeps_the_old_padron(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """R-2.5: a COPY failure leaves the previous padrón untouched."""
    old = _write_zip(tmp_path / "old.zip", [f"{OLD_RUC}|EMPRESA VIEJA|ACTIVO|HABIDO"])
    # NUL passes parse_line but the text COPY rejects it, so the COPY fails mid-stream.
    broken = _write_zip(
        tmp_path / "broken.zip",
        [
            f"{NEW_RUC}|EMPRESA NUEVA|ACTIVO|HABIDO",
            f"{BROKEN_RUC}|EMPRESA\x00ROTA|ACTIVO|HABIDO",
        ],
    )

    async with _autocommit(db_pool) as conn:
        await import_padron(conn, old)
        with pytest.raises(psycopg.DataError):
            await import_padron(conn, broken)

    async with db_pool.connection() as conn:
        kept = await lookup(conn, OLD_RUC)
        new = await lookup(conn, NEW_RUC)
    assert kept == PadronRow(
        ruc=OLD_RUC,
        razon_social="EMPRESA VIEJA",
        estado="ACTIVO",
        condicion="HABIDO",
    )
    assert new is None


async def test_import_streams_a_large_zip_under_the_memory_cap(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """R-2.5: importing a ZIP with 20 MB or more of text peaks under 10 MB."""
    zip_path = tmp_path / "large.zip"
    _write_large_zip(zip_path)
    assert zip_path.stat().st_size >= _MIN_UNCOMPRESSED_BYTES

    tracemalloc.start()
    try:
        result = await _import(zip_path, db_pool)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result == ImportResult(rows=LARGE_ROWS, skipped=0)
    assert peak < _MAX_PEAK_BYTES


async def test_tiny_copy_batches_import_every_row(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-2.5: with two-row COPY batches, all five rows of the ZIP reach the padrón."""
    monkeypatch.setattr(padron, "_COPY_BATCH_ROWS", _TINY_BATCH_ROWS)
    rucs = [str(CHILD_BASE + index) for index in range(_TINY_BATCH_ROWS_COUNT)]
    zip_path = _write_zip(
        tmp_path / "batched.zip",
        [f"{ruc}|EMPRESA {index}|ACTIVO|HABIDO" for index, ruc in enumerate(rucs)],
    )

    result = await _import(zip_path, db_pool)

    assert result == ImportResult(rows=_TINY_BATCH_ROWS_COUNT, skipped=0)
    async with db_pool.connection() as conn:
        cur = await conn.execute("SELECT ruc FROM padron ORDER BY ruc")
        stored = [str(row[0]) for row in await cur.fetchall()]
    assert stored == rucs


async def test_import_keeps_child_rss_flat(
    tmp_path: Path,
    test_database_url: str,
) -> None:
    """R-2.5: a child importing ~135 MB through COPY peaks under 100 MB RSS."""
    zip_path = tmp_path / "child.zip"
    _write_child_zip(zip_path)
    env = {**os.environ, "DATABASE_URL": test_database_url}

    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _CHILD_IMPORT,
        str(zip_path),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await child.communicate()
    zip_path.unlink()

    assert child.returncode == 0, stderr.decode()
    peak_kb = int(stdout)
    assert peak_kb < _MAX_CHILD_RSS_KB, f"child peak RSS {peak_kb} KB >= {_MAX_CHILD_RSS_KB} KB"
