"""Canonical fetch types and the fetcher port (S7.1, R-5.1).

A fetcher gets an invoice's official PDF and XML from one source (a portal, a
QR link, an inbox) and reports the outcome as a reason code. The chain decides
which fetcher runs next from these codes (D65), so nothing here carries page
text or raises for an expected outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

#: A UTF-8 BOM may precede the first byte of a document.
UTF8_BOM = b"\xef\xbb\xbf"
#: ASCII whitespace that may precede the first byte of a document.
ASCII_WHITESPACE = b" \t\n\r\f\v"

_PDF_MAGIC = b"%PDF-"


def is_pdf(data: bytes) -> bool:
    """Return whether ``data`` starts with a PDF header, after an optional BOM and whitespace (R-5.6)."""
    return data.removeprefix(UTF8_BOM).lstrip(ASCII_WHITESPACE).startswith(_PDF_MAGIC)


class MailboxError(Exception):
    """The mailbox answered with a refusal or broke (R-5.2)."""


class FetchReason(StrEnum):
    """Why one fetch ended; only ``OK`` carries files (S7.1)."""

    OK = "ok"
    NOT_FOUND_YET = "not_found_yet"
    #: The source answered and holds no document; final (R-5.4).
    NOT_FOUND = "not_found"
    #: The source gave no document URL to request (R-5.4).
    NO_URL = "no_url"
    #: The store has no website to fetch from; its portal is manual (S10.10, R-5.4).
    NO_WEBSITE = "no_website"
    #: The portal shows a captcha the bot cannot solve (D99, R-5.4).
    CAPTCHA_UNSOLVABLE = "captcha_unsolvable"
    #: The sender was asked to type the portal's captcha and has not answered yet (D209).
    CAPTCHA_ASKED = "captcha_asked"
    #: The portal refused the sender's captcha answer; the bot may ask again (D209).
    CAPTCHA_WRONG = "captcha_wrong"
    #: The sender did not answer the captcha in time (D209).
    CAPTCHA_TIMEOUT = "captcha_timeout"
    #: The source needs credentials the bot does not have (R-5.4).
    AUTH_REQUIRED = "auth_required"
    NO_PORTAL = "no_portal"
    PORTAL_INACTIVE = "portal_inactive"
    TECHNICAL = "technical"
    TIMEOUT = "timeout"
    GUARD_BLOCKED = "guard_blocked"
    XML_INVALID = "xml_invalid"
    XML_MISMATCH = "xml_mismatch"
    MISSING_DOCS = "missing_docs"


#: The reasons that count as a technical failure: the source broke or timed out (D167, spec 06 §3).
TECHNICAL_REASONS = frozenset({FetchReason.TECHNICAL, FetchReason.TIMEOUT})


@dataclass(frozen=True)
class FetchedFile:
    """One file a fetcher downloaded, by reference."""

    kind: Literal["pdf", "xml"]
    path: Path
    size: int
    sha256: str


@dataclass(frozen=True)
class FetchResult:
    """The outcome of one fetch attempt (R-5.1)."""

    reason: FetchReason
    files: tuple[FetchedFile, ...]
    portal_key: str | None

    @property
    def ok(self) -> bool:
        """Whether the fetch returned the flow's documents."""
        return self.reason is FetchReason.OK


@dataclass(frozen=True)
class FetchInvoice:
    """The invoice fields a fetcher needs to identify it on a portal."""

    ruc_emisor: str
    tipo: str
    serie: str
    numero: str
    fecha: date
    total: Decimal
    document_url: str | None = None


class Fetcher(Protocol):
    """One source of an invoice's official files (R-5.1)."""

    async def fetch(self, invoice: FetchInvoice, out_dir: Path) -> FetchResult:
        """Fetch ``invoice`` into ``out_dir`` and report why.

        A fetcher never raises for an expected outcome: a missing invoice, a
        timeout or a blocked request comes back as a reason code. It returns
        reason codes, never page text.
        """
        ...
