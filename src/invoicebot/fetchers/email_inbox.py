"""The ``email_inbox`` fetcher: find an invoice's files in the flow's mailbox (S11.9, R-5.2).

The mailbox is searched by serie; only recent messages with a matching serie-numero
attachment are looked at. Each XML download is capped and tried in turn, and the first
one that matches the invoice wins. The PDF beside that XML is taken too. The fetcher
never raises for an expected outcome: a missing mail or a sign-in need is a reason code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal, Protocol

import httpx

from invoicebot.auth.msal_cache import AuthRequired
from invoicebot.config.models import MediaMaxBytes
from invoicebot.extract.ubl import XmlRejected, matches_invoice, parse_invoice_xml
from invoicebot.fetchers.base import FetchedFile, FetchInvoice, FetchReason, FetchResult
from invoicebot.fetchers.graph_mail import MailAttachment, MailMessage
from invoicebot.fetchers.portal import invoice_reading, sha256_hex
from invoicebot.sinks.onedrive import GraphError, GraphTransient

#: The name this fetcher uses in a flow's ``fetchers`` list (R-5.2).
EMAIL_INBOX_FETCHER = "email_inbox"
#: The newest messages Graph is asked for; the invoice's own mail is near the top (S11.9).
SEARCH_TOP = 25
#: XML attachments downloaded per fetch before the fetcher gives up (S11.9).
MAX_XML_DOWNLOADS = 5
#: How far before the invoice date a message may have arrived (S11.9).
DATE_SLACK = timedelta(days=1)


class Mailbox(Protocol):
    """One flow's mailbox, read through an injected client (S11.9, R-5.2)."""

    async def search(self, term: str, top: int) -> list[MailMessage]:
        """Return up to ``top`` recent messages with attachments that match ``term``."""
        ...

    async def attachments(self, message_id: str) -> list[MailAttachment]:
        """Return the file attachments of ``message_id``."""
        ...

    async def download(self, message_id: str, attachment_id: str, max_bytes: int) -> bytes:
        """Return one attachment's bytes.

        Raises:
            ValueError: the attachment is larger than ``max_bytes``; the fetcher skips it.
        """
        ...


@dataclass(frozen=True)
class _Match:
    """The message whose XML matched, with the attachments already listed for it."""

    message: MailMessage
    attachment: MailAttachment
    data: bytes
    attachments: tuple[MailAttachment, ...]


class EmailInboxFetcher:
    """Find an invoice's XML and PDF in a flow's mailbox (S11.9, R-5.2)."""

    def __init__(self, mailbox: Mailbox, max_bytes: MediaMaxBytes) -> None:
        self._mailbox = mailbox
        self._max_bytes = max_bytes

    async def fetch(self, invoice: FetchInvoice, out_dir: Path) -> FetchResult:
        """Fetch ``invoice`` from the mailbox and report why (S11.9, R-5.2)."""
        try:
            return await self._fetch(invoice, out_dir)
        except AuthRequired:
            return _result(FetchReason.AUTH_REQUIRED)
        except (GraphTransient, GraphError, httpx.HTTPError):
            return _result(FetchReason.TECHNICAL)

    async def _fetch(self, invoice: FetchInvoice, out_dir: Path) -> FetchResult:
        """Search, match and write the files, letting the mailbox's errors escape."""
        messages = await self._mailbox.search(invoice.serie, SEARCH_TOP)
        pattern = _name_pattern(invoice)
        match, reason = await self._find_xml(_newest_first(messages, invoice.fecha), invoice, pattern)
        if match is None:
            return _result(reason if reason is not None else FetchReason.NOT_FOUND_YET)
        return await self._ok_result(match, pattern, out_dir)

    async def _find_xml(
        self,
        messages: list[MailMessage],
        invoice: FetchInvoice,
        pattern: re.Pattern[str],
    ) -> tuple[_Match | None, FetchReason | None]:
        """Return the first XML that matches, or the last rejection and why (R-5.6)."""
        downloaded = 0
        last_reason: FetchReason | None = None
        for message in messages:
            if downloaded >= MAX_XML_DOWNLOADS:
                break
            match, used, reason = await self._xml_in_message(
                message, invoice, pattern, MAX_XML_DOWNLOADS - downloaded
            )
            downloaded += used
            if reason is not None:
                last_reason = reason
            if match is not None:
                return match, None
        return None, last_reason

    async def _xml_in_message(
        self,
        message: MailMessage,
        invoice: FetchInvoice,
        pattern: re.Pattern[str],
        remaining: int,
    ) -> tuple[_Match | None, int, FetchReason | None]:
        """Try one message's XML attachments, returning its match, download count and last reason."""
        attachments = await self._mailbox.attachments(message.id)
        downloaded = 0
        last_reason: FetchReason | None = None
        for attachment in attachments:
            if not _matches(attachment, ".xml", pattern):
                continue
            if downloaded >= remaining:
                break
            if attachment.size > self._max_bytes.xml:
                continue
            data = await _download(self._mailbox, message.id, attachment, self._max_bytes.xml)
            if data is None:
                continue
            downloaded += 1
            reason = _xml_outcome(data, invoice)
            if reason is None:
                return _Match(message, attachment, data, tuple(attachments)), downloaded, None
            last_reason = reason
        return None, downloaded, last_reason

    async def _ok_result(
        self,
        match: _Match,
        pattern: re.Pattern[str],
        out_dir: Path,
    ) -> FetchResult:
        """Write the matching XML and, when usable, the PDF beside it (R-5.6)."""
        files = [_write("xml", match.data, out_dir)]
        pdf = await self._pdf(match, pattern)
        if pdf is not None:
            files.append(_write("pdf", pdf, out_dir))
        return FetchResult(FetchReason.OK, tuple(files), None)

    async def _pdf(self, match: _Match, pattern: re.Pattern[str]) -> bytes | None:
        """Return the PDF that belongs with the matching XML, or ``None`` (R-5.6)."""
        pdfs = [attachment for attachment in match.attachments if _matches(attachment, ".pdf", pattern)]
        if not pdfs:
            only = [attachment for attachment in match.attachments if _is_pdf(attachment)]
            if len(only) != 1:
                return None
            pdfs = only
        attachment = pdfs[0]
        if attachment.size > self._max_bytes.pdf:
            return None
        return await _download(self._mailbox, match.message.id, attachment, self._max_bytes.pdf)


