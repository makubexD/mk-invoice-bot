#!/usr/bin/env python3
"""Reject committed test fixtures that carry EXIF data or real-looking RUCs (R-9.17).

Usage: ``check_fixtures.py [root]`` (default root: the current directory).

Images must not contain EXIF metadata. Any standalone 11-digit number in a tracked
text file whose SUNAT check digit is valid must be listed in
``tests/synthetic_rucs.txt``; that allowlist is the only place synthetic RUC-like
values may appear. ``uv.lock`` and binary files are skipped. The one exception is
the store directory (R-5.8, D47): ``config/stores/<ruc>.yaml`` may contain its own
RUC, and only that one. Files come from ``git ls-files``; outside a git checkout
the root is walked instead. Exits 1 and prints one line per problem, 0 when the
fixtures are clean.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

from invoicebot.extract.validate import is_valid_ruc

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic"})
SKIP_DIRS = frozenset({"__pycache__", ".venv", ".git"})
SKIP_NAMES = frozenset({"uv.lock"})
BINARY_PROBE_BYTES = 8192
RUC_RE = re.compile(r"(?<![0-9])[0-9]{11}(?![0-9])")
ALLOWLIST_RELATIVE = Path("tests") / "synthetic_rucs.txt"
STORES_RELATIVE = Path("config") / "stores"


def _jpeg_has_exif(data: bytes) -> bool:
    """Walk the JPEG segments and look for an APP1 segment tagged ``Exif``."""
    if not data.startswith(b"\xff\xd8"):
        return False
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        if marker == 0xFF:
            offset += 1
            continue
        if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        length = int.from_bytes(data[offset + 2 : offset + 4], "big")
        if marker == 0xE1 and data[offset + 4 : offset + 10] == b"Exif\x00\x00":
            return True
        if marker == 0xDA or length < 2:
            return False
        offset += 2 + length
    return False


def _png_has_exif(data: bytes) -> bool:
    """Walk the PNG chunks and look for an ``eXIf`` chunk."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset = 8
    while offset + 8 <= len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        if data[offset + 4 : offset + 8] == b"eXIf":
            return True
        offset += 12 + length
    return False


def _webp_has_exif(data: bytes) -> bool:
    """Walk the WebP chunks and look for an ``EXIF`` chunk."""
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return False
    offset = 12
    while offset + 8 <= len(data):
        size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        if data[offset : offset + 4] == b"EXIF":
            return True
        offset += 8 + size + (size & 1)
    return False


def has_exif(path: Path) -> bool:
    """Return whether the file at ``path`` carries EXIF metadata (R-9.17)."""
    suffix = path.suffix.lower()
    if suffix in {".tif", ".tiff"}:
        return True
    data = path.read_bytes()
    if suffix in {".jpg", ".jpeg"}:
        return _jpeg_has_exif(data)
    if suffix == ".png":
        return _png_has_exif(data)
    if suffix == ".webp":
        return _webp_has_exif(data)
    if suffix == ".heic":
        return b"Exif" in data
    return False


def _load_allowlist(path: Path) -> set[str]:
    """Read the one-value-per-line synthetic RUC allowlist, ignoring comments."""
    if not path.is_file():
        return set()
    allowed: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.split("#", 1)[0].strip()
        if value:
            allowed.add(value)
    return allowed


def _own_store_ruc(path: Path, stores_dir: Path) -> str | None:
    """Return the RUC a store directory file is named after, or ``None`` for any other file."""
    if path.parent == stores_dir and path.suffix == ".yaml" and is_valid_ruc(path.stem):
        return path.stem
    return None


def _git_tracked_files(root: Path) -> list[Path] | None:
    """Return the files git tracks under ``root``, or ``None`` outside a git checkout."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no untrusted input
            [git, "ls-files", "-z"], cwd=root, capture_output=True, check=False
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    names = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    return [root / name for name in names if name]


def _walk_files(root: Path) -> Iterator[Path]:
    """Yield every file under ``root``, pruning skipped directories."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
        for filename in sorted(filenames):
            yield Path(dirpath) / filename


def _iter_scanned_files(root: Path) -> Iterator[Path]:
    """Yield the tracked files under ``root``, walking the tree when git is unavailable."""
    tracked = _git_tracked_files(root)
    if tracked is None:
        yield from _walk_files(root)
        return
    yield from (path for path in tracked if path.is_file())


def find_problems(root: Path) -> list[str]:
    """Return one message per EXIF-bearing image or non-allowlisted RUC (R-9.17)."""
    allowlist_path = root / ALLOWLIST_RELATIVE
    allowed = _load_allowlist(allowlist_path)
    stores_dir = root / STORES_RELATIVE
    problems: list[str] = []
    for path in _iter_scanned_files(root):
        relative = path.relative_to(root).as_posix()
        if path.name in SKIP_NAMES:
            continue
        if path.suffix.lower() in IMAGE_SUFFIXES:
            if has_exif(path):
                problems.append(f"{relative}: image contains EXIF metadata")
            continue
        if path == allowlist_path:
            continue
        data = path.read_bytes()
        if b"\x00" in data[:BINARY_PROBE_BYTES]:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        own_ruc = _own_store_ruc(path, stores_dir)
        for match in RUC_RE.finditer(text):
            value = match.group()
            if is_valid_ruc(value) and value not in allowed and value != own_ruc:
                problems.append(
                    f"{relative}: possible real RUC {value} (add synthetic values to "
                    f"{ALLOWLIST_RELATIVE.as_posix()})"
                )
    return problems


def main(argv: list[str] | None = None) -> int:
    """Check the fixture tree and return 1 when a problem is found."""
    parser = argparse.ArgumentParser(description="Check test fixtures for EXIF data and real-looking RUCs.")
    parser.add_argument("root", nargs="?", type=Path, default=Path(), help="repository root (default: .)")
    args = parser.parse_args(argv)
    problems = find_problems(args.root)
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
