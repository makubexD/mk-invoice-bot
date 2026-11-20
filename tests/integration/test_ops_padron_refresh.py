"""Integration tests for the weekly padrón refresh (S12.4, D159)."""

from __future__ import annotations

import asyncio
import io
import zipfile
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.config.models import Padron
from invoicebot.ops.padron_refresh import PadronRefreshError, refresh_padron
from invoicebot.padron import ImportResult
from invoicebot.store.padron import PadronRow, lookup

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: The one text member a padrón ZIP may hold.
MEMBER = "padron_reducido_ruc.txt"
HEADER = "RUC|RAZON SOCIAL|ESTADO|CONDICION"
#: Synthetic RUCs, in the 11-digit space no other test file uses.
FIRST_RUC = "20000000901"
SECOND_RUC = "20000000902"
#: A deadline short enough to expire during the import.
TINY_IMPORT_TIMEOUT_S = 0.001
#: Unique, valid RUCs for a padrón whose COPY cannot finish inside one millisecond.
BULK_RUC_BASE = 30_000_000_000
BULK_ROWS = 20_000
#: One byte past a one-MiB cap.
OVER_MIB_BYTES = 1024 * 1024 + 1


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def empty_padron(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> AsyncIterator[None]:
    """Start each test from an empty padrón, with no staging table left over."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM padron")
        await conn.execute("DROP TABLE IF EXISTS padron_staging")
    yield


def _cfg(**overrides: Any) -> Padron:
    """Return the padrón settings a refresh test uses."""
    return Padron(refresh_cron="0 4 * * 0", **overrides)


def _padron_zip(lines: Sequence[str]) -> bytes:
    """Build a stored (uncompressed) padrón ZIP with ``lines`` after the header."""
    text = "\n".join([HEADER, *lines]) + "\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(MEMBER, text.encode("iso-8859-1"))
    return buffer.getvalue()


def _bulk_rows(count: int) -> list[str]:
    """Return ``count`` unique, valid padrón lines, so the import outlasts a tiny deadline."""
    return [f"{BULK_RUC_BASE + index}|EMPRESA {index}|ACTIVO|HABIDO" for index in range(count)]


def _ok(body: bytes) -> httpx.MockTransport:
    """Serve ``body`` with a 200 to any request."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    return httpx.MockTransport(handler)


def _stalled(first: bytes, second: bytes, delay_s: float) -> httpx.MockTransport:
    """Serve a body that stalls ``delay_s`` between two chunks, staying under any size cap."""

    async def chunks() -> AsyncIterator[bytes]:
        yield first
        await asyncio.sleep(delay_s)
        yield second

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=chunks())

    return httpx.MockTransport(handler)


