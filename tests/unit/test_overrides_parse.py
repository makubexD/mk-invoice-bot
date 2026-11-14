"""Unit tests for chat override parsing (R-7.4, D70)."""

import pytest

from invoicebot.pipeline.overrides import MAX_OVERRIDE_CHARS, parse_overrides


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("motivo=Viaje", {"motivo": "Viaje"}),
        ("Motivo=Viaje", {"motivo": "Viaje"}),
        ("DESCRIPCION=Almuerzo", {"descripcion": "Almuerzo"}),
        ("  motivo = Viaje de trabajo  ", {"motivo": "Viaje de trabajo"}),
        ("motivo=Viaje y hotel", {"motivo": "Viaje y hotel"}),
        ("motivo=Viaje\ndescripcion=Almuerzo", {"motivo": "Viaje", "descripcion": "Almuerzo"}),
        ("hola\nmotivo=Viaje\nadiós", {"motivo": "Viaje"}),
    ],
)
def test_parse_overrides_reads_both_keys(text: str, expected: dict[str, str]) -> None:
    """R-7.4, D70: motivo= and descripcion= are read case-insensitively to the end of their line."""
    assert parse_overrides(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "hola",
        "motivo",
        "motivo=",
        "=Viaje",
        "ruc=20123456789",
        "motivos=Viaje",
    ],
)
def test_parse_overrides_ignores_other_text(text: str) -> None:
    """R-7.4, D70: a missing =, an empty value and any other key are ignored."""
    assert parse_overrides(text) == {}


@pytest.mark.parametrize(
    "value",
    [
        "x" * (MAX_OVERRIDE_CHARS + 1),
        "Viaje\tida",
        "Viaje\x07vuelta",
        "Viaje\x00final",
        "Viaje\u202evuelta",
    ],
)
def test_parse_overrides_drops_a_bad_value(value: str) -> None:
    """R-7.4, R-12.8: a value over 120 characters or with an unsafe character is dropped."""
    assert parse_overrides(f"motivo={value}") == {}


def test_parse_overrides_keeps_a_value_of_exactly_the_cap() -> None:
    """R-7.4: a value of exactly 120 characters is accepted."""
    value = "x" * MAX_OVERRIDE_CHARS
    assert parse_overrides(f"motivo={value}") == {"motivo": value}
