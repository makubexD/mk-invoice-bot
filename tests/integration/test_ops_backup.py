"""Integration tests for the daily backup (R-9.11, R-9.16, S12.3, D119)."""

import asyncio
import io
import logging
import subprocess
import tarfile
from dataclasses import replace
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import pyrage
import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.ops.backup import BackupError, BackupJob, run_backup

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: The local date the artifacts are named after.
TODAY = date(2026, 11, 16)
#: A cap no artifact in these tests reaches.
MAX_BYTES = 100_000_000
TARGET = "/backups/ops"
#: The runtime age identity's file name, as deploy/docker-compose.yml mounts it beside the config.
IDENTITY_NAME = "age-identity.txt"


class FakeDrive:
    """An in-memory Drive that keeps uploads, or fails every upload with ``error``."""

    def __init__(self, error: Exception | None = None) -> None:
        self.uploads: dict[str, bytes] = {}
        self.error = error

    async def upload(self, path: str, data: bytes) -> None:
        """Keep the bytes at ``path``, or raise the configured error."""
        if self.error is not None:
            raise self.error
        self.uploads[path] = data

    async def download(self, path: str, max_bytes: int) -> bytes:
        """Return what was uploaded at ``path``."""
        return self.uploads[path]


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def migrated_database(db_pool: AsyncConnectionPool[AsyncConnection[object]]) -> None:
    """Depend on the session fixture that resets and migrates the database pg_dump reads."""


@pytest.fixture
def identity() -> pyrage.x25519.Identity:
    """Return a throwaway age identity the backup encrypts to (D119)."""
    return pyrage.x25519.Identity.generate()


@pytest.fixture
def drive() -> FakeDrive:
    """Return an empty in-memory Drive."""
    return FakeDrive()


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """Return a config directory with one kept file, a secrets, an age and an identity file."""
    directory = tmp_path / "config"
    directory.mkdir()
    (directory / "example.yaml").write_text("ops: {}\n")
    (directory / "secrets.yaml").write_text("token: synthetic\n")
    (directory / "store.age").write_text("ciphertext\n")
    (directory / IDENTITY_NAME).write_text("AGE-SECRET-KEY-SYNTHETIC\n")
    return directory


@pytest.fixture
def job(config_dir: Path, identity: pyrage.x25519.Identity, test_database_url: str) -> BackupJob:
    """Return a backup job pointed at the test database and the throwaway recipient."""
    return BackupJob(
        cmd=("pg_dump",),
        database_url=test_database_url,
        config_dir=config_dir,
        excluded_paths=(config_dir / IDENTITY_NAME,),
        recipient=str(identity.to_public()),
        target=TARGET,
        max_bytes=MAX_BYTES,
    )


async def test_backup_dump_restores_invoices_without_kv_data(
    job: BackupJob, identity: pyrage.x25519.Identity, tmp_path: Path
) -> None:
    """R-9.11: the uploaded dump decrypts and lists invoices with no kv data."""
    drive = FakeDrive()
    await run_backup(job, drive, TODAY)

    dump = pyrage.decrypt(drive.uploads[f"{TARGET}/db-{TODAY.isoformat()}.dump.age"], [identity])
    archive = tmp_path / "db.dump"
    archive.write_bytes(dump)
    completed = await asyncio.to_thread(
        subprocess.run,
        ["pg_restore", "--list", str(archive)],
        check=True,
        capture_output=True,
        text=True,
    )
    listing = completed.stdout

    assert "TABLE public invoices" in listing
    assert "TABLE DATA public kv" not in listing


async def test_backup_config_tarball_skips_secrets_and_age_files(
    job: BackupJob, identity: pyrage.x25519.Identity
) -> None:
    """R-9.11: the uploaded config tarball keeps example.yaml and no secrets, age or identity file."""
    drive = FakeDrive()
    await run_backup(job, drive, TODAY)

    data = pyrage.decrypt(drive.uploads[f"{TARGET}/config-{TODAY.isoformat()}.tar.age"], [identity])
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        names = archive.getnames()

    assert names == ["example.yaml"]


async def test_backup_returns_the_uploaded_payload_sizes(job: BackupJob, drive: FakeDrive) -> None:
    """R-9.11: run_backup returns the sizes of the two uploaded encrypted payloads."""
    sizes = await run_backup(job, drive, TODAY)

    dump_path = f"{TARGET}/db-{TODAY.isoformat()}.dump.age"
    config_path = f"{TARGET}/config-{TODAY.isoformat()}.tar.age"
    assert sizes.dump_bytes == len(drive.uploads[dump_path])
    assert sizes.config_bytes == len(drive.uploads[config_path])


async def test_backup_dump_over_cap_raises_too_large_and_uploads_nothing(
    job: BackupJob, drive: FakeDrive
) -> None:
    """R-9.11: a dump over max_bytes fails as too_large and uploads nothing."""
    with pytest.raises(BackupError) as raised:
        await run_backup(replace(job, max_bytes=1), drive, TODAY)

    assert raised.value.reason == "too_large"
    assert drive.uploads == {}


async def test_backup_config_over_cap_raises_too_large(job: BackupJob, drive: FakeDrive) -> None:
    """R-9.11: a config tarball over max_bytes fails as too_large."""
    with pytest.raises(BackupError) as raised:
        await run_backup(replace(job, cmd=("true",), max_bytes=1), drive, TODAY)

    assert raised.value.reason == "too_large"
    assert drive.uploads == {}


async def test_backup_upload_failure_raises_upload(job: BackupJob) -> None:
    """R-9.11: a Drive that refuses the upload fails the backup as upload."""
    with pytest.raises(BackupError) as raised:
        await run_backup(job, FakeDrive(error=RuntimeError("drive down")), TODAY)

    assert raised.value.reason == "upload"


async def test_backup_never_logs_or_raises_the_database_password(
    job: BackupJob, drive: FakeDrive, caplog: pytest.LogCaptureFixture
) -> None:
    """R-9.16: the database password is in no log record and in no error message."""
    parts = urlsplit(job.database_url)
    sentinel = "synthetic-not-a-real-credential"
    failing = parts._replace(
        netloc=f"{parts.username}:{sentinel}@{parts.hostname}:{parts.port}",
        path="/missing_backup_db",
    ).geturl()
    caplog.set_level(logging.DEBUG)

    with pytest.raises(BackupError) as raised:
        await run_backup(replace(job, database_url=failing), drive, TODAY)

    assert raised.value.reason == "dump"
    assert sentinel not in caplog.text
    assert sentinel not in str(raised.value)
