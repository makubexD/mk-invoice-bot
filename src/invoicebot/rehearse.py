"""Rehearse the live pipeline through the dev-only console channel (D165).

``rehearse_send`` turns one CLI invocation (a file on stdin, a text or a button
press) into a canonical console message and runs it through the same intake the
webhook uses. Outbound replies stay in the outbox for ``rehearse replies``.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from invoicebot.channels.base import Attachment, AttachmentKind, InboundMessage
from invoicebot.channels.console import (
    CONSOLE,
    MEDIA_REF_MAX_CHARS,
    MIME_BY_SUFFIX,
    console_channel,
    inbox_dir,
)
from invoicebot.channels.media_limits import max_bytes_for
from invoicebot.channels.profiles import sender_role
from invoicebot.config.models import Config
from invoicebot.pipeline.intake import IntakeResult, IntakeSettings, handle_inbound
from invoicebot.store.db import create_pool

#: The attachment kind a ``rehearse send`` file name's extension produces (D165).
_KIND_BY_SUFFIX: dict[str, AttachmentKind] = {
    ".jpg": "photo",
    ".jpeg": "photo",
    ".png": "photo",
    ".pdf": "pdf",
    ".xml": "xml",
}


class RehearseError(Exception):
    """A ``rehearse`` command refused to run; the CLI turns it into exit 2 (D165)."""


@dataclass(frozen=True)
class RehearseSend:
    """One ``rehearse send`` invocation: a file name, a text or a button id (D165)."""

    name: str | None = None
    text: str | None = None
    button: str | None = None


async def rehearse_send(
    data_dir: Path,
    database_url: str,
    cfg: Config,
    intake: IntakeSettings,
    request: RehearseSend,
    stdin: BinaryIO,
) -> IntakeResult:
    """Feed one console message through the live intake and return its result (D165).

    A file named by ``request.name`` is read from ``stdin`` up to its kind's cap and
    written to the inbox before intake; a text or button runs without a file. The
    message is handled in one pooled connection, so intake keeps its own transaction.

    Raises:
        RehearseError: the config has no console channel, a file name's extension is
            unknown, or the file's bytes are over their cap.
    """
    channel = console_channel(cfg)
    if channel is None:
        raise RehearseError("console channel not configured (D165)")
    attachment: Attachment | None = None
    if request.name is not None:
        attachment = await _read_attachment(request.name, cfg, data_dir, stdin)
    message = InboundMessage(
        channel=CONSOLE,
        external_id=f"console-{uuid.uuid4().hex}",
        profile_id=channel.profile,
        sender_role=sender_role(channel.profile, cfg.owner.profile, intake.roles.admins),
        text=request.text or "",
        attachments=[attachment] if attachment is not None else [],
        button_id=request.button,
        received_at=datetime.now(UTC),
    )
    pool = create_pool(database_url)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            result = await handle_inbound(
                conn,
                message,
                max_queued_per_sender=intake.max_queued_per_sender,
                media_max_bytes=intake.media_max_bytes,
                timezone=intake.timezone,
                roles=intake.roles,
            )
    finally:
        await pool.close()
    return result


async def _read_attachment(name: str, cfg: Config, data_dir: Path, stdin: BinaryIO) -> Attachment:
    """Read one inbox file from stdin and return its attachment (D165).

    Raises:
        RehearseError: the extension is unknown or the bytes are over their cap.
    """
    kind, mime = _kind_and_mime(name)
    limit = max_bytes_for(kind, cfg.limits.media)
    data = await asyncio.to_thread(stdin.read, limit + 1)
    if len(data) > limit:
        raise RehearseError(f"{name!r} is over the {kind} cap ({limit} bytes) (D165)")
    media_ref = _media_ref(name)
    await asyncio.to_thread(_write_inbox, inbox_dir(data_dir) / media_ref, data)
    return Attachment(kind=kind, media_ref=media_ref, mime=mime, size=len(data))


def _kind_and_mime(name: str) -> tuple[AttachmentKind, str]:
    """Return the attachment kind and mime a file name's extension gives (D165).

    Raises:
        RehearseError: the extension is not a supported media type.
    """
    suffix = Path(name).suffix.lower()
    kind = _KIND_BY_SUFFIX.get(suffix)
    mime = MIME_BY_SUFFIX.get(suffix)
    if kind is None or mime is None:
        raise RehearseError(f"unsupported file type: {name!r} (D165)")
    return kind, mime


def _media_ref(name: str) -> str:
    """Return a unique inbox file name that satisfies the console media pattern (D165).

    The name keeps its extension and a sanitized, truncated stem, prefixed with a fresh
    UUID so two sends never collide.
    """
    suffix = Path(name).suffix.lower()
    stem = Path(name).stem
    allowed = "".join(char for char in stem if char.isascii() and (char.isalnum() or char in "._-"))
    max_stem = MEDIA_REF_MAX_CHARS - 32 - 1 - len(suffix)
    stem = allowed[: max(1, max_stem)].rstrip("._-")
    return f"{uuid.uuid4().hex}-{stem or 'file'}{suffix}"


def _write_inbox(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` (0600) under a 0700 inbox directory (D165)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
