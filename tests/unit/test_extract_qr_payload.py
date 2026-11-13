"""Tests for the SUNAT QR payload parser (R-2.2, R-2.8)."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from invoicebot.extract.qr_payload import parse_qr_payload

# Both values come from tests/synthetic_rucs.txt. The third is deliberately invalid.
SYNTHETIC_RUC = "20000000001"
SYNTHETIC_RUC_OTHER = "51900000000"
WRONG_CHECK_DIGIT_RUC = "20000000002"
DNI = "12345678"
FULL = 1.0
INFERRED = 0.9

FieldSpec = tuple[str, float] | str | None

BASE_FIELDS: dict[str, tuple[str, float]] = {
    "ruc_emisor": (SYNTHETIC_RUC, FULL),
    "tipo": ("01", FULL),
    "serie": ("F001", FULL),
    "numero": ("15160", FULL),
    "igv": ("7.36", FULL),
    "total": ("49.90", FULL),
    "fecha_emision": ("2026-11-13", FULL),
    "tipo_doc_adq": ("6", FULL),
    "num_doc_adq": (SYNTHETIC_RUC, FULL),
}


def _fields(changes: Mapping[str, FieldSpec] | None = None) -> dict[str, tuple[str, float]]:
    """Return the expected ``(value, confidence)`` per field, applying ``changes``.

    A change holds ``(value, confidence)``, a bare value at full confidence, or ``None`` to
    drop the field from the expected reading.
    """
    expected = dict(BASE_FIELDS)
    for name, change in (changes or {}).items():
        if change is None:
            expected.pop(name, None)
        elif isinstance(change, tuple):
            expected[name] = change
        else:
            expected[name] = (change, FULL)
    return expected


VALID_PAYLOADS = [
    pytest.param(
        f"{SYNTHETIC_RUC}|01|F001|15160|7.36|49.90|2026-11-13|6|{SYNTHETIC_RUC}|abc123|",
        _fields(),
        id="standard-with-hash-and-trailing-pipe",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC}|01|F001|15160|7.36|49.90|2026-11-13",
        _fields({"tipo_doc_adq": None, "num_doc_adq": None}),
        id="seven-parts-no-buyer",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC}|01|F001|15160|7.36|49.90|13/11/2026|6|{SYNTHETIC_RUC}",
        _fields(),
        id="dmy-date-becomes-iso",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC}|01|F001|15160|7,36|49,90|2026-11-13|6|{SYNTHETIC_RUC}",
        _fields(),
        id="comma-decimals",
    ),
    pytest.param(
        f" {SYNTHETIC_RUC} | 01 | F001 | 15160 | 7.36 | 49.90 | 2026-11-13 | 6 | {SYNTHETIC_RUC} ",
        _fields(),
        id="spaces-around-parts",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC}|01|F001|00015160|7.36|49.90|2026-11-13|6|{SYNTHETIC_RUC}",
        _fields({"numero": "00015160"}),
        id="zero-padded-numero-kept",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC}|03|B001|15160|7.36|49.90|2026-11-13|1|{DNI}",
        _fields({"tipo": ("03", FULL), "serie": "B001", "tipo_doc_adq": ("1", FULL), "num_doc_adq": DNI}),
        id="dni-buyer",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC}|01|F001|15160|7.36|49.90|2026-11-13|-|-",
        _fields({"tipo_doc_adq": None, "num_doc_adq": None}),
        id="dash-buyer-dropped",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC}|01|F001|15160|7.36|49.90|2026-11-13|",
        _fields({"tipo_doc_adq": None, "num_doc_adq": None}),
        id="empty-buyer-trailing-pipe",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC}|01|F001|15160|7.36|49.90|2026-11-13||",
        _fields({"tipo_doc_adq": None, "num_doc_adq": None}),
        id="empty-buyer-two-empty-parts",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC}|01|F001|15160|7.36|49.90|2026-11-13|6|{SYNTHETIC_RUC}|hash",
        _fields(),
        id="ten-parts",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC}|01|F001|15160|7.36|49.90|2026-11-13|6|{SYNTHETIC_RUC}|hash|extra",
        _fields(),
        id="eleven-parts-extra-ignored",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC}|6|F001|15160|7.36|49.90|2026-11-13||{SYNTHETIC_RUC}",
        _fields({"tipo": ("01", INFERRED), "tipo_doc_adq": ("6", INFERRED)}),
        id="nine-parts-tipo-and-buyer-type-inferred",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC}|6|E001|15160|7.36|49.90|2026-11-13||{SYNTHETIC_RUC}",
        _fields({"serie": "E001", "tipo": None, "tipo_doc_adq": ("6", INFERRED)}),
        id="nine-parts-e-serie-leaves-tipo-missing",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC}|6|F001|15160|7.36|49.90|2026-11-13||{DNI}",
        _fields({"tipo": ("01", INFERRED), "tipo_doc_adq": ("1", INFERRED), "num_doc_adq": DNI}),
        id="nine-parts-dni-buyer-type-inferred",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC}|6|B001|15160|7.36|49.90|2026-11-13|6|{SYNTHETIC_RUC}",
        _fields({"tipo": ("03", INFERRED), "serie": "B001"}),
        id="b-serie-infers-boleta",
    ),
    pytest.param(
        f"{WRONG_CHECK_DIGIT_RUC}|01|F001|15160|7.36|49.90|2026-11-13|6|{SYNTHETIC_RUC}",
        _fields({"ruc_emisor": WRONG_CHECK_DIGIT_RUC}),
        id="issuer-wrong-check-digit-kept-for-br-e02",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC}|01|F001|15160|7.36|49.90|2026-11-13||{WRONG_CHECK_DIGIT_RUC}",
        _fields({"tipo_doc_adq": None, "num_doc_adq": WRONG_CHECK_DIGIT_RUC}),
        id="invalid-ruc-buyer-does-not-infer-type",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC}|01|F001|15160|0|49|2026-11-13|6|{SYNTHETIC_RUC}",
        _fields({"igv": "0", "total": "49"}),
        id="integer-amounts",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC}|01|F001|15160|0.00|0.00|2026-11-13|6|{SYNTHETIC_RUC}",
        _fields({"igv": "0.00", "total": "0.00"}),
        id="zero-amounts",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC_OTHER}|07|F123|1|4.74|45.16|2026-11-13|6|{SYNTHETIC_RUC}",
        _fields(
            {
                "ruc_emisor": SYNTHETIC_RUC_OTHER,
                "tipo": "07",
                "serie": "F123",
                "numero": "1",
                "igv": "4.74",
                "total": "45.16",
            }
        ),
        id="credit-note",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC}|03|F001|15160|7.36|49.90|2026-11-13|6|{SYNTHETIC_RUC}",
        _fields({"tipo": "03"}),
        id="explicit-two-digit-tipo-kept",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC}|01|F001|15160|7,36|49,90|13/11/2026|6|{SYNTHETIC_RUC}",
        _fields(),
        id="dmy-date-and-comma-decimals",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC}|01|F001|15160|7.36|1234567890.99|2026-11-13|6|{SYNTHETIC_RUC}",
        _fields({"total": "1234567890.99"}),
        id="ten-integer-digit-total",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC}|01|F001|15160|7.36|12,50|2026-11-13|6|{SYNTHETIC_RUC}",
        _fields({"total": "12.50"}),
        id="comma-total-becomes-dot",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC}|01|F001|15160|7.36|49.90|2026-11-13|DNI|{DNI}",
        _fields({"tipo_doc_adq": ("1", INFERRED), "num_doc_adq": DNI}),
        id="invalid-buyer-type-inferred-from-dni",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC}|01|F001|15160|7.36|49.90|2026-11-13|6|1234567\n8",
        _fields({"num_doc_adq": None}),
        id="buyer-number-with-newline-dropped",
    ),
    pytest.param(
        f"{SYNTHETIC_RUC}|01|F001|15160|7.36|49.90|2026-11-13|6|1234567890123456",
        _fields({"num_doc_adq": None}),
        id="sixteen-char-buyer-number-dropped",
    ),
]


@pytest.mark.parametrize(("payload", "expected"), VALID_PAYLOADS)
def test_parse_qr_payload_maps_parts_by_position(
    payload: str, expected: dict[str, tuple[str, float]]
) -> None:
    """R-2.2: a SUNAT payload maps by position, and every field is QR-sourced."""
    reading = parse_qr_payload(payload)

    assert reading is not None
    assert reading.document_url is None
    actual = {name: (field.value, field.confidence) for name, field in reading.fields.items()}
    assert actual == expected
    assert all(field.source == "qr" for field in reading.fields.values())


def test_inferred_tipo_carries_the_inferred_flag() -> None:
    """D79: a tipo inferred from the serie letter is flagged inferred."""
    reading = parse_qr_payload(f"{SYNTHETIC_RUC}|6|F001|15160|7.36|49.90|2026-11-13")

    assert reading is not None
    assert reading.fields["tipo"].flag == "inferred"


@pytest.mark.parametrize(
    ("payload", "expected_url"),
    [
        ("http://example.com/invoice/1", "http://example.com/invoice/1"),
        ("https://example.com/cpe?serie=F001", "https://example.com/cpe?serie=F001"),
    ],
)
def test_parse_qr_payload_url_becomes_document_url(payload: str, expected_url: str) -> None:
    """R-2.8: a URL payload becomes ``document_url`` and carries no fields."""
    reading = parse_qr_payload(f"  {payload}  ")

    assert reading is not None
    assert reading.fields == {}
    assert reading.document_url == expected_url


BROKEN_PAYLOADS = [
    pytest.param("", id="empty-string"),
    pytest.param("   ", id="whitespace-only"),
    pytest.param("hello", id="no-pipes"),
    pytest.param("see https://example.com/cpe", id="url-not-at-start"),
    pytest.param(f"{SYNTHETIC_RUC}|01|F001|15160|7.36", id="five-parts"),
    pytest.param(f"{SYNTHETIC_RUC}|01|F001|15160|7.36|49.90", id="six-parts"),
    pytest.param("2000000000A|01|F001|15160|7.36|49.90|2026-11-13", id="letters-in-ruc"),
    pytest.param("2000000000|01|F001|15160|7.36|49.90|2026-11-13", id="short-ruc"),
    pytest.param(f"{SYNTHETIC_RUC}|01||15160|7.36|49.90|2026-11-13", id="empty-serie"),
    pytest.param(f"{SYNTHETIC_RUC}|01|F001||7.36|49.90|2026-11-13", id="empty-numero"),
    pytest.param(f"{SYNTHETIC_RUC}|01|F001|15160||49.90|2026-11-13", id="empty-igv"),
    pytest.param(f"{SYNTHETIC_RUC}|01|F001|15160|7.36||2026-11-13", id="empty-total"),
    pytest.param(f"{SYNTHETIC_RUC}|01|F001|15160|abc|49.90|2026-11-13", id="letters-in-igv"),
    pytest.param(f"{SYNTHETIC_RUC}|01|F001|15160|7.36|NaN|2026-11-13", id="nan-total"),
    pytest.param(f"{SYNTHETIC_RUC}|01|F001|15160|Infinity|49.90|2026-11-13", id="infinite-igv"),
    pytest.param(f"{SYNTHETIC_RUC}|01|F001|15160|7.36|-Infinity|2026-11-13", id="negative-infinite-total"),
    pytest.param(f"{SYNTHETIC_RUC}|01|F001|15160|7.36|49,9,0|2026-11-13", id="double-comma-amount"),
    pytest.param(f"{SYNTHETIC_RUC}|01|F001|15160|7.36|49.90|32/13/2026", id="impossible-dmy-date"),
    pytest.param(f"{SYNTHETIC_RUC}|01|F001|15160|7.36|49.90|2026-13-01", id="impossible-iso-date"),
    pytest.param(f"{SYNTHETIC_RUC}|01|F001|15160|7.36|49.90|13/11/26", id="two-digit-year"),
    pytest.param(f"{SYNTHETIC_RUC}|01|F001|15160|7.36|49.90|not-a-date", id="letters-as-date"),
    pytest.param(f"{SYNTHETIC_RUC}|01|F001|15160|1e13|49.90|2026-11-13", id="exponent-igv"),
    pytest.param(f"{SYNTHETIC_RUC}|01|F001|15160|7.36|99999999999.99|2026-11-13", id="overflowing-total"),
    pytest.param(f"{SYNTHETIC_RUC}|01|F001|15160|7.36|-5|2026-11-13", id="negative-total"),
    pytest.param(f"{SYNTHETIC_RUC}|01|F001|15160|7.36|1_000|2026-11-13", id="underscore-total"),
    pytest.param(f"{SYNTHETIC_RUC}|01|F001|15160|7.36|12.345|2026-11-13", id="three-decimal-total"),
    pytest.param(f"{SYNTHETIC_RUC}|01|F001|15160|7.36|49.90\x00|2026-11-13", id="nul-in-total"),
    pytest.param(f"{SYNTHETIC_RUC}|01|F0001|15160|7.36|49.90|2026-11-13", id="five-char-serie"),
    pytest.param(f"{SYNTHETIC_RUC}|01|F00\n1|15160|7.36|49.90|2026-11-13", id="newline-in-serie"),
    pytest.param(f"{SYNTHETIC_RUC}|01|F001|123456789|7.36|49.90|2026-11-13", id="nine-digit-numero"),
    pytest.param(f"{SYNTHETIC_RUC}|01|F001|12a|7.36|49.90|2026-11-13", id="letters-in-numero"),
]


@pytest.mark.parametrize("payload", BROKEN_PAYLOADS)
def test_parse_qr_payload_returns_none_for_broken_payloads(payload: str) -> None:
    """R-2.2: a short or unparseable payload returns ``None`` instead of raising."""
    assert parse_qr_payload(payload) is None
