"""Tests for the OCR rule parser and printed URLs (S3.6, R-2.3, R-2.4, R-2.10, R-2.14)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from invoicebot.extract.ocr import OcrText, OcrWord
from invoicebot.extract.ocr_parse import OcrReading, parse_ocr
from invoicebot.extract.parsing_rules import ParsingRules, load_rules

ROOT = Path(__file__).resolve().parents[2]
RULES_PATH = ROOT / "config" / "parsing_rules.yaml"

# Synthetic RUCs from tests/synthetic_rucs.txt. No real RUC may appear here.
SYNTHETIC_RUC = "20000000001"
SYNTHETIC_RUC_OTHER = "51900000000"
WRONG_CHECK_DIGIT_RUC = "20000000002"
DEFAULT_CONFIDENCE = 0.95


@pytest.fixture(scope="module")
def rules() -> ParsingRules:
    """Return the checked-in parsing rules."""
    return load_rules(RULES_PATH)


def _ocr(*lines: str) -> OcrText:
    """Return an :class:`OcrText` whose words all carry the default confidence."""
    return OcrText(
        lines=tuple(
            tuple(OcrWord(text=word, confidence=DEFAULT_CONFIDENCE) for word in line.split())
            for line in lines
        )
    )


def _fields(text: OcrText, rules: ParsingRules) -> dict[str, str]:
    """Return the values the OCR parser reads from ``text``."""
    return {name: field.value for name, field in parse_ocr(text, rules).reading.fields.items()}


def test_load_rules_config_file_returns_compiled_rules(rules: ParsingRules) -> None:
    """S3.6: the checked-in rules load and every pattern is compiled ignoring case."""
    assert rules.ruc
    assert rules.amount_patterns["total"]
    assert rules.printed_url.flags & re.IGNORECASE


def test_load_rules_pattern_without_named_group_raises(tmp_path: Path) -> None:
    """S3.6: a rule pattern that loses its named group is rejected when the file loads."""
    document = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    document["date"] = r"\d{2}/\d{2}/\d{4}"
    path = tmp_path / "parsing_rules.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(ValueError, match="lacks named group"):
        load_rules(path)


def test_parse_ocr_factura_reads_six_required_fields(rules: ParsingRules) -> None:
    """R-2.3: a factura with every required field reads them all with source 'ocr'."""
    text = _ocr(
        f"RUC {SYNTHETIC_RUC}",
        "FACTURA ELECTRÓNICA",
        "F001-123",
        "Fecha de emisión: 13/11/2026",
        "TOTAL S/ 12.50",
    )

    result = parse_ocr(text, rules)

    assert result.reading.document_url is None
    assert _fields(text, rules) == {
        "ruc_emisor": SYNTHETIC_RUC,
        "tipo": "01",
        "serie": "F001",
        "numero": "123",
        "fecha_emision": "2026-11-13",
        "total": "12.50",
    }
    assert all(field.source == "ocr" for field in result.reading.fields.values())
    assert all(
        field.confidence == pytest.approx(DEFAULT_CONFIDENCE) for field in result.reading.fields.values()
    )


def test_parse_ocr_boleta_reads_type_serie_and_numero(rules: ParsingRules) -> None:
    """R-2.3: a boleta with B001-123 reads tipo 03, serie B001 and numero 123."""
    fields = _fields(_ocr(f"RUC {SYNTHETIC_RUC}", "BOLETA ELECTRÓNICA", "B001-123"), rules)

    assert (fields["tipo"], fields["serie"], fields["numero"]) == ("03", "B001", "123")


def test_parse_ocr_serie_letter_o_is_read_as_zero(rules: ParsingRules) -> None:
    """S0.3: FOO1-123 read with the letter O becomes serie F001 and numero 123."""
    fields = _fields(_ocr(f"RUC {SYNTHETIC_RUC}", "FOO1-123"), rules)

    assert (fields["serie"], fields["numero"]) == ("F001", "123")


def test_parse_ocr_document_without_total_has_no_total_field(rules: ParsingRules) -> None:
    """R-2.13: a document that never prints a total gives no total field."""
    fields = _fields(_ocr(f"RUC {SYNTHETIC_RUC}", "FACTURA ELECTRÓNICA", "F001-123"), rules)

    assert "total" not in fields


def test_parse_ocr_invalid_ruc_before_valid_ruc_is_skipped(rules: ParsingRules) -> None:
    """R-2.4: an 11-digit number with a bad check digit is not read as the issuer."""
    fields = _fields(_ocr(f"RUC {WRONG_CHECK_DIGIT_RUC}", f"RUC {SYNTHETIC_RUC}"), rules)

    assert fields["ruc_emisor"] == SYNTHETIC_RUC
    assert "num_doc_adq" not in fields


def test_parse_ocr_two_valid_rucs_read_issuer_and_buyer(rules: ParsingRules) -> None:
    """R-2.4: the first valid RUC is the issuer and the next distinct one the buyer."""
    result = parse_ocr(_ocr(f"RUC {SYNTHETIC_RUC}", f"RUC {SYNTHETIC_RUC_OTHER}"), rules)
    fields = result.reading.fields

    assert fields["ruc_emisor"].value == SYNTHETIC_RUC
    assert fields["num_doc_adq"].value == SYNTHETIC_RUC_OTHER
    assert fields["tipo_doc_adq"].value == "6"
    assert fields["tipo_doc_adq"].confidence == fields["num_doc_adq"].confidence


def test_parse_ocr_reads_charges_when_printed(rules: ParsingRules) -> None:
    """R-2.14: RECARGO and ICBPER lines are read as service_charge and icbper."""
    fields = _fields(_ocr(f"RUC {SYNTHETIC_RUC}", "RECARGO AL CONSUMO 5.00", "ICBPER 0.50"), rules)

    assert fields["service_charge"] == "5.00"
    assert fields["icbper"] == "0.50"


def test_parse_ocr_printed_url_reduced_to_scheme_host_path(rules: ParsingRules) -> None:
    """R-2.10: a printed URL keeps only its scheme, host and path, de-duplicated."""
    text = _ocr(
        f"RUC {SYNTHETIC_RUC}",
        "Consulte su documento en https://x.example/p?q=1",
        "Otra vez en https://x.example/p?q=1.",
    )

    result: OcrReading = parse_ocr(text, rules)

    assert result.printed_urls == ("https://x.example/p",)


def test_parse_ocr_low_confidence_words_lower_field_confidence(rules: ParsingRules) -> None:
    """R-2.6: a field's confidence is the mean confidence of the words its match covers."""
    text = OcrText(
        lines=(
            (OcrWord(text="RUC", confidence=DEFAULT_CONFIDENCE), OcrWord(text=SYNTHETIC_RUC, confidence=0.5)),
            (OcrWord(text="FACTURA", confidence=1.0), OcrWord(text="ELECTRÓNICA", confidence=0.4)),
        )
    )

    fields = parse_ocr(text, rules).reading.fields

    assert fields["ruc_emisor"].confidence == pytest.approx(0.5)
    assert fields["tipo"].confidence == pytest.approx(0.7)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param("IMPORTE TOTAL S/ 1,234.50", "1234.50", id="importe-total"),
        pytest.param("TOTAL: 12,50", "12.50", id="total-colon-comma"),
        pytest.param("TOTAL 49.90", "49.90", id="plain-total"),
        pytest.param("SUB TOTAL 10.00", None, id="sub-total-is-not-total"),
    ],
)
def test_parse_ocr_total_from_printed_line(rules: ParsingRules, line: str, expected: str | None) -> None:
    """R-2.14: the total is read from the amount the total keyword anchors."""
    fields = _fields(_ocr(f"RUC {SYNTHETIC_RUC}", line), rules)

    if expected is None:
        assert "total" not in fields
    else:
        assert fields["total"] == expected


def test_parse_ocr_dash_date_becomes_iso(rules: ParsingRules) -> None:
    """R-2.3: a dd-mm-yyyy date is read as an ISO date."""
    fields = _fields(_ocr(f"RUC {SYNTHETIC_RUC}", "Fecha: 13-11-2026"), rules)

    assert fields["fecha_emision"] == "2026-11-13"


def test_parse_ocr_serie_without_type_keyword_infers_tipo(rules: ParsingRules) -> None:
    """R-2.3: without a FACTURA keyword, the serie's first letter infers tipo."""
    fields = parse_ocr(_ocr(f"RUC {SYNTHETIC_RUC}", "F001-123"), rules).reading.fields

    assert fields["tipo"].value == "01"
    assert fields["tipo"].flag == "inferred"
    assert fields["tipo"].confidence == fields["serie"].confidence
