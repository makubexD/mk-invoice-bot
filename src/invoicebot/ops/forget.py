"""Forget a terminal invoice: its drive folder, host files and database rows (D196)."""

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from invoicebot.pipeline.handlers import TMP_DIRECTORY
from invoicebot.sinks.base import Drive
from invoicebot.sinks.mailer import MAIL_DIRECTORY
from invoicebot.states import TERMINAL
from invoicebot.store.db import Pool
from invoicebot.store.forget import ForgetTarget, lock_target, purge, release_key


@dataclass(frozen=True)
class ForgetRequest:
    """What the operator asked to forget: ``drive`` and ``purge`` are opt-in, ``apply`` ends a dry run."""

    invoice_id: int
    drive: bool
    purge: bool
    apply: bool


class ForgetRefused(Exception):
    """The invoice cannot be forgotten; the message says why.

    ``done`` holds the report lines of the parts already applied (the drive folder
    deleted before a late refusal), so the caller can still show them.
    """

    def __init__(self, reason: str, done: tuple[str, ...] = ()) -> None:
        super().__init__(reason)
        self.done = done


@dataclass(frozen=True)
class _Files:
    """Host paths: the derived ones, the recorded ones inside ``data_dir`` and those outside it."""

    derived: tuple[Path, ...]
    recorded: tuple[Path, ...]
    outside: tuple[str, ...]


@dataclass(frozen=True)
class _Scan:
    """What exists on disk: derived paths and recorded files to remove, recorded non-files to skip."""

    derived: tuple[Path, ...]
    recorded: tuple[Path, ...]
    not_file: tuple[Path, ...]

    @property
    def count(self) -> int:
        """Return how many paths a removal takes."""
        return len(self.derived) + len(self.recorded)


async def _read_and_check(pool: Pool, request: ForgetRequest) -> ForgetTarget:
    """Read the invoice (locking it only for this transaction) and check it may be forgotten."""
    async with pool.connection() as conn, conn.transaction():
        target = await lock_target(conn, request.invoice_id)
    return _require_forgettable(target, request)


def _require_forgettable(target: ForgetTarget | None, request: ForgetRequest) -> ForgetTarget:
    """Return ``target`` when it exists, is terminal and may be purged if asked; else refuse."""
    invoice_id = request.invoice_id
    if target is None:
        raise ForgetRefused(f"invoice {invoice_id} does not exist")
    if target.state not in TERMINAL:
        raise ForgetRefused(
            f"invoice {invoice_id} is {target.state}, not finished; cancel it first with /cancelar"
        )
    if request.purge and target.paid:
        raise ForgetRefused(
            f"invoice {invoice_id} is part of a payment; --purge would break it "
            "(release the key without --purge)"
        )
    return target


def _check_drive(target: ForgetTarget, drive: Drive | None, request: ForgetRequest) -> None:
    """Refuse ``--drive`` when there is no drive or no folder to delete."""
    if not request.drive:
        return
    if drive is None:
        raise ForgetRefused("--drive needs a configured drive")
    if not target.folder_path:
        raise ForgetRefused(
            f"invoice {request.invoice_id} has no OneDrive folder to delete "
            "(a flow A Sheet row and folder are removed by hand)"
        )


def _is_inside(data_dir: Path, path: Path) -> bool:
    """Return whether ``path``, as written and once resolved, is strictly under ``data_dir``."""
    written = Path(os.path.normpath(data_dir.absolute() / path))
    root = Path(os.path.normpath(data_dir.absolute()))
    resolved = path.resolve()
    resolved_root = data_dir.resolve()
    return (
        written != root
        and written.is_relative_to(root)
        and resolved != resolved_root
        and resolved.is_relative_to(resolved_root)
    )


def _plan_files(data_dir: Path, target: ForgetTarget) -> _Files:
    """Split the invoice's host paths into derived, recorded-inside and outside ``data_dir``."""
    derived_candidates = (
        data_dir / TMP_DIRECTORY / str(target.invoice_id),
        data_dir / MAIL_DIRECTORY / str(target.invoice_id),
        data_dir / MAIL_DIRECTORY / f"{target.invoice_id}.eml",
    )
    candidates = [data_dir / p for p in target.local_paths]
    derived = tuple(p for p in derived_candidates if _is_inside(data_dir, p))
    recorded = tuple(dict.fromkeys(p for p in candidates if _is_inside(data_dir, p)))
    outside = tuple(
        dict.fromkeys(str(p) for p in (*derived_candidates, *candidates) if not _is_inside(data_dir, p))
    )
    return _Files(derived, recorded, outside)


