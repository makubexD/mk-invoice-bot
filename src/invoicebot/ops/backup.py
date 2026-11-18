"""The daily backup: dump Postgres, tar the config, encrypt both, upload (S12.3, R-9.11, D119).

``pg_dump`` runs with the connection details in the child's environment only, so
neither the URL nor the password appears on the command line, in an exception or
in a log record (R-9.16). The dump is read into memory because :meth:`Drive.upload`
takes bytes; a dump over the cap fails the run (D119). Every failure raises
:class:`BackupError` whose ``reason`` names the stage for BR-O04.
"""

from __future__ import annotations

import asyncio
import fnmatch
import io
import logging
import os
import tarfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, unquote, urlsplit

import pyrage  # type: ignore[import-untyped]  # pyrage ships no type information

from invoicebot.sinks.base import Drive

logger = logging.getLogger(__name__)

#: How long ``pg_dump`` may run before the backup fails (D119).
DUMP_TIMEOUT_S = 600

#: Size of each dump chunk read, so the cap is checked while it streams (D119).
CHUNK_BYTES = 1 << 20

#: The dump options D119 names: a custom-format archive without the rebuildable caches.
DUMP_OPTIONS = ("--format=custom", "--exclude-table-data=kv", "--exclude-table-data=padron")

#: Config files never put in the tarball: the age secrets and encrypted stores (R-9.9, D119).
EXCLUDED_NAMES = ("secrets*", "*.age")

#: The stage that failed; BR-O04 maps each to its description.
BackupReason = Literal["dump", "too_large", "encrypt", "upload"]

#: The database URL query keys the dump accepts, each mapped to the child's environment name (R-9.14, D148).
PG_ENVIRONMENT_BY_QUERY_KEY = {
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "application_name": "PGAPPNAME",
    "passfile": "PGPASSFILE",
}


class BackupError(Exception):
    """A backup stage failed; ``reason`` says which and the message names only it."""

    def __init__(self, reason: BackupReason) -> None:
        super().__init__(f"backup failed: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class BackupSizes:
    """The sizes of the two encrypted artifacts a backup uploaded (R-9.11)."""

    dump_bytes: int
    config_bytes: int


@dataclass(frozen=True)
class BackupJob:
    """The inputs of one backup run (D119)."""

    cmd: tuple[str, ...]
    database_url: str
    config_dir: Path
    #: The secrets store and the runtime age identity, left out wherever they sit (R-9.9, D119).
    excluded_paths: tuple[Path, ...]
    recipient: str
    target: str
    max_bytes: int


async def run_backup(job: BackupJob, drive: Drive, today: date) -> BackupSizes:
    """Dump, tar, encrypt and upload both artifacts, or raise :class:`BackupError` (R-9.11, D119).

    Returns:
        The sizes of the encrypted dump and config payloads that were uploaded.
    """
    dump = await _dump(job)
    config = await asyncio.to_thread(_config_tarball, job)
    encrypted_dump = await asyncio.to_thread(_encrypt, dump, job.recipient)
    encrypted_config = await asyncio.to_thread(_encrypt, config, job.recipient)
    artifacts = (
        (f"db-{today.isoformat()}.dump.age", encrypted_dump),
        (f"config-{today.isoformat()}.tar.age", encrypted_config),
    )
    for name, data in artifacts:
        try:
            await drive.upload(f"{job.target}/{name}", data)
        except Exception:
            # The Drive port has no shared error type, so this boundary maps every adapter failure.
            raise BackupError("upload") from None
    return BackupSizes(dump_bytes=len(encrypted_dump), config_bytes=len(encrypted_config))


async def _dump(job: BackupJob) -> bytes:
    """Run ``pg_dump``, returning the archive bytes or raising :class:`BackupError` (D119)."""
    argv = [*job.cmd, *DUMP_OPTIONS]
    process: asyncio.subprocess.Process | None = None
    try:
        async with asyncio.timeout(DUMP_TIMEOUT_S):
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=pg_environment(job.database_url),
            )
            if process.stdout is None or process.stderr is None:
                raise BackupError("dump")
            stderr = asyncio.create_task(process.stderr.read())
            try:
                data = await _read_dump(process, process.stdout, job.max_bytes)
                returncode = await process.wait()
                await stderr
            finally:
                stderr.cancel()
                await asyncio.gather(stderr, return_exceptions=True)
    except BackupError:
        raise
    except (TimeoutError, OSError):
        if process is not None:
            if process.returncode is None:
                process.kill()
            await process.wait()
        raise BackupError("dump") from None
    if returncode != 0:
        logger.warning("backup dump failed", extra={"exit_code": returncode})
        raise BackupError("dump")
    return data


