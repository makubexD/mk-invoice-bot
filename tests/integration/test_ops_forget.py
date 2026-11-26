"""Integration tests for the forget operation (D196)."""

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from invoicebot.ops.forget import ForgetRefused, ForgetRequest, forget
from invoicebot.sinks.onedrive import GraphError

pytestmark = pytest.mark.asyncio(loop_scope="session")

FOLDER = "/Reembolsos/2026-11/F014-15828"
UNKNOWN_ID = 1_999_999_998


class FakeDrive:
    """A drive that records deletes and can fail them."""

    def __init__(self, error: Exception | None = None) -> None:
        self.deleted: list[str] = []
        self._error = error

    async def delete(self, path: str) -> None:
        """Record the path, or raise the configured error."""
        if self._error is not None:
            raise self._error
        self.deleted.append(path)


async def _seed(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    data_dir: Path,
    *,
    state: str = "DONE",
    folder: str | None = FOLDER,
    local_path: str | None = None,
) -> tuple[int, str, list[Path]]:
    """Insert an invoice with a key, a file row and host files; return its id, key and files."""
    key = f"20000000001-F014-{uuid4().hex}"
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            INSERT INTO invoices (profile_id, origin_channel, state, flow_id, serie, numero, natural_key,
                                  flow_data_json)
            VALUES (%s, 'whatsapp', %s, 'work_microsoft', 'F014', '15828', %s, %s)
            RETURNING id
            """,
            (f"profile-{uuid4().hex}", state, key, Jsonb({"folder_path": folder} if folder else {})),
        )
        row = await cur.fetchone()
        assert row is not None
        invoice_id = int(row[0])
        photo = local_path or f"tmp/{invoice_id}/photo.jpg"
        await conn.execute(
            "INSERT INTO files (invoice_id, kind, local_path, sha256, size) "
            "VALUES (%s, 'photo', %s, 'ab', 1)",
            (invoice_id, photo),
        )
    files = [
        data_dir / "tmp" / str(invoice_id) / "photo.jpg",
        data_dir / "mail" / str(invoice_id) / "review.pdf",
        data_dir / "mail" / f"{invoice_id}.eml",
    ]
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    return invoice_id, key, files


async def _row(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int) -> tuple[Any, ...] | None:
    """Return the invoice's ``(natural_key, files count)`` or ``None`` when the invoice is gone."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT natural_key, (SELECT count(*) FROM files WHERE invoice_id = i.id) "
            "FROM invoices i WHERE id = %s",
            (invoice_id,),
        )
        return await cur.fetchone()


async def test_dry_run_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D196: a dry run reports the plan, deletes nothing and keeps the row, key and files."""
    invoice_id, key, files = await _seed(db_pool, tmp_path)
    drive = FakeDrive()
    lines = await forget(db_pool, tmp_path, drive, ForgetRequest(invoice_id, True, True, False))
    assert lines[0] == f"invoice {invoice_id} (F014-15828, DONE, flow work_microsoft)"
    assert f"drive: would delete {FOLDER}" in lines
    assert "files: would remove 3" in lines
    assert "database: would purge" in lines
    assert lines[-1] == "dry run: nothing changed"
    assert drive.deleted == []
    assert await _row(db_pool, invoice_id) == (key, 1)
    assert all(p.exists() for p in files)


async def test_apply_without_flags_releases_key(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D196: apply alone releases the key and keeps the row, the folder and the files."""
    invoice_id, _, files = await _seed(db_pool, tmp_path)
    drive = FakeDrive()
    lines = await forget(db_pool, tmp_path, drive, ForgetRequest(invoice_id, False, False, True))
    assert lines[1:] == ["drive: kept", "files: kept", "database: key released"]
    assert drive.deleted == []
    assert await _row(db_pool, invoice_id) == (None, 1)
    assert all(p.exists() for p in files)


async def test_apply_drive_deletes_folder_once(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D196: apply with drive deletes the folder once and releases the key."""
    invoice_id, _, files = await _seed(db_pool, tmp_path)
    drive = FakeDrive()
    lines = await forget(db_pool, tmp_path, drive, ForgetRequest(invoice_id, True, False, True))
    assert drive.deleted == [FOLDER]
    assert f"drive: deleted {FOLDER}" in lines
    assert lines[-1] == "database: key released"
    assert await _row(db_pool, invoice_id) == (None, 1)
    assert all(p.exists() for p in files)


async def test_apply_drive_purge_removes_everything(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D196: apply with drive and purge deletes the folder, the host files and the rows."""
    invoice_id, _, files = await _seed(db_pool, tmp_path)
    drive = FakeDrive()
    lines = await forget(db_pool, tmp_path, drive, ForgetRequest(invoice_id, True, True, True))
    assert drive.deleted == [FOLDER]
    assert "files: removed 3" in lines
    assert lines[-1] == "database: purged (files 1, invoices 1)"
    assert await _row(db_pool, invoice_id) is None
    assert not any(p.exists() for p in files)


async def test_non_terminal_invoice_is_refused(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D196: a BATCHED invoice is refused, naming /cancelar, and nothing changes."""
    invoice_id, key, files = await _seed(db_pool, tmp_path, state="BATCHED")
    drive = FakeDrive()
    with pytest.raises(ForgetRefused, match="/cancelar"):
        await forget(db_pool, tmp_path, drive, ForgetRequest(invoice_id, True, True, True))
    assert drive.deleted == []
    assert await _row(db_pool, invoice_id) == (key, 1)
    assert all(p.exists() for p in files)


async def test_missing_invoice_is_refused(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D196: an unknown invoice id is refused."""
    with pytest.raises(ForgetRefused, match="does not exist"):
        await forget(db_pool, tmp_path, None, ForgetRequest(UNKNOWN_ID, False, False, True))


@pytest.mark.parametrize("case", ["no_drive", "no_folder"])
async def test_drive_flag_without_drive_or_folder_is_refused(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path, case: str
) -> None:
    """D196: --drive with no drive, or with no folder_path, is refused before any change."""
    invoice_id, key, files = await _seed(db_pool, tmp_path, folder=None if case == "no_folder" else FOLDER)
    drive = None if case == "no_drive" else FakeDrive()
    with pytest.raises(ForgetRefused):
        await forget(db_pool, tmp_path, drive, ForgetRequest(invoice_id, True, True, True))
    assert await _row(db_pool, invoice_id) == (key, 1)
    assert all(p.exists() for p in files)


async def test_drive_failure_propagates_and_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D196: a GraphError from the drive propagates; the row, key and files are unchanged."""
    invoice_id, key, files = await _seed(db_pool, tmp_path)
    with pytest.raises(GraphError):
        await forget(
            db_pool, tmp_path, FakeDrive(GraphError(500)), ForgetRequest(invoice_id, True, True, True)
        )
    assert await _row(db_pool, invoice_id) == (key, 1)
    assert all(p.exists() for p in files)


async def test_local_path_outside_data_dir_is_skipped(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D196: a files.local_path outside the data directory is not removed and the report says so."""
    data_dir = tmp_path / "data"
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"x")
    invoice_id, _, _ = await _seed(db_pool, data_dir, local_path=str(outside))
    lines = await forget(db_pool, data_dir, None, ForgetRequest(invoice_id, False, True, True))
    assert outside.exists()
    assert f"files: skipped outside data dir: {outside}" in lines
    assert await _row(db_pool, invoice_id) is None
