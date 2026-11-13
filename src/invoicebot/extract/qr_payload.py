"""Parse a SUNAT QR payload into a :class:`Reading` (R-2.2, R-2.8).

The payload is pipe-separated and read by position; extra trailing parts are ignored. A payload
that starts with ``http`` carries a URL instead of data (R-2.8). Non-standard payloads seen in the
owner's invoices are repaired rather than rejected (D79): a date may carry a time, a short label
may precede the issuer RUC, a document-type part that is not a two-digit type comes from the
serie's first letter, an empty buyer-type part comes from the buyer number, and a buyer type
written as a word becomes its code. Inferred fields carry a lower confidence so the summary can
mark them.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from invoicebot.extract.fields import FULL_CONFIDENCE, Field, Flag, Reading, finite_decimal
from invoicebot.extract.validate import is_valid_ruc

PIPE = "|"
URL_PREFIXES = ("http://", "https://")
REQUIRED_PART_COUNT = 7
TIPO_DOC_ADQ_INDEX = 7
NUM_DOC_ADQ_INDEX = 8
INFERRED_CONFIDENCE = 0.9
RUC_LENGTH = 11
DNI_LENGTH = 8
DOCUMENT_TYPE_BY_SERIE_LETTER = {"F": "01", "B": "03"}
BUYER_TYPE_BY_WORD = {"RUC": "6", "DNI": "1"}
ISO_DATE_OR_DATETIME_PATTERN = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})(?: [0-9]{2}:[0-9]{2}:[0-9]{2})?")
DMY_DATE_PATTERN = re.compile(r"([0-9]{2})/([0-9]{2})/([0-9]{4})")
AMOUNT_PATTERN = re.compile(r"[0-9]{1,10}(?:[.,][0-9]{1,2})?")
QR_SERIE_SHAPE = re.compile(r"[A-Z0-9]{1,4}")
NUMERO_PATTERN = re.compile(r"[0-9]{1,8}")
TIPO_DOC_ADQ_PATTERN = re.compile(r"[0-9A-Z]")
NUM_DOC_ADQ_PATTERN = re.compile(r"[0-9A-Z]{1,15}")
PREFIXED_RUC_PATTERN = re.compile(r"[^0-9|]{1,10} - ([0-9]{11})")


def parse_qr_payload(text: str) -> Reading | None:
    """Return the reading carried by ``text``, or ``None`` when it is not a SUNAT payload.

    Never raises: a shorter payload, a non-numeric RUC, a serie or numero of the wrong shape, an
    unparseable date or amount, and an amount with more than ten integer digits or two decimals
    all return ``None``. Buyer fields of the wrong shape are dropped, and an invalid buyer type
    is inferred from a valid buyer number.
    """
    stripped = text.strip()
    if stripped.startswith(URL_PREFIXES):
        return Reading(fields={}, document_url=stripped)

    parts = [part.strip() for part in stripped.split(PIPE)]
    if len(parts) < REQUIRED_PART_COUNT:
        return None

    ruc_text, tipo_text, serie, numero, igv_text, total_text, fecha_text = parts[:REQUIRED_PART_COUNT]
    ruc_emisor = _issuer_ruc(ruc_text)
    if (
        ruc_emisor is None
        or QR_SERIE_SHAPE.fullmatch(serie) is None
        or NUMERO_PATTERN.fullmatch(numero) is None
    ):
        return None
    fecha = parse_date(fecha_text)
    igv = parse_amount(igv_text)
    total = parse_amount(total_text)
    if fecha is None or igv is None or total is None:
        return None

    fields: dict[str, Field] = {
        "ruc_emisor": _field(ruc_emisor),
        "serie": _field(serie),
        "numero": _field(numero),
        "igv": _field(str(igv)),
        "total": _field(str(total)),
        "fecha_emision": _field(fecha),
    }
    tipo = _document_type(tipo_text, serie)
    if tipo is not None:
        fields["tipo"] = tipo
    fields.update(_buyer_fields(parts))
    return Reading(fields=fields, document_url=None)


def _field(value: str, confidence: float = FULL_CONFIDENCE, flag: Flag | None = None) -> Field:
    """Return a QR-sourced field with the given confidence and flag."""
    return Field(value=value, source="qr", confidence=confidence, flag=flag)


def _is_issuer_ruc(text: str) -> bool:
    """Return whether ``text`` is the 11-digit shape of a RUC.

    The check digit is not tested here: an issuer with a bad one still becomes a reading so
    ``check_issuer`` can answer BR-E02 instead of the photo looking unreadable.
    """
    return len(text) == RUC_LENGTH and text.isascii() and text.isdigit()


def _issuer_ruc(text: str) -> str | None:
    """Return the issuer RUC, dropping a short non-numeric label printed before it (D79)."""
    if _is_issuer_ruc(text):
        return text
    prefixed = PREFIXED_RUC_PATTERN.fullmatch(text)
    if prefixed is not None and _is_issuer_ruc(prefixed.group(1)):
        return prefixed.group(1)
    return None


def _document_type(tipo_text: str, serie: str) -> Field | None:
    """Return ``tipo``, inferred from the serie's first letter when the part is not two digits (D79)."""
    if len(tipo_text) == 2 and tipo_text.isascii() and tipo_text.isdigit():
        return _field(tipo_text)
    inferred = DOCUMENT_TYPE_BY_SERIE_LETTER.get(serie[:1])
    if inferred is None:
        return None
    return _field(inferred, INFERRED_CONFIDENCE, flag="inferred")