def _name_pattern(invoice: FetchInvoice) -> re.Pattern[str]:
    """Compile the serie-numero name match once per fetch (S11.9)."""
    return re.compile(rf"{re.escape(invoice.serie.upper())}-0*{int(invoice.numero)}(?!\d)")


def _newest_first(messages: list[MailMessage], fecha: date) -> list[MailMessage]:
    """Return the messages received no earlier than ``fecha`` minus a day, newest first (S11.9)."""
    earliest = fecha - DATE_SLACK
    recent = [message for message in messages if message.received_at.date() >= earliest]
    return sorted(recent, key=lambda message: message.received_at, reverse=True)


def _matches(attachment: MailAttachment, extension: str, pattern: re.Pattern[str]) -> bool:
    """Return whether the attachment has ``extension`` and its name matches the invoice (S11.9)."""
    return attachment.name.lower().endswith(extension) and pattern.search(attachment.name.upper()) is not None


def _is_pdf(attachment: MailAttachment) -> bool:
    """Return whether the attachment is a PDF by its name's extension, case-insensitively."""
    return attachment.name.lower().endswith(".pdf")


async def _download(
    mailbox: Mailbox,
    message_id: str,
    attachment: MailAttachment,
    max_bytes: int,
) -> bytes | None:
    """Return the attachment's bytes, or ``None`` when it is over ``max_bytes`` (R-5.6)."""
    try:
        return await mailbox.download(message_id, attachment.id, max_bytes)
    except ValueError:
        return None


def _xml_outcome(data: bytes, invoice: FetchInvoice) -> FetchReason | None:
    """Return why the XML is unusable, or ``None`` when it matches the invoice (R-5.6)."""
    try:
        reading = parse_invoice_xml(data)
    except XmlRejected:
        return FetchReason.XML_INVALID
    if not matches_invoice(reading, invoice_reading(invoice)):
        return FetchReason.XML_MISMATCH
    return None


def _write(kind: Literal["pdf", "xml"], data: bytes, out_dir: Path) -> FetchedFile:
    """Write ``data`` as ``out_dir/<kind>.<kind>`` and return it with its digest (R-5.6)."""
    path = out_dir / f"{kind}.{kind}"
    path.write_bytes(data)
    return FetchedFile(kind, path, len(data), sha256_hex(path))


def _result(reason: FetchReason) -> FetchResult:
    """Return a result that carries no files."""
    return FetchResult(reason, (), None)
