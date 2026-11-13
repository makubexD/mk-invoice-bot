"""Tests for the field model and the R-2.4 validators (R-2.4, R-2.13)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from invoicebot.extract.fields import Field, Reading
from invoicebot.extract.validate import (
    check_date,
    check_igv_rate,
    check_issuer,
    check_serie,
    check_tipo,
    check_totals,
    doubtful_fields,
    is_valid_ruc,
)

# Both values come from tests/synthetic_rucs.txt; the second exercises check digit 10 -> 0.
SYNTHETIC_RUC = "20000000001"
SYNTHETIC_RUC_CHECK_TEN = "51900000000"
RUC_WEIGHTS = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
TODAY = date(2026, 11, 13)
THRESHOLD = 0.8
RATES = (Decimal("0.18"), Decimal("0.105"), Decimal("0.10"))


def _ruc_with_check_digit(first_ten: str) -> str:
    """Build a synthetic RUC with its SUNAT check digit, so no valid literal lives here."""
    total = sum(int(digit) * weight for digit, weight in zip(first_ten, RUC_WEIGHTS, strict=True))
    check = 11 - (total % 11)
    if check == 10:
        check = 0
    elif check == 11:
        check = 1
    return first_ten + str(check)


def _wrong_check_digit(ruc: str) -> str:
    """Return ``ruc`` with its last digit changed, so the check digit is deliberately wrong."""
    return ruc[:-1] + str((int(ruc[-1]) + 1) % 10)


def _field(value: str, confidence: float = 1.0) -> Field:
    return Field(value=value, source="qr", confidence=confidence)


def _valid_fields(**overrides: Field) -> dict[str, Field]:
    """Return a complete, consistent reading that no R-2.4 check flags."""
    fields = {
        "ruc_emisor": _field(SYNTHETIC_RUC),
        "tipo": _field("01"),
        "serie": _field("F001"),
        "numero": _field("15160"),
        "fecha_emision": _field("2026-11-13"),
        "total": _field("49.90"),
        "base": _field("40.90"),
        "igv": _field("7.36"),
        "service_charge": _field("1.64"),
    }
    fields.update(overrides)
    return fields


def _reading(fields: dict[str, Field]) -> Reading:
    return Reading(fields=fields, document_url=None)


@pytest.mark.parametrize(
    ("first_ten", "last_digit"),
    [
        ("2000000001", "0"),  # raw check digit 10
        ("2000000006", "1"),  # raw check digit 11
    ],
)
def test_ruc_check_digit_adjustments(first_ten: str, last_digit: str) -> None:
    """R-2.4: check digits 10 and 11 are written as 0 and 1."""
    ruc = _ruc_with_check_digit(first_ten)

    assert ruc[-1] == last_digit
    assert is_valid_ruc(ruc)


@pytest.mark.parametrize(
    "ruc",
    [
        SYNTHETIC_RUC,
        SYNTHETIC_RUC_CHECK_TEN,
        _ruc_with_check_digit("2000000001"),
        _ruc_with_check_digit("2000000006"),
    ],
)
def test_ruc_with_valid_check_digit_is_accepted(ruc: str) -> None:
    """R-2.4: an 11-digit RUC whose check digit matches is valid."""
    assert is_valid_ruc(ruc)


@pytest.mark.parametrize(
    "ruc",
    [
        _wrong_check_digit(SYNTHETIC_RUC),
        "2000000000",
        "200000000012",
        "2000000000A",
        "",
        " 20000000001",
    ],
)
def test_ruc_with_wrong_shape_is_rejected(ruc: str) -> None:
    """R-2.4: a short, long, non-numeric or wrong-check-digit RUC is invalid."""
    assert not is_valid_ruc(ruc)


def test_check_issuer_returns_rule_ids() -> None:
    """BR-E02: only a RUC that fails its check digit returns the rule id."""
    assert check_issuer(SYNTHETIC_RUC) is None
    assert check_issuer(_wrong_check_digit(SYNTHETIC_RUC)) == "BR-E02"


@pytest.mark.parametrize("serie", ["F001", "B123", "E4A9", "0001", "1234"])
def test_serie_accepted_forms(serie: str) -> None:
    """R-2.4: a serie is a F/B/E prefixed code or four digits."""
    assert check_serie(serie)


@pytest.mark.parametrize("serie", ["F01", "X001", "f001", "F0011", "", "F00-", "F00 "])
def test_serie_rejected_forms(serie: str) -> None:
    """R-2.4: a serie outside the two patterns is rejected."""
    assert not check_serie(serie)


@pytest.mark.parametrize(
    ("offset_days", "expected"),
    [
        (0, None),
        (-365, None),
        (1, "BR-E03"),
        (-366, "BR-E03"),
    ],
)
def test_date_bounds(offset_days: int, expected: str | None) -> None:
    """BR-E03: a future date or one older than the limit is rejected; today and the edge pass."""
    fecha = TODAY + timedelta(days=offset_days)

    assert check_date(fecha, TODAY, max_age_days=365) == expected


def test_tipo_outside_supported_returns_rule_id() -> None:
    """BR-E05: a tipo outside the configured list is rejected."""
    assert check_tipo("01", ["01", "03"]) is None
    assert check_tipo("09", ["01", "03"]) == "BR-E05"


@pytest.mark.parametrize(
    ("total", "base", "igv", "charges", "discounts", "expected"),
    [
        ("49.90", "40.90", "7.36", "1.64", "0", True),  # 18 % with a service charge
        ("49.90", "45.16", "4.74", None, None, True),  # 10.5 %, no charge
        ("49.90", "42.29", "7.61", None, None, True),  # 18 %, no charge
        ("44.90", "40.90", "7.36", "1.64", "5.00", True),  # with a discount
        ("49.96", "40.90", "7.36", "1.64", "0", False),  # off by 0.06
        ("49.90", None, "7.36", "1.64", "0", True),  # missing base passes
        ("49.90", "40.90", None, "1.64", "0", True),  # missing IGV passes
    ],
)
def test_totals_check(
    total: str,
    base: str | None,
    igv: str | None,
    charges: str | None,
    discounts: str | None,
    expected: bool,
) -> None:
    """R-2.4: total matches base + IGV + charges - discounts within S/ 0.05."""
    amounts = [
        Decimal(total),
        Decimal(base) if base is not None else None,
        Decimal(igv) if igv is not None else None,
        Decimal(charges) if charges is not None else None,
        Decimal(discounts) if discounts is not None else None,
    ]

    assert check_totals(*amounts) is expected


@pytest.mark.parametrize(
    ("base", "igv", "rates", "expected"),
    [
        ("40.90", "7.36", ["0.18"], True),
        ("45.16", "4.74", ["0.18", "0.105", "0.10"], True),
        ("45.36", "4.54", ["0.18", "0.105", "0.10"], True),
        ("40.90", "4.74", ["0.18", "0.105", "0.10"], False),
    ],
)
def test_igv_rate_check(base: str, igv: str, rates: list[str], expected: bool) -> None:
    """R-2.4: IGV matches base times one of the configured rates within S/ 0.05."""
    assert check_igv_rate(Decimal(base), Decimal(igv), [Decimal(rate) for rate in rates]) is expected


def test_reading_missing_required_names_absent_fields() -> None:
    """R-2.13: missing_required names only the absent required fields, in order."""
    fields = _valid_fields()
    del fields["serie"]
    del fields["total"]

    assert _reading(fields).missing_required() == ("serie", "total")


def test_reading_missing_required_empty_when_complete() -> None:
    """R-2.13: a reading carrying every required field has nothing missing."""
    assert _reading(_valid_fields()).missing_required() == ()


def test_doubtful_fields_all_good() -> None:
    """R-2.4: a complete, consistent reading has no doubtful field."""
    assert doubtful_fields(_reading(_valid_fields()), THRESHOLD, RATES) == ()


def test_doubtful_fields_low_confidence() -> None:
    """R-2.4: a required field below the threshold is named."""
    reading = _reading(_valid_fields(numero=_field("15160", confidence=0.5)))

    assert doubtful_fields(reading, THRESHOLD, RATES) == ("numero",)


def test_doubtful_fields_bad_serie() -> None:
    """R-2.4: a serie outside the pattern is named even at full confidence."""
    reading = _reading(_valid_fields(serie=_field("X001")))

    assert doubtful_fields(reading, THRESHOLD, RATES) == ("serie",)


def test_doubtful_fields_totals_mismatch() -> None:
    """R-2.4: a total off by more than the tolerance is named."""
    reading = _reading(_valid_fields(total=_field("49.96")))

    assert doubtful_fields(reading, THRESHOLD, RATES) == ("total",)


def test_doubtful_fields_igv_rate_mismatch() -> None:
    """R-2.4: an IGV that matches none of the configured rates is named."""
    reading = _reading(
        _valid_fields(
            total=_field("48.40"),
            igv=_field("7.50"),
            service_charge=_field("0.00"),
        )
    )

    assert doubtful_fields(reading, THRESHOLD, RATES) == ("igv",)


@pytest.mark.parametrize("text", ["n/a", "NaN", "sNaN", "Infinity", "-Infinity"])
def test_doubtful_fields_total_that_is_not_a_finite_number_is_doubtful(text: str) -> None:
    """R-2.4: a total that is not a finite number is named instead of raising."""
    reading = _reading(_valid_fields(total=_field(text)))

    assert doubtful_fields(reading, THRESHOLD, RATES) == ("total",)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("base", ()),  # no base: neither the totals nor the IGV check can run
        ("igv", ()),
        ("service_charge", ("total",)),  # the charge drops out, so 40.90 + 7.36 != 49.90
    ],
)
def test_doubtful_fields_non_finite_part_amount_is_ignored(name: str, expected: tuple[str, ...]) -> None:
    """R-2.4: a NaN base, IGV or charge counts as absent instead of raising."""
    reading = _reading(_valid_fields(**{name: _field("NaN")}))

    assert doubtful_fields(reading, THRESHOLD, RATES) == expected


def test_doubtful_fields_names_exactly_the_failing_fields() -> None:
    """R-2.4: the result lists each failing field once, in a stable order."""
    reading = _reading(
        _valid_fields(
            ruc_emisor=_field(SYNTHETIC_RUC, confidence=0.1),
            serie=_field("X001"),
            total=_field("49.96", confidence=0.1),
        )
    )

    assert doubtful_fields(reading, THRESHOLD, RATES) == ("ruc_emisor", "serie", "total")
