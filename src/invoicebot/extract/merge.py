"""Combine readings of one document into the reading the pipeline carries.

Two jobs. ``merge`` joins the OCR and QR readings (D81): OCR is the primary reading and the QR
confirms it. A field both readers give with the same value reaches full confidence, a field they
disagree on takes the QR's field unless the QR is contradicted, and then keeps the OCR value
flagged ``conflict`` (D210, D211), and a field only one reader gives keeps that reader's confidence
and flag (R-2.6). ``best_of_reads`` takes each required field from the best of
several OCR reads of the same document, by confidence (D200, D212).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date

from invoicebot.extract.fields import (
    AMOUNT_FIELDS,
    BUYER_NAME,
    FULL_CONFIDENCE,
    REQUIRED,
    UNCHECKED_BUYER_RUC,
    Field,
    Reading,
    finite_decimal,
)


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

    qr_wins = not _contradicts(ocr, qr)
    fields = {
        name: ocr_field
        if (qr_field := qr.fields.get(name)) is None
        else _merged_field(name, ocr_field, qr_field, qr_wins)
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


#: Fields that show whether the QR belongs to the invoice the OCR read (D210, D211).
ANCHOR_FIELDS = ("ruc_emisor", "total", "fecha_emision")


def _contradicts(ocr: Reading, qr: Reading) -> bool:
    """Return whether the readers share an anchor the QR did not infer and agree on none (D211).

    A single agreeing anchor shows the QR is this invoice's (D210); with no shared anchor there is
    no evidence against it, so it is not contradicted.
    """
    shared = [
        (name, ocr_field.value, qr_field.value)
        for name in ANCHOR_FIELDS
        if (ocr_field := ocr.fields.get(name)) is not None
        and (qr_field := qr.fields.get(name)) is not None
        and qr_field.flag != "inferred"
    ]
    return bool(shared) and not any(same_value(*anchor) for anchor in shared)


def _merged_field(name: str, ocr_field: Field, qr_field: Field, qr_wins: bool) -> Field:
    """Return the merged field: confirmed, the QR's when it is not contradicted, or a conflict (D81, D211).

    When both readers inferred the field, their agreement is not independent evidence: it stays
    ``inferred`` at the OCR confidence rather than reaching full confidence.
    """
    if same_value(name, ocr_field.value, qr_field.value):
        if ocr_field.flag == "inferred" and qr_field.flag == "inferred":
            return ocr_field
        return replace(ocr_field, confidence=FULL_CONFIDENCE, flag=None)
    return qr_field if qr_wins else replace(ocr_field, flag="conflict")


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


#: The invoice number's two parts, taken together from one read or not at all (D200, D212).
_SERIE_NUMERO = ("serie", "numero")

#: The buyer hints, carried only without a buyer RUC (D213).
_BUYER_HINTS = (UNCHECKED_BUYER_RUC, BUYER_NAME)

#: The score of a read that lacks a field (or half a pair): lower than any confidence.
_MISSING = -1.0


def best_of_reads(
    kept: Reading, others: Sequence[Reading], qr_names: frozenset[str] = frozenset()
) -> Reading:
    """Return ``kept`` with each required field replaced by the best read of it (D200, D212).

    Each required field comes from the read that gives it at the highest confidence among ``kept``
    and ``others``; a tie keeps ``kept``'s, then the earlier read's, and a missing field scores
    lower than any confidence. ``serie`` and ``numero`` move together from one read, scored by the
    lower of their two confidences; a read with only one of them has no pair and never splits one.
    A field named in ``qr_names`` (the QR reading's fields) is never taken, so the QR value is the
    one the merge keeps, and the pair is not taken when the QR has either part. A ``ruc_emisor``
    from another read that equals ``kept``'s buyer ``num_doc_adq`` is skipped, and a ``ruc_emisor``
    taken from another read brings that read's ``num_doc_adq`` when neither ``kept`` nor the QR has
    a buyer, so D199 can still swap them. Fields outside ``REQUIRED`` and the URLs stay ``kept``'s;
    taken fields replace in place, and with nothing better ``kept`` itself is returned. The buyer
    hints (``num_doc_adq_unchecked``, ``nombre_adq``) are each taken from their highest-confidence
    read (a tie keeps ``kept``'s) when the result, or the QR, has no ``num_doc_adq``; otherwise
    neither hint is carried (D213).
    """
    taken: dict[str, Field] = {}
    groups = [(name,) for name in REQUIRED if name not in _SERIE_NUMERO] + [_SERIE_NUMERO]
    buyer = kept.value("num_doc_adq")
    for names in groups:
        if qr_names.isdisjoint(names):
            read = _best_read(names, kept, others, buyer)
            if read is not kept:
                taken.update({name: read.fields[name] for name in names})
                brings_buyer = names == ("ruc_emisor",) and buyer is None and "num_doc_adq" not in qr_names
                if brings_buyer and read.value("num_doc_adq") is not None:
                    taken["num_doc_adq"] = read.fields["num_doc_adq"]
    fields = _with_buyer_hints({**kept.fields, **taken}, kept, others, "num_doc_adq" in qr_names)
    if fields == kept.fields:
        return kept
    return replace(kept, fields=fields)


def _with_buyer_hints(
    fields: dict[str, Field], kept: Reading, others: Sequence[Reading], qr_has_buyer: bool
) -> dict[str, Field]:
    """Return ``fields`` with the buyer hints dropped, or taken from their best read (D213)."""
    without_hints = {name: field for name, field in fields.items() if name not in _BUYER_HINTS}
    if "num_doc_adq" in fields or qr_has_buyer:
        return without_hints
    for name in _BUYER_HINTS:
        read = _best_read((name,), kept, others, None)
        if read.value(name) is not None:
            without_hints[name] = read.fields[name]
    return without_hints


def _best_read(
    names: tuple[str, ...], kept: Reading, others: Sequence[Reading], buyer: str | None
) -> Reading:
    """Return the read scoring highest on ``names``, ``kept`` unless another is strictly better (D212).

    A ``ruc_emisor`` equal to ``buyer`` is not a candidate from another read (D199).
    """
    best, best_score = kept, _score(names, kept)
    for other in others:
        if names == ("ruc_emisor",) and other.value("ruc_emisor") == buyer:
            continue
        if (score := _score(names, other)) > best_score:
            best, best_score = other, score
    return best


def _score(names: tuple[str, ...], read: Reading) -> float:
    """Return the lowest confidence ``read`` gives over ``names``, or ``_MISSING`` when one is absent."""
    if any(read.value(name) is None for name in names):
        return _MISSING
    return min(read.fields[name].confidence for name in names)