async def _read_dump(
    process: asyncio.subprocess.Process, stdout: asyncio.StreamReader, max_bytes: int
) -> bytes:
    """Read the dump, killing the process once it passes ``max_bytes`` (D119)."""
    data = bytearray()
    while chunk := await stdout.read(CHUNK_BYTES):
        data.extend(chunk)
        if len(data) > max_bytes:
            process.kill()
            await process.wait()
            raise BackupError("too_large")
    return bytes(data)


def _netloc_host(netloc: str) -> str | None:
    """Return the host from ``netloc`` with its case kept, or None when it has none (R-9.14)."""
    hostport = netloc.rsplit("@", 1)[-1]
    if hostport.startswith("["):
        end = hostport.find("]")
        return hostport[1:end] if end != -1 else None
    host = hostport.rsplit(":", 1)[0]
    return host or None


def pg_environment(database_url: str) -> dict[str, str]:
    """Return the child's environment: PATH plus the libpq connection keys (D119, R-9.14, R-9.16).

    Raises:
        ValueError: when the URL has a query key the dump cannot pass on, naming the key
            and never its value.
    """
    parts = urlsplit(database_url)
    values = {
        "PGHOST": _netloc_host(parts.netloc),
        "PGPORT": str(parts.port) if parts.port is not None else None,
        "PGUSER": unquote(parts.username) if parts.username is not None else None,
        "PGPASSWORD": unquote(parts.password) if parts.password is not None else None,
        "PGDATABASE": parts.path.lstrip("/") or None,
    }
    env = {"PATH": os.environ.get("PATH", "")}
    env.update({key: value for key, value in values.items() if value is not None})
    for key, value in parse_qsl(parts.query):
        env_name = PG_ENVIRONMENT_BY_QUERY_KEY.get(key)
        if env_name is None:
            raise ValueError(f"unsupported database URL query parameter: {key}")
        env[env_name] = value
    return env


def _config_tarball(job: BackupJob) -> bytes:
    """Build the config tarball in memory, skipping secrets, age files and the excluded paths (D119)."""
    excluded = {path.resolve() for path in job.excluded_paths}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(job.config_dir.rglob("*")):
            if path.is_file() and not _is_excluded(path.name) and path.resolve() not in excluded:
                archive.add(path, arcname=str(path.relative_to(job.config_dir)))
    data = buffer.getvalue()
    if len(data) > job.max_bytes:
        raise BackupError("too_large")
    return data


def _is_excluded(name: str) -> bool:
    """Return whether a config file name matches a never-backed-up pattern (D119)."""
    return any(fnmatch.fnmatch(name, pattern) for pattern in EXCLUDED_NAMES)


def _encrypt(data: bytes, recipient: str) -> bytes:
    """Encrypt ``data`` to the age recipient, or raise :class:`BackupError` (D119)."""
    try:
        encrypted: bytes = pyrage.encrypt(data, [pyrage.x25519.Recipient.from_str(recipient)])
    except (pyrage.RecipientError, pyrage.EncryptError):
        raise BackupError("encrypt") from None
    return encrypted
