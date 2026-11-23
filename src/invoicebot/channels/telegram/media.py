"""Download Telegram media under a byte cap and, for photos, a pixel cap (R-1.4, R-1.11).

``getFile`` is called on every attempt, so a retried download never reuses an
expired file path. Bytes stream straight to a private directory under
``dest_root`` and are hashed as they arrive; a rejected download leaves
nothing behind.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from invoicebot.channels.base import AttachmentKind, DownloadedMedia, MediaTransient, MediaUnusable
from invoicebot.channels.media_limits import (
    DIRECTORY_MODE,
    FILE_NAME,
    ByteCap,
    MediaRejected,
    max_bytes_for,
    write_capped_stream,
)
from invoicebot.channels.telegram.client import (
    GETFILE_MAX_BYTES,
    NOT_SENT_STATUS,
    TelegramClient,
    TelegramError,
)
from invoicebot.config.models import MediaLimits

FILE_ID_PATTERN = r"[A-Za-z0-9_-]{1,255}"
#: Whole-download deadline; must stay below the worker lease (300 s in queue/worker.py).
DOWNLOAD_TIMEOUT_S = 120.0
#: Failures a retry can fix: no answer at all (NOT_SENT_STATUS), a 5xx, a rejected token,
#: a timeout or a rate limit. Any other status means the media is gone.
HTTP_SERVER_ERROR = 500
RETRYABLE_STATUSES = frozenset({401, 408, 429})
#: The mime type each attachment kind is stored as (R-1.4).
_MIME_BY_KIND: dict[AttachmentKind, str] = {
    "photo": "image/jpeg",
    "pdf": "application/pdf",
    "xml": "application/xml",
}


class TelegramMediaSource:
    """The ``MediaSource`` adapter over the Telegram client (R-1.5)."""

    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    async def download(
        self, media_ref: str, kind: AttachmentKind, out_dir: Path, limits: MediaLimits
    ) -> DownloadedMedia:
        """Download one Telegram media item into ``out_dir`` (R-1.4, R-1.11).

        Raises ``MediaTransient`` when a retry may fix the failure and ``MediaUnusable``
        when this media can never be kept, each wrapping the original as ``__cause__``.
        """
        return await download_media(self._client, media_ref, kind, out_dir, limits)


async def download_media(
    client: TelegramClient,
    file_id: str,
    kind: AttachmentKind,
    dest_root: Path,
    limits: MediaLimits,
) -> DownloadedMedia:
    """Download one media item into ``dest_root`` and return its path, hash and size.

    Raises ``ValueError`` for a file id that could escape the destination
    directory, ``MediaRejected`` for an unsupported kind or a size over the cap,
    ``MediaTransient`` when a retry may fix a Bot API failure, and
    ``MediaUnusable`` for a failure a retry cannot fix. The original exception
    is the ``__cause__`` of either neutral error. On any failure the partial
    file and its directory are removed.
    """
    if re.fullmatch(FILE_ID_PATTERN, file_id) is None:
        raise ValueError("file_id must be a plain identifier")
    limit = min(max_bytes_for(kind, limits), GETFILE_MAX_BYTES)
    directory = dest_root / file_id
    path = directory / FILE_NAME
    try:
        info = await client.get_file(file_id)
        if info.size is not None and info.size > limit:
            raise MediaRejected("too_large")
        try:
            directory.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
            path.write_bytes(b"")  # start empty, so the returned path exists even for an empty body
            async with asyncio.timeout(DOWNLOAD_TIMEOUT_S):
                async with client.stream_file(info.file_path) as body:
                    size, sha256 = await write_capped_stream(body, path, ByteCap(limit), kind, limits)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
    except TelegramError as err:
        raise _neutral_error(err) from err
    return DownloadedMedia(path=path, sha256=sha256, size=size, mime=_MIME_BY_KIND[kind])


def _retryable(exc: TelegramError) -> bool:
    """Return whether a retry can fix the error: no answer or a retryable status (R-1.11)."""
    return (
        exc.status == NOT_SENT_STATUS or exc.status >= HTTP_SERVER_ERROR or exc.status in RETRYABLE_STATUSES
    )


def _neutral_error(exc: TelegramError) -> MediaTransient | MediaUnusable:
    """Map a Bot API failure onto the neutral media error the pipeline sees (R-1.11)."""
    if _retryable(exc):
        return MediaTransient(str(exc))
    return MediaUnusable(str(exc))
