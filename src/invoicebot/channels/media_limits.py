"""Cap untrusted media before its bytes are read (R-1.11, BR-E07).

Size, pixel and zip limits come from the image header and the zip central
directory only: nothing here decodes pixel data or extracts an archive. A
rejection carries one of the reasons channels map to a chat message.
"""

from __future__ import annotations

import asyncio
import hashlib
import struct
import zipfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Literal

from invoicebot.channels.base import AttachmentKind
from invoicebot.config.models import MediaLimits, MediaMaxBytes

#: Bytes read before a photo's header is checked (R-1.4).
MEDIA_HEADER_BYTES = 512 * 1024
#: Name of a stored media file inside its own directory.
FILE_NAME = "file"
#: Mode of a stored media directory: the app user only.
DIRECTORY_MODE = 0o700

type RejectionReason = Literal[
    "too_large",
    "too_many_pixels",
    "unsupported_type",
    "zip_entries",
    "zip_ratio",
]

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_SIGNATURE_SIZE = len(_PNG_SIGNATURE)
_PNG_LENGTH_OFFSET = 8
_PNG_TYPE_OFFSET = 12
_PNG_WIDTH_OFFSET = 16
_PNG_HEIGHT_OFFSET = 20
_PNG_MIN_HEADER_BYTES = 24
_PNG_IHDR = b"IHDR"
_PNG_IHDR_LENGTH = 13

_JPEG_SOI_BYTES = b"\xff\xd8"
_JPEG_MARKER_PREFIX = 0xFF
_JPEG_STUFFED_BYTE = 0x00
_JPEG_TEM = 0x01
_JPEG_SOI = 0xD8
_JPEG_EOI = 0xD9
_JPEG_SOS = 0xDA
_JPEG_RST_FIRST = 0xD0
_JPEG_RST_LAST = 0xD7
_JPEG_SOF_FIRST = 0xC0
_JPEG_SOF_LAST = 0xCF
_JPEG_NON_SOF_MARKERS = frozenset({0xC4, 0xC8, 0xCC})
_JPEG_SOF_MARKERS = frozenset(range(_JPEG_SOF_FIRST, _JPEG_SOF_LAST + 1)) - _JPEG_NON_SOF_MARKERS
_JPEG_STANDALONE_MARKERS = frozenset({_JPEG_TEM, _JPEG_SOI, *range(_JPEG_RST_FIRST, _JPEG_RST_LAST + 1)})
_JPEG_LENGTH_SIZE = 2
_JPEG_SOF_MIN_LENGTH = 7
_JPEG_SOF_HEIGHT_OFFSET = 3
_JPEG_SOF_WIDTH_OFFSET = 5

_MEGAPIXEL = 1_000_000

_BYTES_FOR_KIND: dict[AttachmentKind, Callable[[MediaMaxBytes], int]] = {
    "photo": lambda caps: caps.image,
    "pdf": lambda caps: caps.pdf,
    "xml": lambda caps: caps.xml,
}

#: Document mime types every channel keeps, mapped to their attachment kind (R-1.4).
DOCUMENT_KINDS: dict[str, AttachmentKind] = {
    "application/pdf": "pdf",
    "text/xml": "xml",
    "application/xml": "xml",
}


class MediaRejected(Exception):
    """Media failed a size, pixel or archive check before it was read."""

    def __init__(self, reason: RejectionReason) -> None:
        self.reason = reason
        super().__init__(reason)


def image_megapixels(head: bytes) -> float | None:
    """Return the declared megapixels of a PNG or JPEG head, or ``None``.

    Only the given bytes are inspected and no allocation follows the declared
    size, so an oversized or malformed header costs no memory.
    """
    png = _png_megapixels(head)
    if png is not None:
        return png
    return _jpeg_megapixels(head)


def check_image_header(head: bytes, max_megapixels: int) -> None:
    """Reject a head that is not PNG/JPEG or declares too many pixels."""
    megapixels = image_megapixels(head)
    if megapixels is None:
        raise MediaRejected("unsupported_type")
    if megapixels > max_megapixels:
        raise MediaRejected("too_many_pixels")


