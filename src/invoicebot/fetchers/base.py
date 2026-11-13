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


class FetchReason(StrEnum):
    """Why one fetch ended; only ``OK`` carries files (S7.1)."""

    OK = "ok"
    NOT_FOUND_YET = "not_found_yet"
    NO_PORTAL = "no_portal"
    PORTAL_INACTIVE = "portal_inactive"
    TECHNICAL = "technical"
    TIMEOUT = "timeout"
    GUARD_BLOCKED = "guard_blocked"
    XML_INVALID = "xml_invalid"
    XML_MISMATCH = "xml_mismatch"
    MISSING_DOCS = "missing_docs"


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


class Fetcher(Protocol):
    """One source of an invoice's official files (R-5.1)."""

    async def fetch(self, invoice: FetchInvoice, out_dir: Path) -> FetchResult:
        """Fetch ``invoice`` into ``out_dir`` and report why.

        A fetcher never raises for an expected outcome: a missing invoice, a
        timeout or a blocked request comes back as a reason code. It returns
        reason codes, never page text.
        """
        ...
