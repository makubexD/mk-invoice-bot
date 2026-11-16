"""The invoice email and the modes that deliver it (R-7.6, R-7.7, D69, D89).

A mode either hands the mail to Graph, which sends it once, or writes the
``.eml`` for the owner to send by hand. When ``mail.send_enabled`` is false
every mode is replaced by the assisted one at startup (R-7.7).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Protocol

from invoicebot.config.models import Config, MailerSink
from invoicebot.sinks.base import Attachment, OutgoingMail
from invoicebot.sinks.eml import build_eml
from invoicebot.sinks.expr import render

logger = logging.getLogger(__name__)

#: Directory under the data directory that holds the assisted modes' drafts.
MAIL_DIRECTORY = "mail"

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

    async def deliver(self, invoice_id: int, mail: OutgoingMail) -> str | None:
        """Deliver ``mail`` for ``invoice_id``.

        Returns:
            The ``.eml`` path for an assisted mode, ``None`` when the mode sent it.
        """
        ...


class DryRun:
    """The assisted mode: write the ``.eml`` the owner sends (R-7.7, D69, D108)."""

    completion: Completion = "tap"
    done_rule: str = "BR-D08"

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    async def deliver(self, invoice_id: int, mail: OutgoingMail) -> str | None:
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


class GraphSend:
    """The automatic mode: hand the mail to Graph, which sends it once (R-7.6, D89, D108)."""

    completion: Completion = "accepted"
    done_rule: str = "BR-D10"

    def __init__(self, mailer: Mailer) -> None:
        self._mailer = mailer

    async def deliver(self, _invoice_id: int, mail: OutgoingMail) -> None:
        """Send ``mail`` through the injected mailer, once per call (D89)."""
        await self._mailer.send(mail)


def mail_modes(cfg: Config, dry_run: DryRun, graph: GraphSend) -> Mapping[str, MailMode]:
    """Return the configured modes, closing real sending when it is off (R-7.7, D89).

    With ``mail.send_enabled`` false, ``graph`` maps to the dry run, so no mode
    can send real mail outside production.
    """
    if not cfg.mail.send_enabled:
        return {"dry_run": dry_run, "graph": dry_run}
    return {"dry_run": dry_run, "graph": graph}


def _write_eml(path: Path, data: bytes) -> None:
    """Create ``path``'s directory and write ``data`` over any existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