class ByteCap:
    """Track bytes read so far and reject the first one past the limit."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.total = 0

    def add(self, n: int) -> None:
        """Count ``n`` more bytes, rejecting once the total exceeds the limit."""
        self.total += n
        if self.total > self.limit:
            raise MediaRejected("too_large")


def max_bytes_for(kind: AttachmentKind, limits: MediaLimits) -> int:
    """Return the configured byte cap for an attachment kind."""
    cap_for = _BYTES_FOR_KIND.get(kind)
    if cap_for is None:
        raise MediaRejected("unsupported_type")
    return cap_for(limits.max_bytes)


def document_kind(mime: str) -> AttachmentKind | None:
    """Return the attachment kind a channel's document mime type maps to, or ``None``
    when it is not kept (R-1.4).
    """
    if mime.startswith("image/"):
        return "photo"
    return DOCUMENT_KINDS.get(mime)


async def write_capped_stream(
    chunks: AsyncIterator[bytes],
    path: Path,
    cap: ByteCap,
    kind: AttachmentKind,
    limits: MediaLimits,
) -> tuple[int, str]:
    """Stream the body into ``path`` and return the bytes written and their hex SHA-256.

    A photo's first 512 KiB are held in memory until ``check_image_header``
    passes, so a rejected image never reaches the disk (BR-E07). The larger
    window keeps the SOF marker findable behind a long EXIF/APP1 segment.
    """
    digest = hashlib.sha256()
    written = 0
    pending: bytearray | None = bytearray() if kind == "photo" else None
    async for chunk in chunks:
        cap.add(len(chunk))
        digest.update(chunk)
        if pending is not None:
            pending.extend(chunk)
            if len(pending) < MEDIA_HEADER_BYTES:
                continue
            check_image_header(bytes(pending), limits.max_megapixels)
            written += len(pending)
            await asyncio.to_thread(_append_bytes, path, bytes(pending))
            pending = None
        else:
            written += len(chunk)
            await asyncio.to_thread(_append_bytes, path, chunk)
    if pending is not None:
        check_image_header(bytes(pending), limits.max_megapixels)
        written += len(pending)
        await asyncio.to_thread(_append_bytes, path, bytes(pending))
    return written, digest.hexdigest()


def _append_bytes(path: Path, data: bytes) -> None:
    """Append ``data`` to ``path``; runs in a worker thread so the loop is not blocked."""
    with path.open("ab") as file:
        file.write(data)


def check_zip(path: Path, max_entries: int, max_ratio: int) -> None:
    """Reject a zip with too many entries or too much compression.

    Only the central directory is read; no entry is extracted or decompressed.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
    except zipfile.BadZipFile as exc:
        raise MediaRejected("unsupported_type") from exc
    if len(entries) > max_entries:
        raise MediaRejected("zip_entries")
    total_size = sum(entry.file_size for entry in entries)
    total_compressed = sum(entry.compress_size for entry in entries)
    if total_size / max(total_compressed, 1) > max_ratio:
        raise MediaRejected("zip_ratio")


def _png_megapixels(head: bytes) -> float | None:
    if len(head) < _PNG_MIN_HEADER_BYTES or head[:_PNG_SIGNATURE_SIZE] != _PNG_SIGNATURE:
        return None
    chunk_length: int = struct.unpack_from(">I", head, _PNG_LENGTH_OFFSET)[0]
    if head[_PNG_TYPE_OFFSET:_PNG_WIDTH_OFFSET] != _PNG_IHDR or chunk_length != _PNG_IHDR_LENGTH:
        return None
    width: int = struct.unpack_from(">I", head, _PNG_WIDTH_OFFSET)[0]
    height: int = struct.unpack_from(">I", head, _PNG_HEIGHT_OFFSET)[0]
    return width * height / _MEGAPIXEL


def _jpeg_megapixels(head: bytes) -> float | None:
    if not head.startswith(_JPEG_SOI_BYTES):
        return None
    position = len(_JPEG_SOI_BYTES)
    while position < len(head):
        located = _jpeg_marker_at(head, position)
        if located is None:
            return None
        marker, length_offset = located
        if marker in (_JPEG_STUFFED_BYTE, _JPEG_EOI, _JPEG_SOS):
            return None
        if marker in _JPEG_STANDALONE_MARKERS:
            position = length_offset
            continue
        length = _jpeg_segment_length(head, length_offset)
        if length is None:
            return None
        if marker in _JPEG_SOF_MARKERS:
            return _jpeg_sof_megapixels(head, length_offset, length)
        position = length_offset + length
    return None


def _jpeg_marker_at(head: bytes, position: int) -> tuple[int, int] | None:
    """Return the marker byte and its length-field offset, skipping fill bytes."""
    if head[position] != _JPEG_MARKER_PREFIX:
        return None
    while position < len(head) and head[position] == _JPEG_MARKER_PREFIX:
        position += 1
    if position >= len(head):
        return None
    return head[position], position + 1


def _jpeg_segment_length(head: bytes, offset: int) -> int | None:
    if offset + _JPEG_LENGTH_SIZE > len(head):
        return None
    length: int = struct.unpack_from(">H", head, offset)[0]
    return length if length >= _JPEG_LENGTH_SIZE else None


def _jpeg_sof_megapixels(head: bytes, length_offset: int, length: int) -> float | None:
    if length < _JPEG_SOF_MIN_LENGTH or length_offset + _JPEG_SOF_MIN_LENGTH > len(head):
        return None
    height: int = struct.unpack_from(">H", head, length_offset + _JPEG_SOF_HEIGHT_OFFSET)[0]
    width: int = struct.unpack_from(">H", head, length_offset + _JPEG_SOF_WIDTH_OFFSET)[0]
    return width * height / _MEGAPIXEL
