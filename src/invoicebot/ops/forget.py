"""Forget a terminal invoice: its drive folder, host files and database rows (D196)."""

import asyncio
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
    """The invoice cannot be forgotten; the message says why."""


@dataclass(frozen=True)
class _Files:
    """Host paths to remove (inside the data directory) and those skipped (outside it)."""

    inside: tuple[Path, ...]
    outside: tuple[str, ...]


async def _locked(pool: Pool, invoice_id: int) -> ForgetTarget:
    """Lock the invoice in its own transaction and return it, refusing a missing or non-terminal one."""
    async with pool.connection() as conn, conn.transaction():
        target = await lock_target(conn, invoice_id)
    return _require_terminal(target, invoice_id)


def _require_terminal(target: ForgetTarget | None, invoice_id: int) -> ForgetTarget:
    """Return ``target`` when it exists and is terminal; otherwise raise ``ForgetRefused``."""
    if target is None:
        raise ForgetRefused(f"invoice {invoice_id} does not exist")
    if target.state not in TERMINAL:
        raise ForgetRefused(
            f"invoice {invoice_id} is {target.state}, not finished; cancel it first with /cancelar"
        )
    return target


def _check_drive(target: ForgetTarget, drive: Drive | None, request: ForgetRequest) -> None:
    """Refuse ``--drive`` when there is no drive or no folder to delete."""
    if not request.drive:
        return
    if drive is None:
        raise ForgetRefused("--drive needs a configured drive")
    if not target.folder_path:
        raise ForgetRefused(f"invoice {request.invoice_id} has no drive folder")


def _plan_files(data_dir: Path, target: ForgetTarget) -> _Files:
    """Split the invoice's host paths into those under ``data_dir`` and those outside it."""
    root = data_dir.resolve()
    candidates = [
        data_dir / TMP_DIRECTORY / str(target.invoice_id),
        data_dir / MAIL_DIRECTORY / str(target.invoice_id),
        data_dir / MAIL_DIRECTORY / f"{target.invoice_id}.eml",
        *(data_dir / p for p in target.local_paths),
    ]
    inside: list[Path] = []
    outside: list[str] = []
    for path in candidates:
        if path.resolve().is_relative_to(root):
            inside.append(path)
        else:
            outside.append(str(path))
    return _Files(tuple(inside), tuple(outside))


def _existing(paths: tuple[Path, ...]) -> list[Path]:
    """Return the distinct paths that exist (a dangling link counts), leaving out those inside another."""
    found = [p for p in dict.fromkeys(paths) if p.exists() or p.is_symlink()]
    return [p for p in found if not any(q != p and p.is_relative_to(q) for q in found)]


def _remove(paths: tuple[Path, ...]) -> int:
    """Remove each existing path (a folder with its content) and return how many went."""
    found = _existing(paths)
    for path in found:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    return len(found)


def _head(target: ForgetTarget) -> str:
    """Return the line that names the invoice."""
    number = f"{target.serie}-{target.numero}"
    return f"invoice {target.invoice_id} ({number}, {target.state}, flow {target.flow_id})"


def _plan_lines(target: ForgetTarget, files: _Files, request: ForgetRequest, *, existing: int) -> list[str]:
    """Return the report of what an apply would do."""
    lines = [_head(target)]
    lines.append(f"drive: would delete {target.folder_path}" if request.drive else "drive: kept")
    lines.append(f"files: would remove {existing}" if request.purge else "files: kept")
    lines.extend(f"files: skipped outside data dir: {p}" for p in files.outside if request.purge)
    lines.append("database: would purge" if request.purge else "database: would release key")
    lines.append("dry run: nothing changed")
    return lines


def _counts_text(counts: dict[str, int]) -> str:
    """Return the nonzero purge counts as ``table n, ...``."""
    return ", ".join(f"{table} {n}" for table, n in counts.items() if n)


async def _finish(pool: Pool, request: ForgetRequest) -> dict[str, int] | None:
    """Re-lock the invoice and purge it or release its key; return the purge counts, or ``None``."""
    async with pool.connection() as conn, conn.transaction():
        _require_terminal(await lock_target(conn, request.invoice_id), request.invoice_id)
        if request.purge:
            return await purge(conn, request.invoice_id)
        await release_key(conn, request.invoice_id)
    return None


async def forget(pool: Pool, data_dir: Path, drive: Drive | None, request: ForgetRequest) -> list[str]:
    """Forget the invoice and return the report lines (D196).

    Refuses an unknown invoice, one not in a terminal state, and ``--drive``
    without a drive or a folder. Without ``apply`` nothing changes. With it the
    drive folder goes first (a failure leaves everything as it was), then the host
    files under ``data_dir`` for ``purge``, then the rows or the key in one
    transaction that checks the invoice is still terminal.
    """
    target = await _locked(pool, request.invoice_id)
    _check_drive(target, drive, request)
    files = _plan_files(data_dir, target)
    if not request.apply:
        existing = len(await asyncio.to_thread(_existing, files.inside))
        return _plan_lines(target, files, request, existing=existing)
    lines = [_head(target)]
    if request.drive and drive is not None and target.folder_path:
        await drive.delete(target.folder_path)
        lines.append(f"drive: deleted {target.folder_path}")
    else:
        lines.append("drive: kept")
    if request.purge:
        removed = await asyncio.to_thread(_remove, files.inside)
        lines.append(f"files: removed {removed}")
        lines.extend(f"files: skipped outside data dir: {p}" for p in files.outside)
    else:
        lines.append("files: kept")
    counts = await _finish(pool, request)
    lines.append(f"database: purged ({_counts_text(counts)})" if counts else "database: key released")
    return lines