def _exists(path: Path) -> bool:
    """Return whether the path exists (a dangling link counts)."""
    return path.exists() or path.is_symlink()


def _scan(files: _Files) -> _Scan:
    """Look at the disk: which paths exist, and which recorded ones are not files or links."""
    derived = tuple(p for p in files.derived if _exists(p))
    recorded: list[Path] = []
    not_file: list[Path] = []
    for path in files.recorded:
        if not _exists(path) or any(path.is_relative_to(q) for q in derived):
            continue
        if path.is_dir() and not path.is_symlink():
            not_file.append(path)
        else:
            recorded.append(path)
    return _Scan(derived, tuple(recorded), tuple(not_file))


def _remove(scan: _Scan) -> int:
    """Remove the scanned paths (derived folders with their content) and return how many went."""
    for path in scan.derived:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    for path in scan.recorded:
        path.unlink(missing_ok=True)
    return scan.count


def _skipped_lines(files: _Files, scan: _Scan) -> list[str]:
    """Return the report lines for the paths a purge leaves alone."""
    lines = [f"files: skipped outside data dir: {p}" for p in files.outside]
    lines.extend(f"files: skipped not a file: {p}" for p in scan.not_file)
    return lines


def _head(target: ForgetTarget) -> str:
    """Return the line that names the invoice."""
    number = f"{target.serie}-{target.numero}"
    return f"invoice {target.invoice_id} ({number}, {target.state}, flow {target.flow_id})"


def _plan_lines(target: ForgetTarget, files: _Files, scan: _Scan, request: ForgetRequest) -> list[str]:
    """Return the report of what an apply would do."""
    lines = [_head(target)]
    lines.append(f"drive: would delete {target.folder_path}" if request.drive else "drive: kept")
    lines.append(f"files: would remove {scan.count}" if request.purge else "files: kept")
    if request.purge:
        lines.extend(_skipped_lines(files, scan))
    lines.append("database: would purge" if request.purge else "database: would release key")
    lines.append("dry run: nothing changed")
    return lines


def _counts_text(counts: dict[str, int]) -> str:
    """Return the nonzero purge counts as ``table n, ...``."""
    return ", ".join(f"{table} {n}" for table, n in counts.items() if n)


async def _delete_drive(target: ForgetTarget, drive: Drive | None, request: ForgetRequest) -> str:
    """Delete the drive folder when asked and return its report line."""
    if request.drive and drive is not None and target.folder_path:
        await drive.delete(target.folder_path)
        return f"drive: deleted {target.folder_path}"
    return "drive: kept"


async def _finish(pool: Pool, data_dir: Path, request: ForgetRequest, drive_line: str) -> list[str]:
    """Re-lock the invoice, re-check it, then remove its files and purge it, or release its key.

    The files go while the row is locked, after the re-check. A refusal here keeps
    ``drive_line`` in the error when the drive folder was already deleted.
    """
    done = (drive_line,) if drive_line.startswith("drive: deleted") else ()
    async with pool.connection() as conn, conn.transaction():
        try:
            target = _require_forgettable(await lock_target(conn, request.invoice_id), request)
        except ForgetRefused as error:
            raise ForgetRefused(str(error), done=done) from error
        lines = [drive_line]
        if not request.purge:
            await release_key(conn, request.invoice_id)
            return [*lines, "files: kept", "database: key released"]
        files = _plan_files(data_dir, target)
        scan = await asyncio.to_thread(_scan, files)
        removed = await asyncio.to_thread(_remove, scan)
        counts = await purge(conn, request.invoice_id)
    return [
        *lines,
        f"files: removed {removed}",
        *_skipped_lines(files, scan),
        f"database: purged ({_counts_text(counts)})",
    ]


async def forget(pool: Pool, data_dir: Path, drive: Drive | None, request: ForgetRequest) -> list[str]:
    """Forget the invoice and return the report lines (D196).

    Refuses an unknown invoice, one not in a terminal state, ``--purge`` on a paid
    invoice, and ``--drive`` without a drive or a folder. Without ``apply`` nothing
    changes. With it the drive folder goes first (a failure leaves everything as it
    was), then one transaction locks the invoice again, checks it, removes the host
    files for ``purge`` and deletes the rows, or releases the key.
    """
    target = await _read_and_check(pool, request)
    _check_drive(target, drive, request)
    if not request.apply:
        files = _plan_files(data_dir, target)
        scan = await asyncio.to_thread(_scan, files)
        return _plan_lines(target, files, scan, request)
    drive_line = await _delete_drive(target, drive, request)
    return [_head(target), *await _finish(pool, data_dir, request, drive_line)]
