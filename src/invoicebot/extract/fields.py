"""The field model every reader produces (R-2.6, R-2.13).

A :class:`Field` is one extracted value with its source and confidence; a :class:`Reading`
is what one reader produced for an invoice, plus the URL when the QR carried one (R-2.8).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, cast, get_args

Source = Literal["qr", "xml", "ocr", "padron", "llm", "user"]
Flag = Literal["inferred", "conflict"]

REQUIRED = ("ruc_emisor", "tipo", "serie", "numero", "fecha_emision", "total")

#: Confidence of a value a reader took directly from the document (R-2.6).
FULL_CONFIDENCE = 1.0

#: The shapes the JSON form of a reading has; both are closed (R-2.6).
_READING_KEYS = frozenset({"fields", "document_url"})
_FIELD_KEYS = frozenset({"value", "source", "confidence"})
_FIELD_KEYS_WITH_FLAG = _FIELD_KEYS | {"flag"}
_SOURCES = frozenset(get_args(Source))
_FLAGS = frozenset(get_args(Flag))


@dataclass(frozen=True)
class Field:
    """One extracted value, its origin and confidence, and why it may need review (R-2.6, D79, D81).

    ``flag`` is ``"inferred"`` when a reader derived the value instead of reading it, and
    ``"conflict"`` when readers disagree; ``None`` means the reader found it as printed.
    """

    value: str
    source: Source
    confidence: float
    flag: Flag | None = None


@dataclass(frozen=True)
class Reading:
    """Everything one reader extracted from an invoice."""

    fields: Mapping[str, Field]
    document_url: str | None

    def missing_required(self) -> tuple[str, ...]:
        """Return the required field names (R-2.13) this reading does not carry, in order."""
        return tuple(name for name in REQUIRED if name not in self.fields)

    def value(self, name: str) -> str | None:
        """Return the text of field ``name``, or ``None`` when it is absent."""
        field = self.fields.get(name)
        return None if field is None else field.value

    def amount(self, name: str) -> Decimal | None:
        """Return field ``name`` as a finite decimal, or ``None`` when absent or not finite (R-2.6)."""
        value = self.value(name)
        return None if value is None else finite_decimal(value)

    def fecha(self) -> date | None:
        """Return ``fecha_emision`` as a date, or ``None`` when missing or not ISO (D80)."""
        value = self.value("fecha_emision")
        if value is None:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None


def reading_to_json(reading: Reading) -> dict[str, Any]:
    """Render a reading as the JSON object the helper and ``fields_json`` carry (R-2.6)."""
    return {
        "fields": {
            name: {
                "value": field.value,
                "source": field.source,
                "confidence": field.confidence,
                "flag": field.flag,
            }
            for name, field in reading.fields.items()
        },
        "document_url": reading.document_url,
    }


def reading_from_json(payload: object) -> Reading:
    """Return ``payload`` as a :class:`Reading`, or raise ValueError when it is not one.

    The payload comes from the helper process or the database, so every level is checked
    here. An error message names the offending key, never its value (R-9.16).
    """
    if not isinstance(payload, dict) or set(payload) != _READING_KEYS:
        raise ValueError("reading must be an object with exactly fields and document_url")
    raw_fields = payload["fields"]
    if not isinstance(raw_fields, dict):
        raise ValueError("reading field 'fields' must be an object")
    document_url = payload["document_url"]
    if document_url is not None and not isinstance(document_url, str):
        raise ValueError("reading field 'document_url' must be a string or null")
    return Reading(
        fields={name: _field_from_json(name, raw) for name, raw in raw_fields.items()},
        document_url=document_url,
    )


def _field_from_json(name: object, raw: object) -> Field:
    """Return one field from its JSON object, validating its name, shape and values (R-2.6)."""
    if not isinstance(name, str):
        raise ValueError("reading field name must be a string")
    if not isinstance(raw, dict) or frozenset(raw) not in (_FIELD_KEYS, _FIELD_KEYS_WITH_FLAG):
        raise ValueError(
            f"field {name!r} must be an object with exactly value, source and confidence, and optionally flag"
        )
    value = raw["value"]
    source = raw["source"]
    confidence = _confidence(raw["confidence"])
    flag = raw.get("flag")
    if not isinstance(value, str):
        raise ValueError(f"field {name!r} value must be a string")
    if not isinstance(source, str) or source not in _SOURCES:
        raise ValueError(f"field {name!r} source must be one of {sorted(_SOURCES)}")
    if confidence is None:
        raise ValueError(f"field {name!r} confidence must be a finite number from 0 to 1")
    if flag is not None and (not isinstance(flag, str) or flag not in _FLAGS):
        raise ValueError(f"field {name!r} flag must be one of {sorted(_FLAGS)} or null")
    return Field(
        value=value, source=cast(Source, source), confidence=confidence, flag=cast(Flag | None, flag)
    )


def _confidence(value: object) -> float | None:
    """Return ``value`` as a confidence in 0..1, or None when it is not a finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        return float(value) if 0 <= value <= 1 else None
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        return None
    return value


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
