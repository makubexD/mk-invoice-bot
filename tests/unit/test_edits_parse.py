"""Unit tests for chat edit parsing (R-3.5, D97)."""

import pytest

from invoicebot.pipeline.edits import EditError, Edits, parse_edits
from invoicebot.pipeline.overrides import MAX_OVERRIDE_CHARS

VALID_RUC = "20000000001"
BAD_RUC = "20000000002"


def test_parse_edits_reads_total_and_date() -> None:
    """R-3.5: total=45,50 and fecha=05/11/2026 become 45.50 and 2026-11-05."""
    edits = parse_edits("total=45,50\nfecha=05/11/2026")
    assert edits.fields == {"total": "45.50", "fecha_emision": "2026-11-05"}
    assert edits.errors == ()


@pytest.mark.parametrize(
    ("line", "field", "value"),
    [
        (f"ruc={VALID_RUC}", "ruc_emisor", VALID_RUC),
        (f"RUC = {VALID_RUC}", "ruc_emisor", VALID_RUC),
        ("fecha=05/11/2026", "fecha_emision", "2026-11-05"),
        ("fecha=05-11-2026", "fecha_emision", "2026-11-05"),
        ("cliente=12345678", "num_doc_adq", "12345678"),
        (f"cliente={VALID_RUC}", "num_doc_adq", VALID_RUC),
    ],
)
def test_parse_edits_accepts_the_aliases(line: str, field: str, value: str) -> None:
    """R-3.5, D97: ruc, fecha and cliente map to their canonical field names."""
    assert parse_edits(line).fields == {field: value}


def test_parse_edits_ignores_case_and_accents_in_keys() -> None:
    """R-3.5, D97: a key is case-insensitive and accent-free after strip."""
    edits = parse_edits(f"  RÚC = {VALID_RUC}  ")
    assert edits.fields == {"ruc_emisor": VALID_RUC}
    assert edits.errors == ()


@pytest.mark.parametrize(
    "line",
    ["motivo=almuerzo", "Motivo=almuerzo", "Descripción=almuerzo", "descripcion = almuerzo"],
)
def test_parse_edits_skips_the_override_keys(line: str) -> None:
    """R-3.5, D97: motivo and descripcion stay with overrides.py, so they are not edits."""
    assert parse_edits(line) == Edits(fields={}, errors=())


def test_parse_edits_ignores_lines_without_equals() -> None:
    """R-3.5: a line without = is neither an edit nor an error."""
    edits = parse_edits("hola\ntotal=45.50\nadiós")
    assert edits.fields == {"total": "45.50"}
    assert edits.errors == ()


@pytest.mark.parametrize("value", [BAD_RUC, "123"])
def test_parse_edits_reports_a_bad_ruc_with_br_e02(value: str) -> None:
    """R-3.5, BR-E02: a RUC that is not 11 digits with a valid check digit gets BR-E02."""
    edits = parse_edits(f"ruc={value}")
    assert edits.fields == {}
    assert edits.errors == (EditError("BR-E02", f"ruc={value}", "ruc_emisor", value),)


@pytest.mark.parametrize("value", ["31/02/2026", "05/13/2026", "2026-11-05", "5/11/2026"])
def test_parse_edits_reports_a_bad_date_with_br_e03(value: str) -> None:
    """R-3.5, BR-E03: an impossible or malformed date gets BR-E03."""
    edits = parse_edits(f"fecha={value}")
    assert edits.fields == {}
    assert edits.errors == (EditError("BR-E03", f"fecha={value}", "fecha_emision", value),)


@pytest.mark.parametrize(
    ("line", "serie", "tipo"),
    [("serie=b001", "B001", "03"), ("serie=F123", "F123", "01"), ("serie= e001 ", "E001", None)],
)
def test_parse_edits_infers_tipo_from_the_serie(line: str, serie: str, tipo: str | None) -> None:
    """R-3.5, D97: a serie without tipo infers 03 from B and 01 from F, never from E."""
    fields = parse_edits(line).fields
    assert fields["serie"] == serie
    assert fields.get("tipo") == tipo


@pytest.mark.parametrize("line", ["serie=b001\ntipo=01", "tipo=01\nserie=b001"])
def test_parse_edits_keeps_an_explicit_tipo(line: str) -> None:
    """R-3.5, D97: an explicit tipo wins over the serie's inferred one, in either order."""
    fields = parse_edits(line).fields
    assert fields == {"serie": "B001", "tipo": "01"}


@pytest.mark.parametrize("value", ["X001", "F01", "12345", "b00"])
def test_parse_edits_reports_a_bad_serie(value: str) -> None:
    """R-2.4: a serie outside the accepted shapes is an error with no rule id."""
    edits = parse_edits(f"serie={value}")
    assert edits.fields == {}
    assert edits.errors == (EditError(None, f"serie={value}", "serie", value),)


