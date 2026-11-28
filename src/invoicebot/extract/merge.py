"""Combine readings of one document into the reading the pipeline carries.

Two jobs. ``merge`` joins the OCR and QR readings (D81): OCR is the primary reading and the QR
confirms it. A field both readers give with the same value reaches full confidence, a field they
disagree on takes the QR's field when the QR corroborates the reading and else keeps the OCR value
flagged ``conflict`` (D210), and a field only one reader gives keeps that reader's confidence and
flag (R-2.6). ``fill_missing`` fills a reading's missing
required fields from other OCR reads of the same document (D200).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date

from invoicebot.extract.fields import AMOUNT_FIELDS, FULL_CONFIDENCE, REQUIRED, Field, Reading, finite_decimal


def merge(ocr: Reading | None, qr: Reading | None) -> Reading:
    """Return the OCR and QR readings merged into one (D81, R-2.6).

    ``document_url`` is the QR's when it carries one, else the OCR's, and ``printed_url``
    and ``url_strip`` are always the OCR's, the only reader that reads the address printed
    on the ticket. Fields are ordered OCR first, then the ones only the QR has.
    """
    if ocr is None:
        return Reading(fields={}, document_url=None) if qr is None else qr
    if qr is None:
        return ocr

    corroborated = _corroborates(ocr, qr)
    fields = {
        name: ocr_field
        if (qr_field := qr.fields.get(name)) is None
        else _merged_field(name, ocr_field, qr_field, corroborated)
        for name, ocr_field in ocr.fields.items()
    }
    for name, qr_field in qr.fields.items():
        if name not in fields:
            fields[name] = qr_field
    return Reading(
        fields=fields,
        document_url=qr.document_url if qr.document_url is not None else ocr.document_url,
        printed_url=ocr.printed_url,
        url_strip=ocr.url_strip,
    )


#: Fields whose agreement shows the QR belongs to the invoice the OCR read (D210).
ANCHOR_FIELDS = ("ruc_emisor", "total", "fecha_emision")


def _corroborates(ocr: Reading, qr: Reading) -> bool:
    """Return whether the QR matches the OCR on an anchor field that the QR did not infer (D210)."""
    for name in ANCHOR_FIELDS:
        ocr_field, qr_field = ocr.fields.get(name), qr.fields.get(name)
        if (
            ocr_field is not None
            and qr_field is not None
            and qr_field.flag != "inferred"
            and same_value(name, ocr_field.value, qr_field.value)
        ):
            return True
    return False


def _merged_field(name: str, ocr_field: Field, qr_field: Field, corroborated: bool) -> Field:
    """Return the merged field: confirmed, the QR's when it corroborates, or a conflict (D81, D210).

    When both readers inferred the field, their agreement is not independent evidence: it stays
    ``inferred`` at the OCR confidence rather than reaching full confidence.
    """
    if same_value(name, ocr_field.value, qr_field.value):
        if ocr_field.flag == "inferred" and qr_field.flag == "inferred":
            return ocr_field
        return replace(ocr_field, confidence=FULL_CONFIDENCE, flag=None)
    return qr_field if corroborated else replace(ocr_field, flag="conflict")


def same_value(name: str, ocr_value: str, qr_value: str) -> bool:
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


#: The invoice number's two parts, filled together or not at all (D200).
_SERIE_NUMERO = ("serie", "numero")


def fill_missing(kept: Reading, others: Sequence[Reading], qr_names: frozenset[str] = frozenset()) -> Reading:
    """Return ``kept`` with its missing required fields taken from ``others`` (D200).

    Each missing required field comes from the first reading in ``others`` that has it, except
    ``serie`` and ``numero``, which move together: both are taken from the first reading with both
    when neither ``kept`` nor the QR has either. A field named in ``qr_names`` (the QR reading's
    fields) is not filled, so the QR value is the one the merge keeps. A ``ruc_emisor`` equal to
    the buyer's ``num_doc_adq`` is skipped, and a filled ``ruc_emisor`` brings that read's
    ``num_doc_adq`` when there is no buyer yet, so D199 can still swap them. Filled fields come
    after the existing ones and the URLs stay ``kept``'s; with nothing to fill, ``kept`` itself is
    returned.
    """

    present = {name for name in (*REQUIRED, "num_doc_adq") if kept.value(name) is not None} | qr_names
    filled: dict[str, Field] = {}
    for name in REQUIRED:
        if name not in _SERIE_NUMERO and name not in present:
            filled.update(_first_fill(name, kept, others, present))
    if present.isdisjoint(_SERIE_NUMERO):
        for other in others:
            if other.value("serie") is not None and other.value("numero") is not None:
                filled.update(serie=other.fields["serie"], numero=other.fields["numero"])
                break
    if not filled:
        return kept
    return replace(kept, fields={**kept.fields, **filled})


def _first_fill(name: str, kept: Reading, others: Sequence[Reading], present: set[str]) -> dict[str, Field]:
    """Return the fields the first fitting read in ``others`` gives for ``name`` (D200).

    An issuer comes with that read's buyer when no buyer is ``present``, so D199 sees both.
    """
    for other in others:
        if (field := _fillable(name, kept, other)) is None:
            continue
        if name == "ruc_emisor" and "num_doc_adq" not in present and other.value("num_doc_adq") is not None:
            return {name: field, "num_doc_adq": other.fields["num_doc_adq"]}
        return {name: field}
    return {}


def _fillable(name: str, kept: Reading, other: Reading) -> Field | None:
    """Return ``other``'s field ``name`` when it may fill ``kept``'s gap, else ``None`` (D200)."""
    value = other.value(name)
    if value is None or (name == "ruc_emisor" and value == kept.value("num_doc_adq")):
        return None
    return other.fields[name]
