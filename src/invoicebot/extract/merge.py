"""Merge the OCR and QR readings into the one reading the pipeline carries (D81).

OCR is the primary reading; the QR confirms it. A field both readers give with the same value
reaches full confidence, a field they disagree on keeps the OCR value and is flagged
``conflict``, and a field only one reader gives keeps that reader's confidence and flag (R-2.6).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from invoicebot.extract.fields import AMOUNT_FIELDS, FULL_CONFIDENCE, Field, Reading, finite_decimal


def merge(ocr: Reading | None, qr: Reading | None) -> Reading:
    """Return the OCR and QR readings merged into one (D81, R-2.6).

    ``document_url`` is the QR's when it carries one, else the OCR's. Fields are ordered OCR
    first, then the ones only the QR has.
    """
    if ocr is None:
        return Reading(fields={}, document_url=None) if qr is None else qr
    if qr is None:
        return ocr

    fields = {
        name: ocr_field
        if (qr_field := qr.fields.get(name)) is None
        else _merged_field(name, ocr_field, qr_field)
        for name, ocr_field in ocr.fields.items()
    }
    for name, qr_field in qr.fields.items():
        if name not in fields:
            fields[name] = qr_field
    return Reading(
        fields=fields,
        document_url=qr.document_url if qr.document_url is not None else ocr.document_url,
    )


def _merged_field(name: str, ocr_field: Field, qr_field: Field) -> Field:
    """Return the OCR field, confirmed at full confidence or flagged as a conflict (D81, R-2.6).

    When both readers inferred the field, their agreement is not independent evidence: it stays
    ``inferred`` at the OCR confidence rather than reaching full confidence.
    """
    if _same_value(name, ocr_field.value, qr_field.value):
        if ocr_field.flag == "inferred" and qr_field.flag == "inferred":
            return ocr_field
        return replace(ocr_field, confidence=FULL_CONFIDENCE, flag=None)
    return replace(ocr_field, flag="conflict")


def _same_value(name: str, ocr_value: str, qr_value: str) -> bool:
    """Return whether both values are equal after the field's own normalisation (D81).

    An amount compares as a ``Decimal`` and a date as a ``date``; when either value does not
    parse that way, the two compare as stripped upper-case text.
    """
    if name in AMOUNT_FIELDS:
        ocr_amount, qr_amount = finite_decimal(ocr_value), finite_decimal(qr_value)
        if ocr_amount is not None and qr_amount is not None:
            return ocr_amount == qr_amount
    elif name == "fecha_emision":
        ocr_date, qr_date = _iso_date(ocr_value), _iso_date(qr_value)
        if ocr_date is not None and qr_date is not None:
            return ocr_date == qr_date
    return ocr_value.strip().upper() == qr_value.strip().upper()


def _iso_date(text: str) -> date | None:
    """Return ``text`` as a date, or ``None`` when it is not an ISO date."""
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None