def _status(code: int) -> httpx.MockTransport:
    """Serve ``code`` with an empty body to any request."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(code)

    return httpx.MockTransport(handler)


async def test_refresh_downloads_imports_and_deletes_the_zip(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """S12.4: a mini padrón ZIP is imported, its counts returned, and the file is gone."""
    body = _padron_zip(
        [
            f"{FIRST_RUC}|EMPRESA UNO|ACTIVO|HABIDO",
            f"{SECOND_RUC}|EMPRESA DOS|ACTIVO|HABIDO",
            "NO ES UN RUC|EMPRESA TRES|ACTIVO|HABIDO",
        ]
    )

    async with httpx.AsyncClient(transport=_ok(body)) as http:
        result = await refresh_padron(db_pool, http, _cfg(), tmp_path)

    assert result == ImportResult(rows=2, skipped=1)
    assert not (tmp_path / "padron" / "padron_reducido_ruc.zip").exists()
    async with db_pool.connection() as conn:
        found = await lookup(conn, FIRST_RUC)
    assert found == PadronRow(
        ruc=FIRST_RUC,
        razon_social="EMPRESA UNO",
        estado="ACTIVO",
        condicion="HABIDO",
    )


async def test_refresh_aborts_a_body_over_the_cap(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """S12.4: a body over ``max_mb`` raises the size reason and leaves no file."""
    async with httpx.AsyncClient(transport=_ok(b"0" * OVER_MIB_BYTES)) as http:
        with pytest.raises(PadronRefreshError, match="size") as raised:
            await refresh_padron(db_pool, http, _cfg(max_mb=1), tmp_path)

    assert raised.value.reason == "size"
    assert not list((tmp_path / "padron").iterdir())


async def test_refresh_aborts_a_download_past_its_deadline_and_keeps_the_old_rows(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """S12.4: a download stalled past ``download_timeout_s`` raises the timeout reason; old rows survive.

    The stalled body stays under any size cap, and no ZIP may be left behind.
    """
    body = _padron_zip([f"{FIRST_RUC}|EMPRESA UNO|ACTIVO|HABIDO"])
    async with httpx.AsyncClient(transport=_ok(body)) as http:
        await refresh_padron(db_pool, http, _cfg(), tmp_path)

    async with httpx.AsyncClient(transport=_stalled(b"P", b"K", 1)) as http:
        with pytest.raises(PadronRefreshError, match="timeout") as raised:
            await refresh_padron(db_pool, http, _cfg(download_timeout_s=0.05), tmp_path)

    assert raised.value.reason == "timeout"
    assert not list((tmp_path / "padron").iterdir())
    async with db_pool.connection() as conn:
        kept = await lookup(conn, FIRST_RUC)
    assert kept == PadronRow(
        ruc=FIRST_RUC,
        razon_social="EMPRESA UNO",
        estado="ACTIVO",
        condicion="HABIDO",
    )


async def test_refresh_aborts_an_import_past_its_deadline_and_keeps_the_old_rows(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """S12.4: an import past ``import_timeout_s`` raises the import timeout reason; the old rows survive.

    The download completes before the import deadline starts, and the padrón's
    rows cannot be copied inside one millisecond, so the deadline expires while
    ``_import`` runs. No ZIP may be left behind.
    """
    old_body = _padron_zip([f"{FIRST_RUC}|EMPRESA UNO|ACTIVO|HABIDO"])
    async with httpx.AsyncClient(transport=_ok(old_body)) as http:
        await refresh_padron(db_pool, http, _cfg(), tmp_path)

    slow_body = _padron_zip(_bulk_rows(BULK_ROWS))
    async with httpx.AsyncClient(transport=_ok(slow_body)) as http:
        with pytest.raises(PadronRefreshError, match="timeout") as raised:
            await refresh_padron(db_pool, http, _cfg(import_timeout_s=TINY_IMPORT_TIMEOUT_S), tmp_path)

    assert raised.value.reason == "import_timeout"
    assert not list((tmp_path / "padron").iterdir())
    async with db_pool.connection() as conn:
        kept = await lookup(conn, FIRST_RUC)
    assert kept == PadronRow(
        ruc=FIRST_RUC,
        razon_social="EMPRESA UNO",
        estado="ACTIVO",
        condicion="HABIDO",
    )


async def test_refresh_lets_a_timeout_error_inside_the_deadline_propagate(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """S12.4: a TimeoutError raised before the deadline is not reported as the timeout reason."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise TimeoutError("disk stalled")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(TimeoutError, match="disk stalled"):
            await refresh_padron(db_pool, http, _cfg(), tmp_path)

    assert not list((tmp_path / "padron").iterdir())


async def test_refresh_reports_a_bad_status_and_keeps_the_old_rows(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """S12.4: a 404 raises the status reason and the padrón imported earlier remains."""
    body = _padron_zip([f"{FIRST_RUC}|EMPRESA UNO|ACTIVO|HABIDO"])
    async with httpx.AsyncClient(transport=_ok(body)) as http:
        await refresh_padron(db_pool, http, _cfg(), tmp_path)

    async with httpx.AsyncClient(transport=_status(404)) as http:
        with pytest.raises(PadronRefreshError, match="status") as raised:
            await refresh_padron(db_pool, http, _cfg(), tmp_path)

    assert raised.value.reason == "status"
    async with db_pool.connection() as conn:
        kept = await lookup(conn, FIRST_RUC)
    assert kept == PadronRow(
        ruc=FIRST_RUC,
        razon_social="EMPRESA UNO",
        estado="ACTIVO",
        condicion="HABIDO",
    )


async def test_refresh_refuses_a_header_only_padron_and_keeps_the_old_rows(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """S12.4, R-2.5: a padrón under half the current rows raises the rows reason and swaps nothing."""
    body = _padron_zip([f"{FIRST_RUC}|EMPRESA UNO|ACTIVO|HABIDO"])
    async with httpx.AsyncClient(transport=_ok(body)) as http:
        await refresh_padron(db_pool, http, _cfg(), tmp_path)

    async with httpx.AsyncClient(transport=_ok(_padron_zip([]))) as http:
        with pytest.raises(PadronRefreshError, match="rows") as raised:
            await refresh_padron(db_pool, http, _cfg(), tmp_path)

    assert raised.value.reason == "rows"
    assert not (tmp_path / "padron" / "padron_reducido_ruc.zip").exists()
    async with db_pool.connection() as conn:
        kept = await lookup(conn, FIRST_RUC)
        cur = await conn.execute("SELECT to_regclass('padron_staging')")
        staging = (await cur.fetchone())[0]
    assert kept is not None
    assert staging is None
