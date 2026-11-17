"""Integration tests for the restore drill (R-9.11)."""

import asyncio
import os
import shlex
import subprocess
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pyrage
import pytest
import pytest_asyncio
from psycopg import AsyncConnection, conninfo, sql
from psycopg_pool import AsyncConnectionPool

from invoicebot.ops.backup import BackupJob, run_backup

pytestmark = pytest.mark.asyncio(loop_scope="session")

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "restore_drill.sh"
#: The local date the backup artifacts are named after.
TODAY = date(2026, 11, 16)
#: A cap no artifact in these tests reaches.
MAX_BYTES = 100_000_000
TARGET = "/backups/ops"
DUMP_NAME = f"db-{TODAY.isoformat()}.dump.age"
MAINTENANCE_DB = "postgres"


class FakeDrive:
    """An in-memory Drive that keeps the uploads the drill decrypts."""

    def __init__(self) -> None:
        self.uploads: dict[str, bytes] = {}

    async def upload(self, path: str, data: bytes) -> None:
        """Keep the bytes at ``path``."""
        self.uploads[path] = data

    async def download(self, path: str, max_bytes: int) -> bytes:
        """Return what was uploaded at ``path``."""
        return self.uploads[path]


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def migrated_database(db_pool: AsyncConnectionPool[AsyncConnection[object]]) -> None:
    """Depend on the session fixture that resets and migrates the database the dump reads."""


@pytest.fixture
def identity() -> pyrage.x25519.Identity:
    """Return a throwaway age identity the backup encrypts to (D119)."""
    return pyrage.x25519.Identity.generate()


@pytest.fixture
def job(tmp_path: Path, identity: pyrage.x25519.Identity, test_database_url: str) -> BackupJob:
    """Return a backup job pointed at the test database and the throwaway recipient."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "example.yaml").write_text("ops: {}\n")
    return BackupJob(
        cmd=("pg_dump",),
        database_url=test_database_url,
        config_dir=config_dir,
        excluded_paths=(),
        recipient=str(identity.to_public()),
        target=TARGET,
        max_bytes=MAX_BYTES,
    )


async def _run_ddl(dsn: str, statement: sql.Composable) -> None:
    """Run one autocommit statement against ``dsn``."""
    conn = await AsyncConnection.connect(dsn, autocommit=True)
    async with conn:
        await conn.execute(statement)


@pytest_asyncio.fixture(loop_scope="session")
async def fresh_database_url(test_database_url: str) -> AsyncIterator[str]:
    """Create an empty throwaway database and drop it after the test."""
    dbname = f"drill_{uuid4().hex[:12]}_test"
    params = conninfo.conninfo_to_dict(test_database_url)
    maintenance_dsn = conninfo.make_conninfo(**{**params, "dbname": MAINTENANCE_DB})
    database_url = conninfo.make_conninfo(**{**params, "dbname": dbname})
    await _run_ddl(maintenance_dsn, sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    try:
        yield database_url
    finally:
        await _run_ddl(
            maintenance_dsn,
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname)),
        )


async def _write_backup(
    job: BackupJob, identity: pyrage.x25519.Identity, directory: Path
) -> tuple[Path, Path]:
    """Run a backup and write its identity and dump where the script reads them."""
    drive = FakeDrive()
    await run_backup(job, drive, TODAY)
    identity_file = directory / "age-identity.txt"
    identity_file.write_text(f"{identity}\n")
    dump_file = directory / DUMP_NAME
    dump_file.write_bytes(drive.uploads[f"{TARGET}/{DUMP_NAME}"])
    return identity_file, dump_file


def _as_url(dsn: str) -> str:
    """Return ``dsn`` as the ``postgresql://`` URL the drill parses."""
    params = conninfo.conninfo_to_dict(dsn)
    host = params.get("host", "localhost")
    port = params.get("port", "5432")
    return (
        f"postgresql://{quote(params['user'], safe='')}:{quote(params['password'], safe='')}"
        f"@{host}:{port}/{quote(params['dbname'], safe='')}"
    )


async def _run_drill(
    identity_file: Path, dump_file: Path, database_url: str, path: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the drill script as the operator would and collect its output."""
    env = {**os.environ, "RESTORE_DATABASE_URL": _as_url(database_url)}
    if path is not None:
        env["PATH"] = path
    return await asyncio.to_thread(
        subprocess.run,
        ["bash", str(SCRIPT), str(identity_file), str(dump_file)],
        capture_output=True,
        text=True,
        env=env,
    )


def _place_fake_tool(bin_dir: Path, name: str, record_file: Path) -> None:
    """Put a fake ``name`` first on PATH that records its argv and succeeds."""
    script = bin_dir / name
    script.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" >> {shlex.quote(str(record_file))}\nexit 0\n"
    )
    script.chmod(0o755)


async def _invoice_count(database_url: str) -> int:
    """Return the number of rows in ``invoices``."""
    conn = await AsyncConnection.connect(database_url)
    async with conn:
        cursor = await conn.execute("SELECT count(*) FROM invoices")
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def test_restore_drill_restores_a_backup_into_a_fresh_database(
    job: BackupJob, identity: pyrage.x25519.Identity, tmp_path: Path, fresh_database_url: str
) -> None:
    """R-9.11: the drill restores the nightly dump into an empty database and prints the count."""
    identity_file, dump_file = await _write_backup(job, identity, tmp_path)

    completed = await _run_drill(identity_file, dump_file, fresh_database_url)

    assert completed.returncode == 0, completed.stderr
    assert "invoices: " in completed.stdout


async def test_restore_drill_refuses_a_target_that_already_has_invoices(
    job: BackupJob, identity: pyrage.x25519.Identity, tmp_path: Path, test_database_url: str
) -> None:
    """R-9.11: a target that already has invoices is refused and nothing is restored."""
    identity_file, dump_file = await _write_backup(job, identity, tmp_path)
    before = await _invoice_count(test_database_url)

    completed = await _run_drill(identity_file, dump_file, test_database_url)

    assert completed.returncode == 1
    assert "already has an invoices table" in completed.stderr
    assert await _invoice_count(test_database_url) == before


async def test_restore_drill_keeps_the_password_off_the_command_line(
    job: BackupJob, identity: pyrage.x25519.Identity, tmp_path: Path
) -> None:
    """R-9.15: psql and pg_restore read the password from the environment, not argv."""
    identity_file, dump_file = await _write_backup(job, identity, tmp_path)
    credential = "drill-password-5f8c1d"
    database_url = f"postgresql://drilluser:{credential}@127.0.0.1:5432/drilldb"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    records = {}
    for tool in ("psql", "pg_restore"):
        records[tool] = tmp_path / f"{tool}-argv.txt"
        _place_fake_tool(bin_dir, tool, records[tool])

    completed = await _run_drill(
        identity_file,
        dump_file,
        database_url,
        path=f"{bin_dir}:{os.environ['PATH']}",
    )

    for tool, record in records.items():
        assert record.is_file(), f"{tool} was not called: {completed.stderr}"
    recorded = "\n".join(record.read_text() for record in records.values())
    assert credential not in recorded
