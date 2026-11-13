"""The field model every reader produces (R-2.6, R-2.13).

A :class:`Field` is one extracted value with its source and confidence; a :class:`Reading`
is what one reader produced for an invoice, plus the URL when the QR carried one (R-2.8).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

Source = Literal["qr", "xml", "ocr", "padron", "llm", "user"]

REQUIRED = ("ruc_emisor", "tipo", "serie", "numero", "fecha_emision", "total")


@dataclass(frozen=True)
class Field:
    """One extracted value with where it came from and how sure the reader was (R-2.6)."""

    value: str
    source: Source
    confidence: float


@dataclass(frozen=True)
class Reading:
    """Everything one reader extracted from an invoice."""

    fields: Mapping[str, Field]
    document_url: str | None

    def missing_required(self) -> tuple[str, ...]:
        """Return the required field names (R-2.13) this reading does not carry, in order."""
        return tuple(name for name in REQUIRED if name not in self.fields)


def finite_decimal(text: str) -> Decimal | None:
    """Return ``text`` as a finite ``Decimal``, or ``None`` when it is not one.

    Extracted text is untrusted: ``NaN`` and ``Infinity`` parse as ``Decimal`` but raise on
    comparison, so every reader and validator parses amounts through this one function.
    """
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() else None
