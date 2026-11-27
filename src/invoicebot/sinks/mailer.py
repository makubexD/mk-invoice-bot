"""The invoice email and the modes that deliver it (R-7.6, R-7.7, D69, D89).

A mode either hands the mail to Graph, which sends it once, or writes the
``.eml`` for the owner to send by hand. When ``mail.send_enabled`` is false
every mode is replaced by the assisted one at startup (R-7.7).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from invoicebot.config.models import Config, MailerSink
from invoicebot.sinks.base import Attachment, Drive, OutgoingMail
from invoicebot.sinks.eml import build_eml
from invoicebot.sinks.expr import render
from invoicebot.sinks.laptop_job import LaptopJob, job_document

logger = logging.getLogger(__name__)

#: Directory under the data directory that holds the assisted modes' drafts.
MAIL_DIRECTORY = "mail"

#: Directory under the OneDrive root that holds the laptop agent's jobs and receipts (R-7.14).
OUTBOX_DIRECTORY = "_outbox"

#: How a mail mode completes, which picks the handler that files it (D108).
#: ``accepted`` when the provider takes the mail, ``receipt`` when a later receipt
#: confirms it, and ``tap`` when the owner sends the draft by hand.
type Completion = Literal["accepted", "receipt", "tap"]


def build_mail(
    sink: MailerSink,
    values: Mapping[str, object],
    files: Mapping[str, Attachment],
) -> OutgoingMail:
    """Build the mail of one invoice from the sink's templates (R-7.6, D69).

    Args:
        sink: the flow's mailer sink: recipients, templates and attachment order.
        values: the flow's step-6 names the subject and body render from.
        files: the attachment of each ``excel_template``, ``pdf`` and ``xml`` kind.

    Returns:
        The rendered mail, with its attachments in ``sink.attach`` order.

    Raises:
        ValueError: ``sink.attach`` names an attachment ``files`` does not hold.
    """
    attachments: list[Attachment] = []
    for kind in sink.attach:
        if kind not in files:
            raise ValueError(f"missing attachment {kind!r}")
        attachments.append(files[kind])
    return OutgoingMail(
        to=tuple(sink.to),
        cc=tuple(sink.cc),
        subject=render(sink.subject, values),
        body=render(sink.body, values),
        attachments=tuple(attachments),
    )


class Mailer(Protocol):
    """The sender a real mode needs; :class:`GraphMailer` implements it."""

    async def send(self, mail: OutgoingMail) -> None:
        """Send ``mail`` once, raising when the delivery fails."""
        ...


class MailMode(Protocol):
    """One way of delivering an invoice email (R-7.7, D89, D108).

    ``completion`` says how the mode completes and picks its delivery handler;
    ``done_rule`` is the rule that reports the completed invoice. An accepted
    mode sends exactly once per call; the caller commits the batch as
    ``sending`` before the call, so a retry never sends it again (D89).
    """

    completion: Completion
    done_rule: str

    async def deliver(self, invoice_id: int, mail: OutgoingMail, folder_path: str) -> str | None:
        """Deliver ``mail`` for ``invoice_id``.

        Args:
            invoice_id: the invoice the mail belongs to.
            mail: the rendered mail to deliver.
            folder_path: the invoice's folder, absolute from the drive root.

        Returns:
            The ``.eml`` path for an assisted mode, the job id for a receipt mode,
            ``None`` when the mode sent it.
        """
        ...


class DryRun:
    """The assisted mode: write the ``.eml`` the owner sends (R-7.7, D69, D108)."""

    completion: Completion = "tap"
    done_rule: str = "BR-D08"

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    async def deliver(self, invoice_id: int, mail: OutgoingMail, _folder_path: str) -> str | None:
        """Write ``build_eml(mail)`` to ``<data_dir>/mail/<invoice_id>.eml`` (D69).

        An existing file is replaced, so an overridden mail rewrites it.

        Returns:
            The path of the written ``.eml``.
        """
        path = self._data_dir / MAIL_DIRECTORY / f"{invoice_id}.eml"
        data = build_eml(mail)
        await asyncio.to_thread(_write_eml, path, data)
        logger.info("mail draft written", extra={"invoice_id": invoice_id, "size_bytes": len(data)})
        return str(path)


class MailerSend:
    """The automatic modes: hand the mail to the injected mailer, which sends it once (R-7.6, D89, D108).

    The ``graph`` and ``smtp`` modes both use it (D205).
    """

    completion: Completion = "accepted"
    done_rule: str = "BR-D10"

    def __init__(self, mailer: Mailer) -> None:
        self._mailer = mailer

    async def deliver(self, _invoice_id: int, mail: OutgoingMail, _folder_path: str) -> None:
        """Send ``mail`` through the injected mailer, once per call (D89)."""
        await self._mailer.send(mail)


class InboxRule:
    """The ``inbox_rule`` mode: hand the mail to the owner's Outlook rule (R-7.7, D108).

    The bot's mailbox sends the mail to the owner's work address only; the
    Outlook rule then forwards it. Recipients are replaced, never added to, so
    the sink's HR addresses never reach Graph in this mode.
    """

    completion: Completion = "accepted"
    done_rule: str = "BR-D13"

    def __init__(self, mailer: Mailer, owner_address: str) -> None:
        self._mailer = mailer
        self._owner_address = owner_address

    async def deliver(self, _invoice_id: int, mail: OutgoingMail, _folder_path: str) -> None:
        """Send ``mail`` to the owner's work address through the injected mailer (R-7.7, D108)."""
        await self._mailer.send(replace(mail, to=(self._owner_address,), cc=()))