def test_parse_edits_accepts_a_four_digit_serie_without_tipo() -> None:
    """R-2.4: a four-digit serie is valid and implies no document type."""
    assert parse_edits("serie=0001").fields == {"serie": "0001"}


@pytest.mark.parametrize(("value", "expected"), [("45,50", "45.50"), ("45.5", "45.5"), ("45", "45")])
def test_parse_edits_normalises_a_total(value: str, expected: str) -> None:
    """R-3.5: a total accepts a dot or a comma and at most two decimals, kept as given."""
    assert parse_edits(f"total={value}").fields == {"total": expected}


@pytest.mark.parametrize("value", ["45.500", "45,", ".50", "1.2.3", "45 reais", ""])
def test_parse_edits_reports_a_bad_total(value: str) -> None:
    """R-3.5: a total outside the amount shape is an error with no rule id."""
    edits = parse_edits(f"total={value}")
    assert edits.fields == {}
    assert edits.errors == (EditError(None, f"total={value}", "total", value),)


@pytest.mark.parametrize(("value", "expected"), [("000123", "123"), ("12345678", "12345678")])
def test_parse_edits_drops_leading_zeros_from_numero(value: str, expected: str) -> None:
    """R-3.5: a numero is 1-8 digits and loses its leading zeros."""
    assert parse_edits(f"numero={value}").fields == {"numero": expected}


@pytest.mark.parametrize("value", ["123456789", "12a", ""])
def test_parse_edits_reports_a_bad_numero(value: str) -> None:
    """R-3.5: a numero that is not 1-8 digits is an error with no rule id."""
    edits = parse_edits(f"numero={value}")
    assert edits.fields == {}
    assert edits.errors == (EditError(None, f"numero={value}", "numero", value),)


@pytest.mark.parametrize("value", ["1234567", "123456789", "1234567a"])
def test_parse_edits_reports_a_bad_cliente(value: str) -> None:
    """R-3.5: a cliente that is not 8 or 11 digits is an error with no rule id."""
    edits = parse_edits(f"cliente={value}")
    assert edits.fields == {}
    assert edits.errors == (EditError(None, f"cliente={value}", "num_doc_adq", value),)


@pytest.mark.parametrize("value", ["01", "03"])
def test_parse_edits_accepts_a_supported_tipo(value: str) -> None:
    """R-3.5: tipo accepts only 01 and 03."""
    assert parse_edits(f"tipo={value}").fields == {"tipo": value}


def test_parse_edits_reports_another_tipo() -> None:
    """R-3.5: a tipo outside 01 and 03 is an error with no rule id."""
    edits = parse_edits("tipo=1")
    assert edits.fields == {}
    assert edits.errors == (EditError(None, "tipo=1", "tipo", "1"),)


def test_parse_edits_reports_an_unknown_key() -> None:
    """R-3.5: an unknown key is an error with no rule id and no field."""
    edits = parse_edits(" color = rojo ")
    assert edits.fields == {}
    assert edits.errors == (EditError(None, "color = rojo", None, "rojo"),)


def test_parse_edits_reports_a_value_over_the_cap() -> None:
    """R-3.5, R-12.8: a value longer than MAX_OVERRIDE_CHARS is an error."""
    value = "1" * (MAX_OVERRIDE_CHARS + 1)
    line = f"total={value}"
    edits = parse_edits(line)
    assert edits.fields == {}
    assert edits.errors == (EditError(None, line, "total", value),)


def test_parse_edits_accepts_ten_assignment_lines() -> None:
    """R-3.5: ten lines with = are within the cap."""
    text = "\n".join(f"numero={number}" for number in range(1, 11))
    assert parse_edits(text).errors == ()


def test_parse_edits_reports_the_eleventh_assignment() -> None:
    """R-3.5: the eleventh line with = is an error and its value is not stored."""
    text = "\n".join(f"numero={number}" for number in range(1, 12))
    edits = parse_edits(text)
    assert edits.fields == {"numero": "10"}
    assert edits.errors == (EditError(None, "numero=11", "numero", "11"),)


def test_parse_edits_does_not_count_lines_without_equals() -> None:
    """R-3.5: only lines with = count toward the ten-line cap."""
    text = "\n".join([*(f"numero={number}" for number in range(1, 11)), "hola", "adiós"])
    assert parse_edits(text).errors == ()


def test_parse_edits_keeps_good_lines_next_to_errors() -> None:
    """R-3.5: a rejected line does not discard the good ones in the same message."""
    edits = parse_edits(f"total=10\nruc={BAD_RUC}\nnumero=7")
    assert edits.fields == {"total": "10", "numero": "7"}
    assert edits.errors == (EditError("BR-E02", f"ruc={BAD_RUC}", "ruc_emisor", BAD_RUC),)
