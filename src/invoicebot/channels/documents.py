"""Read an outbound document for a transport, contained to the data directory (D194, R-1.9).

A document is a file under the data directory. Queue time checks that, but the row waits in
the outbox, so both transports read through this one function at send time: the path must
still resolve under the data directory, the open refuses a symlink, and the size is checked
on the open descriptor, so a swapped or grown file cannot slip past. Every refusal is a
``PermanentSendError`` with a generic message: legal names appear in attachment names, so
no file name or path may reach ``last_error``.
"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

from invoicebot.channels.base import Document
from invoicebot.queue.outbox import PermanentSendError

_UNSENDABLE = "document is not a sendable file under the data directory"


async def read_document(document: Document, data_dir: Path, max_bytes: int) -> bytes:
    """Return the document's bytes, or fail permanently when it is not a sendable file (D194).

    Sendable means a regular file, opened without following a symlink, under ``data_dir``,
    whose size is the one the message declared and is at most ``max_bytes``.
    """
    data = await asyncio.to_thread(_read, document, data_dir, max_bytes)
    if data is None:
        raise PermanentSendError(_UNSENDABLE)
    return data


def _read(document: Document, data_dir: Path, max_bytes: int) -> bytes | None:
    """Return the file's bytes, or ``None`` when any check fails (D194)."""
    try:
        path = Path(document.path).resolve()
        if not path.is_relative_to(data_dir.resolve()):
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes or info.st_size != document.size:
                return None
            data = handle.read(max_bytes + 1)
    except OSError:
        return None
    return data if len(data) <= max_bytes else None
