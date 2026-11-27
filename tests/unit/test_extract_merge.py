"""Tests for merging the OCR and QR readings (D81, R-2.6, D189)."""

from __future__ import annotations

from invoicebot.extract.fields import FULL_CONFIDENCE, Field, Flag, Reading
from invoicebot.extract.merge import fill_missing, merge

OCR_CONFIDENCE = 0.7
DOCUMENT_URL = "https://example.test/invoice"
PRINTED_URL = "https://portal.example.pe/consulta"
URL_STRIP = "hitps.//sy.example pe/tiemda"


def _reading(
    fields: dict[str, Field],
    document_url: str | None = None,
    printed_url: str | None = None,
    url_strip: str | None = None,
) -> Reading:
    """Return a reading built from already-parsed fields."""
    return Reading(fields=fields, document_url=document_url, printed_url=printed_url, url_strip=url_strip)


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


def test_merge_keeps_the_ocr_printed_url() -> None:
    """D189: a merged reading keeps the OCR reading's printed URL."""
    merged = merge(
        _reading({"total": _ocr_field("12.50")}, printed_url=PRINTED_URL),
        _reading({"total": _qr_field("12.50")}),
    )

    assert merged.printed_url == PRINTED_URL


def test_merge_without_ocr_has_no_printed_url() -> None:
    """D189: a QR-only merge carries no printed URL."""
    assert merge(None, _reading({"total": _qr_field("12.50")})).printed_url is None


def test_merge_without_qr_keeps_the_ocr_printed_url() -> None:
    """D189: a merge with no QR reading keeps the OCR printed URL."""
    ocr = _reading({"total": _ocr_field("12.50")}, printed_url=PRINTED_URL)

    assert merge(ocr, None).printed_url == PRINTED_URL


def test_merge_keeps_the_ocr_url_strip() -> None:
    """D191: a merged reading keeps the OCR reading's URL strip."""
    merged = merge(
        _reading({"total": _ocr_field("12.50")}, url_strip=URL_STRIP),
        _reading({"total": _qr_field("12.50")}),
    )

    assert merged.url_strip == URL_STRIP


def test_merge_keeps_tipo_inferred_when_both_readers_inferred_it() -> None:
    """R-2.6: an OCR and a QR `tipo` both inferred and equal stay inferred at the OCR confidence."""
    ocr_field = _ocr_field("01", flag="inferred")
    merged = merge(_reading({"tipo": ocr_field}), _reading({"tipo": _qr_field("01", flag="inferred")}))

    assert merged.fields["tipo"] == ocr_field


def test_merge_confirms_an_inferred_qr_tipo() -> None:
    """D81: an inferred QR `tipo` equal to the OCR `tipo` reaches 1.0 with no flag."""
    merged = merge(
        _reading({"tipo": _ocr_field("01")}),
        _reading({"tipo": _qr_field("01", flag="inferred")}),
    )

    assert merged.fields["tipo"] == Field(value="01", source="ocr", confidence=FULL_CONFIDENCE, flag=None)


BUYER_RUC = "20000000010"
OTHER_RUC = "20000000001"


def test_fill_missing_takes_absent_required_field() -> None:
    """D200: `kept` lacks `fecha_emision`, `others[0]` has it at 0.7: filled, rest unchanged."""
    kept = _reading({"total": _ocr_field("12.50", 0.9)})
    other = _reading({"fecha_emision": _ocr_field("2026-11-01", 0.7)})

    result = fill_missing(kept, [other])

    assert result.fields["fecha_emision"] == _ocr_field("2026-11-01", 0.7)
    assert result.fields["total"] == kept.fields["total"]
    assert list(result.fields) == ["total", "fecha_emision"]


def test_fill_missing_keeps_present_field_and_returns_kept() -> None:
    """D200: `kept` has `total` 49.90 and `others[0]` has 12.50: `kept` is returned itself."""
    kept = _reading({"total": _ocr_field("49.90")})

    result = fill_missing(kept, [_reading({"total": _ocr_field("12.50")})])

    assert result is kept
    assert result.fields["total"].value == "49.90"


def test_fill_missing_takes_first_other_that_has_it() -> None:
    """D200: two others give different `fecha_emision` values: the first one's is taken."""
    first = _reading({"fecha_emision": _ocr_field("2026-11-01")})
    second = _reading({"fecha_emision": _ocr_field("2026-11-02")})

    result = fill_missing(_reading({}), [first, second])

    assert result.fields["fecha_emision"].value == "2026-11-01"


def test_fill_missing_serie_and_numero_come_from_one_reading() -> None:
    """D200: `kept` lacks both; `others[0]` has only `serie`, `others[1]` has both: both from it."""
    only_serie = _reading({"serie": _ocr_field("F001")})
    both = _reading({"serie": _ocr_field("F002"), "numero": _ocr_field("15")})

    result = fill_missing(_reading({}), [only_serie, both])

    assert result.fields["serie"].value == "F002"
    assert result.fields["numero"].value == "15"


def test_fill_missing_leaves_numero_when_kept_has_serie() -> None:
    """D200: `kept` has `serie` but no `numero`, `others[0]` has both: `numero` stays missing."""
    kept = _reading({"serie": _ocr_field("F001")})
    other = _reading({"serie": _ocr_field("F002"), "numero": _ocr_field("15")})

    result = fill_missing(kept, [other])

    assert result is kept
    assert "numero" not in result.fields


def test_fill_missing_skips_buyer_ruc_as_issuer() -> None:
    """D200: a `ruc_emisor` candidate equal to the buyer `num_doc_adq` is skipped for the next read."""
    kept = _reading({"num_doc_adq": _ocr_field(BUYER_RUC)})
    buyer_read = _reading({"ruc_emisor": _ocr_field(BUYER_RUC)})
    issuer_read = _reading({"ruc_emisor": _ocr_field(OTHER_RUC)})

    result = fill_missing(kept, [buyer_read, issuer_read])

    assert result.fields["ruc_emisor"].value == OTHER_RUC


def test_fill_missing_ignores_optional_fields() -> None:
    """D200: `kept` lacks `igv` (not required) and `others[0]` has it: `igv` stays missing."""
    kept = _reading({"total": _ocr_field("12.50")})

    result = fill_missing(kept, [_reading({"igv": _ocr_field("1.90")})])

    assert result is kept
    assert "igv" not in result.fields


def test_fill_missing_with_no_others_returns_kept() -> None:
    """D200: with no other reads, `kept` is returned itself."""
    kept = _reading({}, document_url=DOCUMENT_URL)

    assert fill_missing(kept, []) is kept
