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


def test_load_rules_unknown_amount_field_raises(tmp_path: Path) -> None:
    """S3.6: an amount rule under a field name the readers do not produce is rejected at load."""
    document = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    document["amounts"]["totl"] = ["TOTAL"]
    path = tmp_path / "parsing_rules.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(ValueError, match="totl"):
        load_rules(path)


def test_load_rules_unknown_document_type_raises(tmp_path: Path) -> None:
    """S3.6: a tipo rule mapping to a document type code the OCR reader cannot produce is rejected."""
    document = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    document["tipo"]["99"] = ["NOTA DE CRÉDITO"]
    path = tmp_path / "parsing_rules.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(ValueError, match="99"):
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


@pytest.mark.parametrize(
    ("lines", "numero"),
    [
        pytest.param(
            ("AV. JAVIER PRADO ESTE 4200 - 302 SURCO", "FACTURA ELECTRONICA", "F001-123"),
            "123",
            id="address-before-serie",
        ),
        pytest.param(("FECHA: 2026-11-10", "F001-123"), "123", id="iso-date-before-serie"),
        pytest.param(("BASE-12 F001-9",), "9", id="word-before-serie"),
    ],
)
def test_parse_ocr_serie_numero_skips_number_like_noise(
    rules: ParsingRules, lines: tuple[str, ...], numero: str
) -> None:
    """R-2.3: a dash-joined number or word before the printed serie is not read as serie-numero."""
    fields = _fields(_ocr(*lines), rules)

    assert (fields["serie"], fields["numero"]) == ("F001", numero)


def test_parse_ocr_four_digit_serie_is_read_without_a_letter_serie(rules: ParsingRules) -> None:
    """R-2.3: a four-digit serie is still read when no letter serie is printed."""
    fields = _fields(_ocr(f"RUC {SYNTHETIC_RUC}", "0001-123"), rules)

    assert (fields["serie"], fields["numero"]) == ("0001", "123")


def test_parse_ocr_alphanumeric_letter_serie_outranks_an_address(rules: ParsingRules) -> None:
    """R-2.4: a valid letter serie with letters after the first, such as FA01, is still a letter serie."""
    fields = _fields(_ocr("JR. PUNO 1050 - 201", "FA01-55"), rules)

    assert (fields["serie"], fields["numero"]) == ("FA01", "55")


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
    assert fields["tipo_doc_adq"].flag == "inferred"
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


def test_parse_ocr_malformed_printed_url_is_dropped(rules: ParsingRules) -> None:
    """R-2.10: printed text like ``http://[x`` yields no URL and does not raise."""
    result = parse_ocr(_ocr(f"RUC {SYNTHETIC_RUC}", "Consulte http://[x"), rules)

    assert result.printed_urls == ()


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


@pytest.mark.parametrize(
    ("line", "field", "expected"),
    [
        pytest.param("I.G.V. 18.00% 9.00", "igv", "9.00", id="igv-rate-before-amount"),
        pytest.param("IGV (18%) S/ 9.00", "igv", "9.00", id="igv-rate-in-parentheses"),
        pytest.param("SUB-TOTAL 50.00", "total", None, id="sub-total-is-not-total"),
        pytest.param("SERVICIO DE LAVADO 25.00", "service_charge", None, id="service-line-is-not-a-charge"),
        pytest.param("RECARGO POR SERVICIO 5.00", "service_charge", "5.00", id="recargo-is-a-charge"),
    ],
)
def test_parse_ocr_amount_line_not_misread(
    rules: ParsingRules, line: str, field: str, expected: str | None
) -> None:
    """R-2.14: a printed rate, sub-total or service line does not hide the amount behind it."""
    fields = _fields(_ocr(f"RUC {SYNTHETIC_RUC}", line), rules)

    if expected is None:
        assert field not in fields
    else:
        assert fields[field] == expected


def test_parse_ocr_dash_date_becomes_iso(rules: ParsingRules) -> None:
    """R-2.3: a dd-mm-yyyy date is read as an ISO date."""
    fields = _fields(_ocr(f"RUC {SYNTHETIC_RUC}", "Fecha: 13-11-2026"), rules)

    assert fields["fecha_emision"] == "2026-11-13"


@pytest.mark.parametrize(
    ("lines", "field", "misread"),
    [
        pytest.param(("TOTAL ITEMS 3", "125.50"), "total", "3125.50", id="total-across-lines"),
        pytest.param(("IGV 18", "100.00"), "igv", "18100.00", id="igv-across-lines"),
    ],
)
def test_parse_ocr_amount_does_not_join_across_lines(
    rules: ParsingRules, lines: tuple[str, ...], field: str, misread: str
) -> None:
    """R-2.14: a space in the thousands group never spans a line break."""
    fields = _fields(_ocr(f"RUC {SYNTHETIC_RUC}", *lines), rules)

    assert field not in fields
    assert fields.get(field) != misread


def test_parse_ocr_serie_without_type_keyword_infers_tipo(rules: ParsingRules) -> None:
    """R-2.3: without a FACTURA keyword, the serie's first letter infers tipo."""
    fields = parse_ocr(_ocr(f"RUC {SYNTHETIC_RUC}", "F001-123"), rules).reading.fields

    assert fields["tipo"].value == "01"
    assert fields["tipo"].flag == "inferred"
    assert fields["tipo"].confidence == fields["serie"].confidence
