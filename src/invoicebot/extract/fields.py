"""The field model every reader produces (R-2.6, R-2.13).

A :class:`Field` is one extracted value with its source and confidence; a :class:`Reading`
is what one reader produced for an invoice, plus the URL when the QR carried one (R-2.8).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, cast, get_args

from invoicebot.text import replace_unsafe_chars

Source = Literal["qr", "xml", "ocr", "pdf", "padron", "llm", "user"]
Flag = Literal["inferred", "conflict"]

#: The buyer hints OCR gives when it reads no valid buyer RUC: a RUC failing its check digit and the
#: name after the buyer label (D213).
UNCHECKED_BUYER_RUC = "num_doc_adq_unchecked"
BUYER_NAME = "nombre_adq"

REQUIRED = ("ruc_emisor", "tipo", "serie", "numero", "fecha_emision", "total")

#: The field names whose values compare as amounts.
AMOUNT_FIELDS = frozenset({"total", "base", "igv", "service_charge", "icbper", "discounts"})

#: Confidence of a value a reader took directly from the document (R-2.6).
FULL_CONFIDENCE = 1.0

#: Most characters a printed URL may have; a longer one is not kept (D189).
MAX_PRINTED_URL_CHARS = 200
#: The characters a printed URL's path may hold: the ASCII ones URLs allow (RFC 3986), so no
#: bidi control, zero-width or other non-ASCII character reaches a message in the bot's voice (D189).
PRINTED_URL_PATH_CHARS = r"[A-Za-z0-9\-._~%!$&'()*+,;=:@/?#]"
#: A printed URL as a reading stores it: https, a lowercase ASCII host, an optional path (D189).
_PRINTED_URL_SHAPE = re.compile(rf"https://[a-z0-9.-]+(?:/{PRINTED_URL_PATH_CHARS}*)?")

#: The label prefix of an internationalized (punycode) host, refused so a lookalike of a
#: known domain is never offered (D189).
PUNYCODE_PREFIX = "xn--"
#: A host needs a dot, so at least two labels, and a 2-24 letter top level (D189).
HOST_MIN_LABELS = 2
TLD_MIN_CHARS = 2
TLD_MAX_CHARS = 24

#: Most characters the re-read line under a consult anchor may have (D191).
MAX_URL_STRIP_CHARS = 120
#: The characters a URL strip may hold: the URL ones a portal guess needs, plus a space (D191).
_URL_STRIP_SHAPE = re.compile(r"[A-Za-z0-9.:/_~% -]+")

#: The shapes the JSON form of a reading has; all are closed (R-2.6, D189, D191).
_READING_KEYS = frozenset({"fields", "document_url"})
_READING_OPTIONAL_KEYS = frozenset({"printed_url", "url_strip"})
_FIELD_KEYS = frozenset({"value", "source", "confidence"})
_FIELD_KEYS_WITH_FLAG = _FIELD_KEYS | {"flag"}
_SOURCES = frozenset(get_args(Source))
_FLAGS = frozenset(get_args(Flag))


@dataclass(frozen=True)
class Field:
    """One extracted value, its origin and confidence, and why it may need review (R-2.6, D79, D81).

    ``flag`` is ``"inferred"`` when a reader derived the value instead of reading it, and
    ``"conflict"`` when readers disagree; ``None`` means the reader found it as printed.

    ``value`` never carries an unsafe character (R-12.8): lxml, the ``.eml`` builder and
    the drive path reject them, so each is replaced with a space and the result is stripped.
    """

    value: str
    source: Source
    confidence: float
    flag: Flag | None = None

    def __post_init__(self) -> None:
        """Replace every unsafe character with a space and strip the edges (R-12.8)."""
        object.__setattr__(self, "value", replace_unsafe_chars(self.value))


@dataclass(frozen=True)
class Reading:
    """Everything one reader extracted from an invoice."""

    fields: Mapping[str, Field]
    document_url: str | None
    printed_url: str | None = None
    url_strip: str | None = None

    def missing_required(self) -> tuple[str, ...]:
        """Return the required field names (R-2.13) this reading lacks or leaves empty, in order."""
        return tuple(name for name in REQUIRED if self.value(name) is None)

    def value(self, name: str) -> str | None:
        """Return the text of field ``name``, or ``None`` when it is absent or its text is empty."""
        field = self.fields.get(name)
        return None if field is None or field.value == "" else field.value

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
    payload: dict[str, Any] = {
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
        "printed_url": reading.printed_url,
    }
    if reading.url_strip is not None:
        # Written only when read: the previous release refuses the key (rollback, D191).
        payload["url_strip"] = reading.url_strip
    return payload


def is_printed_url(value: object) -> bool:
    """Return whether ``value`` is a printed URL a reading may hold: https, ASCII, capped (D189)."""
    return (
        isinstance(value, str)
        and len(value) <= MAX_PRINTED_URL_CHARS
        and _PRINTED_URL_SHAPE.fullmatch(value) is not None
    )


def is_printed_host(host: str) -> bool:
    """Return whether ``host`` is dotted labels whose top level is 2-24 letters (D189)."""
    labels = host.split(".")
    if len(labels) < HOST_MIN_LABELS or not all(labels):
        return False
    if any(label.startswith(PUNYCODE_PREFIX) for label in labels):
        return False
    top_level = labels[-1]
    return TLD_MIN_CHARS <= len(top_level) <= TLD_MAX_CHARS and top_level.isascii() and top_level.isalpha()


def is_url_strip(value: object) -> bool:
    """Return whether ``value`` is a URL strip a reading may hold: 1 to 120 URL characters (D191)."""
    return (
        isinstance(value, str)
        and len(value) <= MAX_URL_STRIP_CHARS
        and _URL_STRIP_SHAPE.fullmatch(value) is not None
    )


def reading_from_json(payload: object) -> Reading:
    """Return ``payload`` as a :class:`Reading`, or raise ValueError when it is not one.

    The payload comes from the helper process or the database, so every level is checked
    here. An error message names the offending key, never its value (R-9.16).
    """
    if not isinstance(payload, dict) or not _has_reading_shape(payload):
        raise ValueError(
            "reading must be an object with fields, document_url and optionally printed_url or url_strip"
        )
    raw_fields = payload["fields"]
    if not isinstance(raw_fields, dict):
        raise ValueError("reading field 'fields' must be an object")
    document_url = payload["document_url"]
    if document_url is not None and not isinstance(document_url, str):
        raise ValueError("reading field 'document_url' must be a string or null")
    printed_url = payload.get("printed_url")
    if printed_url is not None and not is_printed_url(printed_url):
        raise ValueError("reading field 'printed_url' must be a printed https URL or null")
    url_strip = payload.get("url_strip")
    if url_strip is not None and not is_url_strip(url_strip):
        raise ValueError("reading field 'url_strip' must be URL characters or null")
    return Reading(
        fields={name: _field_from_json(name, raw) for name, raw in raw_fields.items()},
        document_url=document_url,
        printed_url=printed_url,
        url_strip=url_strip,
    )


def _has_reading_shape(payload: dict[object, object]) -> bool:
    """Return whether ``payload`` has the required keys and only the optional ones (R-2.6, D191)."""
    keys = frozenset(payload)
    return keys >= _READING_KEYS and keys <= _READING_KEYS | _READING_OPTIONAL_KEYS


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
