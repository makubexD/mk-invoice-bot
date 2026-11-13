"""Pure validators for extracted invoice fields (R-2.4).

Each validator returns the rule id that applies (or ``None``/``True`` when it passes), so the
caller decides the state and the message. Amounts are ``Decimal``; text read from a channel
stays text until it is parsed here.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Final, Literal

from invoicebot.extract.fields import REQUIRED, Reading

TOLERANCE = Decimal("0.05")
ZERO = Decimal("0")
RUC_WEIGHTS = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
SERIE_PATTERN = re.compile(r"(?:[FBE][A-Z0-9]{3}|[0-9]{4})")
CHARGE_FIELDS = ("service_charge", "icbper")

#: The issuer RUC fails its check digit (BR-E02).
RULE_BAD_RUC: Final = "BR-E02"
#: The document type is not supported (BR-E05).
RULE_UNSUPPORTED_TIPO: Final = "BR-E05"


def is_valid_ruc(text: str) -> bool:
    """Return whether ``text`` is an 11-digit number with a valid SUNAT check digit (R-2.4)."""
    if len(text) != 11 or not text.isascii() or not text.isdigit():
        return False
    total = sum(int(digit) * weight for digit, weight in zip(text[:10], RUC_WEIGHTS, strict=True))
    check = 11 - (total % 11)
    if check == 10:
        check = 0
    elif check == 11:
        check = 1
    return check == int(text[10])


def check_issuer(ruc: str) -> Literal["BR-E02"] | None:
    """Return ``BR-E02`` when the issuer RUC fails its check digit (BR-E02)."""
    return None if is_valid_ruc(ruc) else RULE_BAD_RUC


def check_serie(serie: str) -> bool:
    """Return whether ``serie`` matches ``[FBE][A-Z0-9]{3}`` or four digits (R-2.4)."""
    return SERIE_PATTERN.fullmatch(serie) is not None


def check_date(fecha: date, today: date, max_age_days: int) -> Literal["BR-E03"] | None:
    """Return ``BR-E03`` when ``fecha`` is in the future or older than the limit (BR-E03)."""
    if fecha > today or fecha < today - timedelta(days=max_age_days):
        return "BR-E03"
    return None


def check_tipo(tipo: str, supported: Collection[str]) -> Literal["BR-E05"] | None:
    """Return ``BR-E05`` when ``tipo`` is outside ``supported`` (BR-E05)."""
    return None if tipo in supported else RULE_UNSUPPORTED_TIPO


def check_totals(
    total: Decimal,
    base: Decimal | None,
    igv: Decimal | None,
    charges: Decimal | None,
    discounts: Decimal | None,
) -> bool:
    """Return whether ``total`` matches base + IGV + charges - discounts (R-2.4).

    The check passes when base or IGV is missing, because there is nothing to compare.
    """
    if base is None or igv is None:
        return True
    expected = base + igv + (charges if charges is not None else ZERO)
    expected -= discounts if discounts is not None else ZERO
    return abs(total - expected) <= TOLERANCE


def check_igv_rate(base: Decimal, igv: Decimal, rates: Sequence[Decimal]) -> bool:
    """Return whether ``igv`` matches ``base`` times one of the configured ``rates`` (R-2.4)."""
    return any(abs(igv - base * rate) <= TOLERANCE for rate in rates)


def doubtful_fields(
    reading: Reading,
    threshold: float,
    rates: Sequence[Decimal],
) -> tuple[str, ...]:
    """Return the field names whose confidence or R-2.4 check fails, in a stable order.

    Required fields below ``threshold`` and the ``serie``/``total`` checks come first, in
    :data:`REQUIRED` order; ``igv`` follows when it matches none of ``rates``. A ``total``
    that is present but not a finite number is doubtful too.
    """
    doubtful = set(_low_confidence(reading, threshold))
    doubtful.update(_failed_checks(reading, rates))
    ordered = [name for name in REQUIRED if name in doubtful]
    if "igv" in doubtful:
        ordered.append("igv")
    return tuple(ordered)


def _low_confidence(reading: Reading, threshold: float) -> list[str]:
    """Return the required fields carried by the reading below ``threshold``."""
    return [
        name
        for name in REQUIRED
        if (field := reading.fields.get(name)) is not None and field.confidence < threshold
    ]


def _failed_checks(reading: Reading, rates: Sequence[Decimal]) -> list[str]:
    """Return ``serie``, ``total`` and ``igv`` when their R-2.4 check fails."""
    failed: list[str] = []
    serie = reading.fields.get("serie")
    if serie is not None and not check_serie(serie.value):
        failed.append("serie")

    base = reading.amount("base")
    igv = reading.amount("igv")
    total = reading.amount("total")
    if total is None and "total" in reading.fields:
        failed.append("total")
    elif total is not None:
        charges = _sum_amounts(reading, CHARGE_FIELDS)
        discounts = reading.amount("discounts")
        if not check_totals(total, base, igv, charges, discounts):
            failed.append("total")

    if base is not None and igv is not None and not check_igv_rate(base, igv, rates):
        failed.append("igv")
    return failed


def _sum_amounts(reading: Reading, names: Sequence[str]) -> Decimal | None:
    """Return the sum of the present amounts in ``names``, or ``None`` when none is present."""
    present = [amount for name in names if (amount := reading.amount(name)) is not None]
    return sum(present, ZERO) if present else None