def _buyer_fields(parts: list[str]) -> dict[str, Field]:
    """Return the buyer fields, inferring the type from a word or the number (D79)."""
    tipo_text = parts[TIPO_DOC_ADQ_INDEX] if len(parts) > TIPO_DOC_ADQ_INDEX else ""
    num_text = parts[NUM_DOC_ADQ_INDEX] if len(parts) > NUM_DOC_ADQ_INDEX else ""
    tipo_doc = tipo_text if TIPO_DOC_ADQ_PATTERN.fullmatch(tipo_text) else ""
    num_doc = num_text if NUM_DOC_ADQ_PATTERN.fullmatch(num_text) else ""

    fields: dict[str, Field] = {}
    if tipo_doc:
        fields["tipo_doc_adq"] = _field(tipo_doc)
    elif (word_code := BUYER_TYPE_BY_WORD.get(tipo_text)) is not None:
        fields["tipo_doc_adq"] = _field(word_code, INFERRED_CONFIDENCE, flag="inferred")
    else:
        inferred = _infer_buyer_type(num_doc)
        if inferred is not None:
            fields["tipo_doc_adq"] = _field(inferred, INFERRED_CONFIDENCE, flag="inferred")
    if num_doc:
        fields["num_doc_adq"] = _field(num_doc)
    return fields


def _infer_buyer_type(num_doc: str) -> str | None:
    """Return ``6`` for a valid RUC number, ``1`` for an eight-digit DNI, else ``None`` (D79)."""
    if is_valid_ruc(num_doc):
        return "6"
    if len(num_doc) == DNI_LENGTH and num_doc.isascii() and num_doc.isdigit():
        return "1"
    return None


def parse_date(text: str) -> str | None:
    """Return ``text`` as an ISO date string, accepting ``yyyy-mm-dd``, ``dd/mm/yyyy`` and a trailing time."""
    iso_match = ISO_DATE_OR_DATETIME_PATTERN.fullmatch(text)
    if iso_match is not None:
        year, month, day = iso_match.groups()
    else:
        dmy_match = DMY_DATE_PATTERN.fullmatch(text)
        if dmy_match is None:
            return None
        day, month, year = dmy_match.groups()
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def parse_amount(text: str) -> Decimal | None:
    """Return ``text`` as a finite decimal with at most two decimals, or ``None``."""
    if AMOUNT_PATTERN.fullmatch(text) is None:
        return None
    return finite_decimal(text.replace(",", "."))
