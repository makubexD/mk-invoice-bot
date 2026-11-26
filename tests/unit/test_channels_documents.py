"""Unit tests for the send-time document read (D194, R-1.9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from invoicebot.channels.base import Document
from invoicebot.channels.documents import read_document
from invoicebot.queue.outbox import PermanentSendError

NAME = "factura-ACME-SAC.xlsx"
MAX_BYTES = 1024


def _document(path: Path, *, size: int) -> Document:
    """Build a canonical document pointing at ``path`` with the declared ``size``."""
    return Document(path=str(path), filename=NAME, mime="application/vnd.ms-excel", size=size)


def _write(directory: Path, data: bytes = b"xlsx-bytes") -> Path:
    """Write a synthetic document under ``directory`` and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / NAME
    path.write_bytes(data)
    return path


async def test_a_file_under_the_data_dir_is_read(tmp_path: Path) -> None:
    """D194: a regular file under the data directory with the declared size is read whole."""
    path = _write(tmp_path / "data")

    data = await read_document(_document(path, size=10), tmp_path / "data", MAX_BYTES)

    assert data == b"xlsx-bytes"


async def test_a_path_outside_the_data_dir_is_refused_without_its_name(tmp_path: Path) -> None:
    """D194: a file outside the data directory is a PermanentSendError that names no file."""
    path = _write(tmp_path / "elsewhere")
    (tmp_path / "data").mkdir()

    with pytest.raises(PermanentSendError) as raised:
        await read_document(_document(path, size=10), tmp_path / "data", MAX_BYTES)

    assert NAME not in str(raised.value)
    assert str(tmp_path) not in str(raised.value)


async def test_a_symlink_inside_the_data_dir_pointing_out_is_refused(tmp_path: Path) -> None:
    """D194: a symlink under the data directory that points outside is refused without its name."""
    target = _write(tmp_path / "elsewhere")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    link = data_dir / NAME
    link.symlink_to(target)

    with pytest.raises(PermanentSendError) as raised:
        await read_document(_document(link, size=10), data_dir, MAX_BYTES)

    assert NAME not in str(raised.value)


async def test_a_size_mismatch_is_refused_without_its_name(tmp_path: Path) -> None:
    """D194: a file whose size differs from the declared one is refused without its name."""
    path = _write(tmp_path / "data")

    with pytest.raises(PermanentSendError) as raised:
        await read_document(_document(path, size=11), tmp_path / "data", MAX_BYTES)

    assert NAME not in str(raised.value)


async def test_a_missing_file_is_refused_without_its_name(tmp_path: Path) -> None:
    """D194: a file that is gone is a PermanentSendError that names no file."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    with pytest.raises(PermanentSendError) as raised:
        await read_document(_document(data_dir / NAME, size=10), data_dir, MAX_BYTES)

    assert NAME not in str(raised.value)


async def test_a_file_over_the_cap_is_refused(tmp_path: Path) -> None:
    """D194: a file over the channel cap is refused even when its declared size matches."""
    path = _write(tmp_path / "data", b"x" * (MAX_BYTES + 1))

    with pytest.raises(PermanentSendError, match="not a sendable file"):
        await read_document(_document(path, size=MAX_BYTES + 1), tmp_path / "data", MAX_BYTES)


async def test_a_directory_is_refused(tmp_path: Path) -> None:
    """D194: a path that is a directory is not a regular file and is refused."""
    directory = tmp_path / "data" / NAME
    directory.mkdir(parents=True)

    with pytest.raises(PermanentSendError, match="not a sendable file"):
        await read_document(_document(directory, size=0), tmp_path / "data", MAX_BYTES)
