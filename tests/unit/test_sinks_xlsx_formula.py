"""Tests for the small Excel formula subset (R-7.4, D87)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from invoicebot.sinks.xlsx_formula import UnsupportedFormula, parse


@pytest.mark.parametrize(
    ("formula", "values", "expected"),
    [
        ("MIN(I16, 50)", {"I16": Decimal("63.5")}, Decimal("50")),
        ("MIN(I16, 50)", {"I16": Decimal("42")}, Decimal("42")),
        ("F16+G16+H16", {"F16": Decimal("40"), "G16": Decimal("7.2"), "H16": Decimal("0.8")}, Decimal("48")),
        ("SUM(F16:H16)", {"F16": Decimal("40"), "G16": Decimal("7.2"), "H16": Decimal("0.8")}, Decimal("48")),
        ("-A1*(B1+2)/4", {"A1": Decimal("2"), "B1": Decimal("6")}, Decimal("-4")),
        ("min(I16, 50)", {"I16": Decimal("63.5")}, Decimal("50")),
        ("MIN(A1:A3)", {"A1": Decimal("5"), "A2": Decimal("3")}, Decimal("3")),
        ("MAX(A1:A3)", {"A1": Decimal("-5"), "A2": Decimal("-3")}, Decimal("-3")),
    ],
)
def test_evaluate_returns_the_value(formula: str, values: dict[str, Decimal], expected: Decimal) -> None:
    """R-7.4: the supported arithmetic and functions evaluate against the cell values."""
    assert parse(formula).evaluate(values) == expected


def test_dollar_refs_name_the_same_cell() -> None:
    """R-7.4: $F$16 and F16 name the same cell, and the column is upper-cased."""
    dollar = parse("$f$16")
    plain = parse("F16")

    assert dollar.refs() == frozenset({"F16"})
    assert dollar.evaluate({"F16": Decimal("5")}) == plain.evaluate({"F16": Decimal("5")})


def test_refs_expand_a_range_to_its_cells() -> None:
    """R-7.4: a range argument reads every cell it covers."""
    assert parse("SUM(A1:B2)").refs() == frozenset({"A1", "B1", "A2", "B2"})


@pytest.mark.parametrize(
    ("formula", "match"),
    [
        ("IF(A1>1,1,0)", "IF"),
        ("Sheet2!A1", "sheet-qualified"),
        ('"x"', "string literal"),
        ("A1^2", r"\^"),
    ],
)
def test_unsupported_formula_raises(formula: str, match: str) -> None:
    """R-7.4: a function, sheet qualifier, string or operator outside the subset is refused."""
    with pytest.raises(UnsupportedFormula, match=match):
        parse(formula)


def test_range_over_1000_cells_raises() -> None:
    """R-7.4: a range wider than 1000 cells is refused."""
    with pytest.raises(UnsupportedFormula, match="1000"):
        parse("SUM(A1:A1001)")


def test_range_outside_a_function_raises() -> None:
    """R-7.4: a range is only allowed as a function argument."""
    with pytest.raises(UnsupportedFormula, match="range is only allowed"):
        parse("A1:B2")


def test_missing_cell_evaluates_to_zero() -> None:
    """R-7.4: a cell missing from the values reads as zero, as in Excel."""
    assert parse("A1+B1").evaluate({"A1": Decimal("5")}) == Decimal("5")


def test_min_with_no_values_is_zero() -> None:
    """R-7.4: MIN with no arguments is zero, as in Excel."""
    assert parse("MIN()").evaluate({}) == Decimal("0")


def test_division_by_zero_raises() -> None:
    """R-7.4: division by zero is refused."""
    with pytest.raises(UnsupportedFormula, match="division by zero"):
        parse("A1/B1").evaluate({"B1": Decimal("0")})


def test_unary_plus_returns_its_operand_unchanged() -> None:
    """R-7.4: a unary plus is accepted and leaves the value as it is."""
    assert parse("=+A1+B1").evaluate({"A1": Decimal("2"), "B1": Decimal("3")}) == Decimal("5")


def test_double_unary_minus_still_negates_twice() -> None:
    """R-7.4: a double unary minus cancels out and yields the operand."""
    assert parse("=--A1").evaluate({"A1": Decimal("7")}) == Decimal("7")


def test_64_nested_parentheses_parse_and_evaluate() -> None:
    """R-7.4: 64 levels of parentheses are the deepest nesting accepted, and they evaluate."""
    formula = "=" + "(" * 64 + "1" + ")" * 64

    assert parse(formula).evaluate({}) == Decimal("1")


def test_65_nested_parentheses_raise_unsupported_formula() -> None:
    """R-7.4: 65 levels of parentheses raise UnsupportedFormula, never RecursionError."""
    formula = "=" + "(" * 65 + "1" + ")" * 65

    with pytest.raises(UnsupportedFormula, match="nested deeper than 64") as raised:
        parse(formula)

    assert not isinstance(raised.value, RecursionError)


def test_1000_leading_minus_signs_raise_unsupported_formula() -> None:
    """R-7.4: 1000 leading minus signs raise UnsupportedFormula, never RecursionError."""
    formula = "=" + "-" * 1000 + "1"

    with pytest.raises(UnsupportedFormula, match="nested deeper than 64") as raised:
        parse(formula)

    assert not isinstance(raised.value, RecursionError)


@pytest.mark.parametrize(
    ("formula", "expected"),
    [
        ("=--1", Decimal("1")),
        ("=+-+1", Decimal("-1")),
    ],
)
def test_leading_signs_still_evaluate(formula: str, expected: Decimal) -> None:
    """R-7.4: combinations of leading plus and minus signs evaluate as before."""
    assert parse(formula).evaluate({}) == expected