class LaptopOutlook:
    """The ``laptop_outlook`` mode: write a signed job for the laptop agent (R-7.14, D108, D111).

    The job goes to ``<root>/_outbox/`` on the personal OneDrive, where the agent
    sends the mail through Outlook classic and writes a receipt back (D108). Its
    HMAC keeps every other program from making the laptop send.
    """

    completion: Completion = "receipt"
    done_rule: str = "BR-D10"

    def __init__(self, drive: Drive, root: str, key: bytes, clock: Callable[[], datetime]) -> None:
        self._drive = drive
        self._root = root
        self._key = key
        self._clock = clock

    async def deliver(self, invoice_id: int, mail: OutgoingMail, folder_path: str) -> str:
        """Upload the signed job for ``mail`` and return its id (R-7.14, D111).

        The job carries ``folder_path`` relative to the drive root, and its
        attachments are named inside that folder, so the agent never reads outside it.

        Returns:
            The job id the receipt reports back.
        """
        job_id = uuid.uuid4().hex
        job = LaptopJob(
            job_id=job_id,
            created_at=self._clock(),
            invoice_id=invoice_id,
            folder=folder_path.lstrip("/"),
            to=mail.to,
            subject=mail.subject,
            body=mail.body,
            attachments=tuple(attachment.name for attachment in mail.attachments),
        )
        await self._drive.upload(
            f"{self._root}/{OUTBOX_DIRECTORY}/{job_id}.json", job_document(job, self._key)
        )
        logger.info("laptop job written", extra={"invoice_id": invoice_id, "job_id": job_id})
        return job_id


def mail_modes(
    cfg: Config,
    dry_run: DryRun,
    graph: MailerSend,
    bot_mailer: Mailer | None,
    laptop: LaptopOutlook | None,
    smtp: MailerSend | None,
) -> Mapping[str, MailMode]:
    """Return the configured modes, closing real sending when it is off (R-7.7, D89).

    With ``mail.send_enabled`` false, ``graph``, ``smtp``, ``inbox_rule`` and
    ``laptop_outlook`` map to the dry run, so no mode can send real mail outside
    production. ``inbox_rule`` is available only when ``mail.owner_work_address``
    and ``bot_mailer`` are set, and it sends through the bot mailbox's mailer (D108).
    ``laptop_outlook`` is available only when ``laptop`` is given (R-7.14), and
    ``smtp`` only when ``smtp`` is given (D205).
    """
    active_graph: MailMode = graph if cfg.mail.send_enabled else dry_run
    modes: dict[str, MailMode] = {"dry_run": dry_run, "graph": active_graph}
    owner_address = cfg.mail.owner_work_address
    if owner_address is not None and bot_mailer is not None:
        modes["inbox_rule"] = InboxRule(bot_mailer, owner_address) if cfg.mail.send_enabled else dry_run
    if laptop is not None:
        modes["laptop_outlook"] = laptop if cfg.mail.send_enabled else dry_run
    if smtp is not None:
        modes["smtp"] = smtp if cfg.mail.send_enabled else dry_run
    return modes


def _write_eml(path: Path, data: bytes) -> None:
    """Create ``path``'s directory and write ``data`` over any existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
