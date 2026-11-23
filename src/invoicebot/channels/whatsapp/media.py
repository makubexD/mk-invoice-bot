"""Download WhatsApp media under a byte cap and, for photos, a pixel cap (R-1.4, R-1.11).

Metadata comes from a fresh ``media_info`` call on every attempt, so a retried
download never reuses an expired URL. Bytes stream straight to a private
directory under ``dest_root`` and are hashed as they arrive; a rejected
download leaves nothing behind.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
from pathlib import Path

from invoicebot.channels.base import AttachmentKind, DownloadedMedia, MediaTransient, MediaUnusable
from invoicebot.channels.media_limits import (
    DIRECTORY_MODE,
    FILE_NAME,
    MEDIA_HEADER_BYTES,
    ByteCap,
    MediaRejected,
    check_image_header,
    max_bytes_for,
)
from invoicebot.channels.whatsapp.client import MediaInfo, WhatsAppClient, WhatsAppError
from invoicebot.config.models import MediaLimits

MEDIA_ID_PATTERN = r"[A-Za-z0-9_-]{1,128}"
#: Whole-download deadline; must stay below the worker lease (300 s in queue/worker.py).
DOWNLOAD_TIMEOUT_S = 120.0
#: Meta answers a retry can fix: a 5xx, or an expired token, a lost permission, a timeout
#: or a rate limit. Any other 4xx, or an error before the request, means the media is gone.
HTTP_SERVER_ERROR = 500
RETRYABLE_STATUSES = frozenset({401, 403, 408, 429})


class MediaChecksumMismatch(Exception):
    """The downloaded bytes do not match the SHA-256 media_info reported (R-1.4)."""


class WhatsAppMediaSource:
    """The ``MediaSource`` adapter over the WhatsApp client (R-1.5)."""

    def __init__(self, client: WhatsAppClient) -> None:
        self._client = client

    async def download(
        self, media_ref: str, kind: AttachmentKind, out_dir: Path, limits: MediaLimits
    ) -> DownloadedMedia:
        """Download one WhatsApp media item into ``out_dir`` (R-1.4, R-1.11).

        Raises ``MediaTransient`` when a retry may fix the failure and ``MediaUnusable``
        when this media can never be kept, each wrapping the original as ``__cause__``.
        """
        return await download_media(self._client, media_ref, kind, out_dir, limits)


async def download_media(
    client: WhatsAppClient,
    media_id: str,
    kind: AttachmentKind,
    dest_root: Path,
    limits: MediaLimits,
) -> DownloadedMedia:
    """Download one media item into ``dest_root`` and return its path, hash and size.

    Raises ``ValueError`` for a media id that could escape the destination
    directory, ``MediaRejected`` for an unsupported kind or a size over the cap,
    ``MediaTransient`` when a retry may fix a Graph API failure, and
    ``MediaUnusable`` for a checksum mismatch or a failure a retry cannot fix.
    The original exception is the ``__cause__`` of either neutral error. On any
    failure the partial file and its directory are removed.
    """
    if re.fullmatch(MEDIA_ID_PATTERN, media_id) is None:
        raise ValueError("media_id must be a plain identifier")
    limit = max_bytes_for(kind, limits)
    directory = dest_root / media_id
    path = directory / FILE_NAME
    try:
        info = await client.media_info(media_id)
        if info.size > limit:
            raise MediaRejected("too_large")
        try:
            directory.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
            path.write_bytes(b"")  # start empty, so the returned path exists even for an empty body
            async with asyncio.timeout(DOWNLOAD_TIMEOUT_S):
                size, sha256 = await _stream_to_file(client, info, path, ByteCap(limit), kind, limits)
            if info.sha256 is not None and info.sha256.lower() != sha256.lower():
                raise MediaChecksumMismatch(media_id)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
    except WhatsAppError as err:
        raise _neutral_error(err) from err
    except MediaChecksumMismatch as err:
        raise MediaUnusable("checksum_mismatch") from err
    return DownloadedMedia(path=path, sha256=sha256, size=size, mime=info.mime)


def _retryable(exc: WhatsAppError) -> bool:
    """Return whether a retry can fix the error: a server, rate, auth or timeout status (R-1.11)."""
    return exc.status >= HTTP_SERVER_ERROR or exc.status in RETRYABLE_STATUSES


def _neutral_error(exc: WhatsAppError) -> MediaTransient | MediaUnusable:
    """Map a Graph API failure onto the neutral media error the pipeline sees (R-1.11)."""
    if _retryable(exc):
        return MediaTransient(str(exc))
    return MediaUnusable(str(exc))


async def _stream_to_file(
    client: WhatsAppClient,
    info: MediaInfo,
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
    async with client.stream_media(info.url) as body:
        async for chunk in body:
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
