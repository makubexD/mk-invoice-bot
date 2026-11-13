"""Parse a SUNAT QR payload into a :class:`Reading` (R-2.2, R-2.8).

The payload is pipe-separated and read by position; extra trailing parts are ignored. A payload
that starts with ``http`` carries a URL instead of data (R-2.8). Non-standard payloads seen in the
owner's invoices are repaired rather than rejected (D79): a document-type part that is not a
two-digit type comes from the serie's first letter, and an empty buyer-type part comes from the
buyer number. Inferred fields carry a lower confidence so the summary can mark them.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from invoicebot.extract.fields import Field, Reading, finite_decimal
from invoicebot.extract.validate import is_valid_ruc

PIPE = "|"
URL_PREFIXES = ("http://", "https://")
REQUIRED_PART_COUNT = 7
TIPO_DOC_ADQ_INDEX = 7
NUM_DOC_ADQ_INDEX = 8
FULL_CONFIDENCE = 1.0
INFERRED_CONFIDENCE = 0.9
RUC_LENGTH = 11
DNI_LENGTH = 8
DOCUMENT_TYPE_BY_SERIE_LETTER = {"F": "01", "B": "03"}
ISO_DATE_PATTERN = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
DMY_DATE_PATTERN = re.compile(r"([0-9]{2})/([0-9]{2})/([0-9]{4})")


def parse_qr_payload(text: str) -> Reading | None:
    """Return the reading carried by ``text``, or ``None`` when it is not a SUNAT payload.

    Never raises: a shorter payload, a non-numeric RUC, an unparseable date or amount, and a
    non-finite amount all return ``None``.
    """
    stripped = text.strip()
    if stripped.startswith(URL_PREFIXES):
        return Reading(fields={}, document_url=stripped)

    parts = [part.strip() for part in stripped.split(PIPE)]
    if len(parts) < REQUIRED_PART_COUNT:
        return None

    ruc_emisor, tipo_text, serie, numero, igv_text, total_text, fecha_text = parts[:REQUIRED_PART_COUNT]
    if not _is_issuer_ruc(ruc_emisor) or not serie or not numero:
        return None
    fecha = _parse_date(fecha_text)
    igv = _parse_amount(igv_text)
    total = _parse_amount(total_text)
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


def _field(value: str, confidence: float = FULL_CONFIDENCE) -> Field:
    """Return a QR-sourced field with the given confidence."""
    return Field(value=value, source="qr", confidence=confidence)


def _is_issuer_ruc(text: str) -> bool:
    """Return whether ``text`` is the 11-digit shape of a RUC.

    The check digit is not tested here: an issuer with a bad one still becomes a reading so
    ``check_issuer`` can answer BR-E02 instead of the photo looking unreadable.
    """
    return len(text) == RUC_LENGTH and text.isascii() and text.isdigit()


def _document_type(tipo_text: str, serie: str) -> Field | None:
    """Return ``tipo``, inferred from the serie's first letter when the part is not two digits (D79)."""
    if len(tipo_text) == 2 and tipo_text.isascii() and tipo_text.isdigit():
        return _field(tipo_text)
    inferred = DOCUMENT_TYPE_BY_SERIE_LETTER.get(serie[:1])
    if inferred is None:
        return None
    return _field(inferred, INFERRED_CONFIDENCE)


def _buyer_fields(parts: list[str]) -> dict[str, Field]:
    """Return the buyer fields, inferring the type from the number when that part is empty (D79)."""
    tipo_text = parts[TIPO_DOC_ADQ_INDEX] if len(parts) > TIPO_DOC_ADQ_INDEX else ""
    num_doc = parts[NUM_DOC_ADQ_INDEX] if len(parts) > NUM_DOC_ADQ_INDEX else ""

    fields: dict[str, Field] = {}
    if tipo_text:
        fields["tipo_doc_adq"] = _field(tipo_text)
    else:
        inferred = _infer_buyer_type(num_doc)
        if inferred is not None:
            fields["tipo_doc_adq"] = _field(inferred, INFERRED_CONFIDENCE)
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


def _parse_date(text: str) -> str | None:
    """Return ``text`` as an ISO date string, accepting ``yyyy-mm-dd`` and ``dd/mm/yyyy``."""
    iso_match = ISO_DATE_PATTERN.fullmatch(text)
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


def _parse_amount(text: str) -> Decimal | None:
    """Return ``text`` as a finite decimal, accepting a comma or a dot, or ``None``."""
    return finite_decimal(text.replace(",", "."))
