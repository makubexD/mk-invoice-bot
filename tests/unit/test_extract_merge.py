"""Tests for merging the OCR and QR readings (D81, R-2.6)."""

from __future__ import annotations

from invoicebot.extract.fields import FULL_CONFIDENCE, Field, Flag, Reading
from invoicebot.extract.merge import merge

OCR_CONFIDENCE = 0.7
DOCUMENT_URL = "https://example.test/invoice"


def _reading(fields: dict[str, Field], document_url: str | None = None) -> Reading:
    """Return a reading built from already-parsed fields."""
    return Reading(fields=fields, document_url=document_url)


def _ocr_field(value: str, confidence: float = OCR_CONFIDENCE, flag: Flag | None = None) -> Field:
    """Return an OCR-sourced field with the given confidence and flag."""
    return Field(value=value, source="ocr", confidence=confidence, flag=flag)


def _qr_field(value: str, flag: Flag | None = None) -> Field:
    """Return a QR-sourced field at full confidence, optionally flagged."""
    return Field(value=value, source="qr", confidence=FULL_CONFIDENCE, flag=flag)


def test_merge_equal_amount_takes_full_confidence() -> None:
    """D81: OCR `total` 12.50 at 0.7 and QR `total` 12.5 merge to 12.50 from OCR at 1.0 with no flag."""
    merged = merge(_reading({"total": _ocr_field("12.50")}), _reading({"total": _qr_field("12.5")}))

    assert merged.fields["total"] == Field(value="12.50", source="ocr", confidence=FULL_CONFIDENCE, flag=None)


def test_merge_different_amount_keeps_ocr_value_and_flags_conflict() -> None:
    """D81: OCR `total` 12.50 and QR `total` 21.50 keep the OCR value and confidence, flagged conflict."""
    merged = merge(_reading({"total": _ocr_field("12.50")}), _reading({"total": _qr_field("21.50")}))

    assert merged.fields["total"] == Field(
        value="12.50", source="ocr", confidence=OCR_CONFIDENCE, flag="conflict"
    )


def test_merge_keeps_qr_only_field() -> None:
    """D81: `num_doc_adq` only in the QR comes through as that QR field."""
    qr_field = _qr_field("20000000001")
    merged = merge(_reading({}), _reading({"num_doc_adq": qr_field}))

    assert merged.fields["num_doc_adq"] == qr_field


def test_merge_keeps_ocr_only_field() -> None:
    """D81: `base` only in the OCR comes through as that OCR field."""
    ocr_field = _ocr_field("42.29")
    merged = merge(_reading({"base": ocr_field}), _reading({}))

    assert merged.fields["base"] == ocr_field


def test_merge_without_qr_returns_the_ocr_reading() -> None:
    """D81: a missing QR reading leaves the OCR reading unchanged."""
    ocr = _reading({"total": _ocr_field("12.50")}, document_url=DOCUMENT_URL)

    assert merge(ocr, None) is ocr


def test_merge_without_ocr_returns_the_qr_reading() -> None:
    """D81: a missing OCR reading leaves the QR reading unchanged."""
    qr = _reading({"total": _qr_field("12.50")})

    assert merge(None, qr) is qr


def test_merge_without_readings_returns_an_empty_reading() -> None:
    """D81: two missing readings merge to an empty reading with no document URL."""
    assert merge(None, None) == Reading(fields={}, document_url=None)


def test_merge_takes_document_url_from_the_qr() -> None:
    """D81: a QR that carries only `document_url` adds it beside the OCR fields."""
    ocr_field = _ocr_field("12.50")
    merged = merge(_reading({"total": ocr_field}), _reading({}, document_url=DOCUMENT_URL))

    assert merged.document_url == DOCUMENT_URL
    assert merged.fields == {"total": ocr_field}


def test_merge_confirms_an_inferred_qr_tipo() -> None:
    """D81: an inferred QR `tipo` equal to the OCR `tipo` reaches 1.0 with no flag."""
    merged = merge(
        _reading({"tipo": _ocr_field("01")}),
        _reading({"tipo": _qr_field("01", flag="inferred")}),
    )

    assert merged.fields["tipo"] == Field(value="01", source="ocr", confidence=FULL_CONFIDENCE, flag=None)
