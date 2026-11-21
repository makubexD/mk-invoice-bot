"""The dev-only rehearsal channel: run the live pipeline without WhatsApp (D165).

Inbound media arrives as a file in ``<data_dir>/console/inbox`` and the outbox
transport keeps console rows in the database for ``invoicebot rehearse replies``
to print. The channel is off unless the config lists it and is refused in
production (D165).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from invoicebot.channels.base import (
    AttachmentKind,
    ChannelName,
    DownloadedMedia,
    MediaSource,
)
from invoicebot.channels.media_limits import (
    DIRECTORY_MODE,
    FILE_NAME,
    MEDIA_HEADER_BYTES,
    ByteCap,
    MediaRejected,
    check_image_header,
    max_bytes_for,
)
from invoicebot.config.models import Config, ConsoleChannel, MediaLimits
from invoicebot.queue.outbox import OutboxRow, Transport

logger = logging.getLogger(__name__)

CONSOLE: ChannelName = "console"
#: Longest ``media_ref`` the console source accepts (D165).
MEDIA_REF_MAX_CHARS = 64
_MEDIA_REF_PATTERN = re.compile(rf"[A-Za-z0-9._-]{{1,{MEDIA_REF_MAX_CHARS}}}")
#: The attachment kind and mime a console file name's extension gives (D165).
MEDIA_BY_SUFFIX: dict[str, tuple[AttachmentKind, str]] = {
    ".jpg": ("photo", "image/jpeg"),
    ".jpeg": ("photo", "image/jpeg"),
    ".png": ("photo", "image/png"),
    ".pdf": ("pdf", "application/pdf"),
    ".xml": ("xml", "application/xml"),
}
_CHUNK_BYTES = 64 * 1024


def inbox_dir(data_dir: Path) -> Path:
    """Return the directory ``rehearse send`` drops inbox files in (D165)."""
    return data_dir / "console" / "inbox"


class ConsoleMediaSource:
    """The ``MediaSource`` adapter over the console inbox directory (D165)."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    async def download(
        self, media_ref: str, kind: AttachmentKind, out_dir: Path, limits: MediaLimits
    ) -> DownloadedMedia:
        """Copy one inbox file into ``out_dir`` and remove the inbox copy (D165).

        A retried download is idempotent: when the inbox copy is already gone but the
        stored file exists, that file is re-hashed and returned (D165).

        Raises:
            ValueError: when ``media_ref`` is not a plain file name or its extension is unknown.
            FileNotFoundError: when neither an inbox file nor a stored file matches ``media_ref``.
            MediaRejected: when the kind is unsupported, the file is over its cap, or a
                photo is not PNG/JPEG or declares too many pixels.
        """
        return await asyncio.to_thread(_download, self._data_dir, media_ref, kind, out_dir, limits)


def _download(
    data_dir: Path,
    media_ref: str,
    kind: AttachmentKind,
    out_dir: Path,
    limits: MediaLimits,
) -> DownloadedMedia:
    """Validate, copy, hash and clean up one inbox file; runs in a worker thread."""
    if _MEDIA_REF_PATTERN.fullmatch(media_ref) is None or media_ref.startswith("."):
        raise ValueError("media_ref must be a plain file name")
    media = MEDIA_BY_SUFFIX.get(Path(media_ref).suffix.lower())
    if media is None:
        raise ValueError(f"unsupported media extension: {media_ref!r}")
    mime = media[1]
    source = inbox_dir(data_dir) / media_ref
    limit = max_bytes_for(kind, limits)
    directory = out_dir / media_ref
    path = directory / FILE_NAME
    if not source.is_file():
        if path.is_file():
            return _existing_download(path, mime, limit)
        raise FileNotFoundError(source)
    try:
        directory.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
        size, sha256 = _copy_to_file(source, path, ByteCap(limit), kind, limits.max_megapixels)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    source.unlink()
    return DownloadedMedia(path=path, sha256=sha256, size=size, mime=mime)


def _existing_download(path: Path, mime: str, limit: int) -> DownloadedMedia:
    """Return an already stored file's media, re-hashed under the same cap (D165)."""
    size = path.stat().st_size
    if size > limit:
        raise MediaRejected("too_large")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK_BYTES):
            digest.update(chunk)
    return DownloadedMedia(path=path, sha256=digest.hexdigest(), size=size, mime=mime)


def _copy_to_file(
    source: Path,
    path: Path,
    cap: ByteCap,
    kind: AttachmentKind,
    max_megapixels: int,
) -> tuple[int, str]:
    """Copy ``source`` to ``path`` under ``cap`` and return its byte count and hex SHA-256.

    A photo's chunks are written as they arrive and ``check_image_header`` runs on
    the first :data:`MEDIA_HEADER_BYTES` (or on the whole file when it is shorter);
    on rejection the caller removes the directory (BR-E07).
    """
    digest = hashlib.sha256()
    written = 0
    pending: bytearray | None = bytearray() if kind == "photo" else None
    with source.open("rb") as src, path.open("wb") as dst:
        while chunk := src.read(_CHUNK_BYTES):
            cap.add(len(chunk))
            digest.update(chunk)
            dst.write(chunk)
            written += len(chunk)
            if pending is not None:
                pending.extend(chunk)
                if len(pending) >= MEDIA_HEADER_BYTES:
                    check_image_header(bytes(pending), max_megapixels)
                    pending = None
    if pending is not None:
        check_image_header(bytes(pending), max_megapixels)
    return written, digest.hexdigest()


def console_transport() -> Transport:
    """Return an outbox transport that keeps console rows for ``rehearse replies`` (D165)."""

    async def deliver(row: OutboxRow) -> str:
        logger.debug("console transport kept outbox row %d", row.id)
        return f"console-{row.id}"

    return deliver


def console_channel(cfg: Config) -> ConsoleChannel | None:
    """Return the configured console channel, if any (D165)."""
    return next((channel for channel in cfg.channels if isinstance(channel, ConsoleChannel)), None)


if TYPE_CHECKING:
    _: MediaSource = ConsoleMediaSource(Path())
