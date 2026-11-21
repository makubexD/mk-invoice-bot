"""Rehearse the live pipeline through the dev-only console channel (D165).

``rehearse_send`` turns one CLI invocation (a file on stdin, a text or a button
press) into a canonical console message and runs it through the same intake the
webhook uses. Outbound replies stay in the outbox for ``rehearse replies``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from pydantic import ValidationError

from invoicebot.channels.base import Attachment, AttachmentKind, Button, InboundMessage, OutboundMessage
from invoicebot.channels.console import (
    CONSOLE,
    MEDIA_BY_SUFFIX,
    MEDIA_REF_MAX_CHARS,
    console_channel,
    inbox_dir,
)
from invoicebot.channels.media_limits import DIRECTORY_MODE, max_bytes_for
from invoicebot.channels.profiles import sender_role
from invoicebot.config.models import Config, ConsoleChannel
from invoicebot.pipeline.intake import IntakeResult, IntakeSettings, handle_inbound
from invoicebot.queue.outbox import OutboxRow, rows_after
from invoicebot.store.db import create_pool
from invoicebot.text import replace_unsafe_chars


class RehearseError(Exception):
    """A ``rehearse`` command refused to run; the CLI turns it into exit 2 (D165)."""


#: The refusal both ``rehearse`` commands give when the config lists no console channel.
_NO_CONSOLE_CHANNEL = "console channel not configured (D165)"
#: What ``rehearse replies`` shows for a console row whose payload is not an outbound message.
INVALID_PAYLOAD_TEXT = "<invalid payload>"
#: Characters in the uuid hex that prefixes every inbox file name.
_UUID_HEX_CHARS = 32

logger = logging.getLogger(__name__)


def require_console(cfg: Config) -> ConsoleChannel:
    """Return the config's console channel, refusing when it lists none (D165)."""
    channel = console_channel(cfg)
    if channel is None:
        raise RehearseError(_NO_CONSOLE_CHANNEL)
    return channel


@dataclass(frozen=True)
class RehearseSend:
    """One ``rehearse send`` invocation: a file name, a text or a button id (D165).

    A request never names both a file and a button: a file carries a caption text,
    but a button press cannot also arrive with the file it would answer (D165).

    Raises:
        RehearseError: a request names both ``name`` and ``button``.
    """

    name: str | None = None
    text: str | None = None
    button: str | None = None

    def __post_init__(self) -> None:
        if self.name is not None and self.button is not None:
            raise RehearseError("rehearse send cannot combine --name and --button (D165)")


@dataclass(frozen=True)
class ConsoleReply:
    """One console outbox row as ``rehearse replies`` shows it (D165)."""

    id: int
    rule_id: str | None
    status: str
    text: str
    buttons: list[Button]


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
    When intake creates no job for the file, or raises, the inbox file is removed
    again so a refused send leaves nothing behind (D165).

    Raises:
        RehearseError: the config has no console channel, a file name's extension is
            unknown, or the file's bytes are over their cap.
    """
    channel = require_console(cfg)
    attachment: Attachment | None = None
    if request.name is not None:
        attachment = await _read_attachment(request.name, cfg, data_dir, stdin)
    message = InboundMessage(
        channel=CONSOLE,
        external_id=f"console-{uuid.uuid4().hex}",
        profile_id=channel.profile,
        sender_role=sender_role(channel.profile, cfg.owner.profile, intake.roles.admins),
        text=replace_unsafe_chars(request.text or ""),
        attachments=[attachment] if attachment is not None else [],
        button_id=request.button,
        # The sender-side send time, as a channel's own timestamp is (only touch_window reads it).
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
    except BaseException:
        await _discard_inbox(data_dir, attachment)
        raise
    finally:
        await pool.close()
    if attachment is not None and result.invoice_id is None:
        # shortcut: a stored result with an invoice id is assumed to consume the file.
        await _discard_inbox(data_dir, attachment)
    return result


async def _discard_inbox(data_dir: Path, attachment: Attachment | None) -> None:
    """Remove the inbox file when no download or attach job will consume it (D165)."""
    if attachment is None:
        return
    path = inbox_dir(data_dir) / attachment.media_ref
    await asyncio.to_thread(path.unlink, missing_ok=True)


async def rehearse_replies(database_url: str, after_id: int) -> list[ConsoleReply]:
    """Read the console outbox rows above ``after_id`` and return them for printing (D165).

    Each payload is validated as an :class:`OutboundMessage` before it is shown, so the
    command never prints a row the pipeline could not have sent; a row that fails shows
    as :data:`INVALID_PAYLOAD_TEXT`, so one bad row never blocks the cursor (D165).
    """
    pool = create_pool(database_url)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            rows = await rows_after(conn, CONSOLE, after_id)
    finally:
        await pool.close()
    return [_console_reply(row) for row in rows]


def _console_reply(row: OutboxRow) -> ConsoleReply:
    """Validate one console row's payload and turn it into a printable reply (D165)."""
    try:
        message = OutboundMessage.model_validate(row.payload)
    except ValidationError:
        logger.warning("console outbox row %d has an invalid payload", row.id)
        return ConsoleReply(id=row.id, rule_id=None, status=row.state, text=INVALID_PAYLOAD_TEXT, buttons=[])
    return ConsoleReply(
        id=row.id,
        rule_id=message.rule_id,
        status=row.state,
        text=message.text,
        buttons=message.buttons,
    )


def format_replies(replies: Sequence[ConsoleReply], after_id: int) -> list[str]:
    """Render replies for the terminal and the cursor for the next call (D165).

    Each reply shows as ``#<id> <rule id or -> <status>``, then its text lines, then
    one ``[<id>] <title>`` line per button. The last line asks for the next call after
    the newest reply, or after ``after_id`` when there are none. Text lines are
    cleaned per line, so a newline between them stays and no control character from
    invoice data reaches the terminal (R-12.8).
    """
    lines: list[str] = []
    for reply in replies:
        lines.append(f"#{reply.id} {reply.rule_id or '-'} {reply.status}")
        lines.extend(replace_unsafe_chars(line) for line in reply.text.splitlines())
        lines.extend(
            f"[{replace_unsafe_chars(button.id)}] {replace_unsafe_chars(button.title)}"
            for button in reply.buttons
        )
    last_id = replies[-1].id if replies else after_id
    lines.append(f"next: --after {last_id}")
    return lines


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
    media = MEDIA_BY_SUFFIX.get(suffix)
    if media is None:
        raise RehearseError(f"unsupported file type: {name!r} (D165)")
    return media


def _media_ref(name: str) -> str:
    """Return a unique inbox file name that satisfies the console media pattern (D165).

    The name keeps its extension and a sanitized, truncated stem, prefixed with a fresh
    UUID so two sends never collide.
    """
    suffix = Path(name).suffix.lower()
    stem = Path(name).stem
    allowed = "".join(char for char in stem if char.isascii() and (char.isalnum() or char in "._-"))
    max_stem = MEDIA_REF_MAX_CHARS - _UUID_HEX_CHARS - 1 - len(suffix)
    stem = allowed[: max(1, max_stem)].rstrip("._-")
    return f"{uuid.uuid4().hex}-{stem or 'file'}{suffix}"


def _write_inbox(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` (0600) under a 0700 inbox directory (D165).

    The mode is set on every call, so an inbox made earlier with a looser umask is
    tightened too.
    """
    path.parent.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
    path.parent.chmod(DIRECTORY_MODE)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
