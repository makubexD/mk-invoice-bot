"""Which sent invoices a reimbursement amount covers (R-7.12, D107), and its argument.

:func:`match_sets` enumerates the sets of candidates whose claimed amounts add up to
the target, up to ``limit`` sets, oldest invoices first. The candidate and its claimed
amount (R-7.13) live in :mod:`invoicebot.store.payments`, which owns their definition;
they are re-exported here. :func:`parse_pagado` turns the ``/pagado`` argument into
either an amount or an id list. The module is pure: it reads no clock and performs no
I/O; callers pass ``today`` in.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from invoicebot.store.payments import Candidate, claimed_cents

__all__ = ["Candidate", "PagadoAmount", "PagadoIds", "claimed_cents", "match_sets", "parse_pagado"]

MAX_PAGADO_IDS = 20
"""The most invoice ids a ``/pagado dd/mm #id #id …`` argument may name (R-7.12)."""

# The digit caps keep an amount inside NUMERIC(12,2) and an id inside INTEGER.
_AMOUNT_RE = re.compile(
    r"(?P<day>\d{1,2})/(?P<month>\d{1,2})\s+(?:[Ss]/\s*)?(?P<amount>\d{1,10}(?:[.,]\d{1,2})?)"
)
_IDS_RE = re.compile(r"(?P<day>\d{1,2})/(?P<month>\d{1,2})(?P<ids>(?:\s+#\d{1,9})+)")
_ID_RE = re.compile(r"#(\d+)")


@dataclass(frozen=True)
class PagadoAmount:
    """A ``/pagado dd/mm <amount>`` argument: the day paid and the amount (R-7.12)."""

    paid_on: date
    amount: Decimal


@dataclass(frozen=True)
class PagadoIds:
    """A ``/pagado dd/mm #id …`` argument: the day paid and the named invoices (R-7.12)."""

    paid_on: date
    invoice_ids: tuple[int, ...]


def parse_pagado(arg: str, today: date) -> PagadoAmount | PagadoIds | None:
    """Parse a ``/pagado`` argument, or return ``None`` when it is not acceptable (R-7.12).

    ``dd/mm <amount>`` yields a :class:`PagadoAmount` and ``dd/mm #id …`` a
    :class:`PagadoIds`. The year is ``today``'s, or the year before when the date would
    fall after ``today``. Anything else is rejected.
    """
    text = arg.strip()
    amount_match = _AMOUNT_RE.fullmatch(text)
    if amount_match is not None:
        return _parse_amount(amount_match, today)
    ids_match = _IDS_RE.fullmatch(text)
    if ids_match is not None:
        return _parse_ids(ids_match, today)
    return None


def _parse_amount(match: re.Match[str], today: date) -> PagadoAmount | None:
    """Build the amount form, or ``None`` for a bad date or a non-positive amount (R-7.12)."""
    paid_on = _resolve_date(int(match["day"]), int(match["month"]), today)
    if paid_on is None:
        return None
    amount = Decimal(match["amount"].replace(",", "."))
    if amount <= 0:
        return None
    return PagadoAmount(paid_on, amount)


def _parse_ids(match: re.Match[str], today: date) -> PagadoIds | None:
    """Build the id form, or ``None`` for a bad date or too many ids (R-7.12)."""
    named_ids = [int(value) for value in _ID_RE.findall(match["ids"])]
    if len(named_ids) > MAX_PAGADO_IDS:
        return None
    paid_on = _resolve_date(int(match["day"]), int(match["month"]), today)
    if paid_on is None:
        return None
    return PagadoIds(paid_on, tuple(dict.fromkeys(named_ids)))


def _resolve_date(day: int, month: int, today: date) -> date | None:
    """Return this year's ``dd/mm``, or last year's when it is after ``today`` (R-7.12)."""
    try:
        candidate = date(today.year, month, day)
        if candidate <= today:
            return candidate
        return date(today.year - 1, month, day)
    except ValueError:
        return None


def match_sets(candidates: Sequence[Candidate], target_cents: int, limit: int = 3) -> list[tuple[int, ...]]:
    """Return up to ``limit`` invoice-id sets whose claimed amounts sum to the target (R-7.12, D107).

    Candidates are searched oldest first, and each set is a tuple of invoice ids in that
    order. The result is sorted by the oldest invoice's ``fecha``, then by set size.
    """
    if target_cents <= 0 or limit <= 0:
        return []
    eligible = tuple(
        sorted(
            (candidate for candidate in candidates if candidate.claimed_cents > 0),
            key=lambda candidate: (candidate.fecha, candidate.invoice_id),
        )
    )
    found = _search(eligible, target_cents, limit)
    oldest = {candidate.invoice_id: candidate.fecha for candidate in eligible}
    found.sort(key=lambda ids: (oldest[ids[0]], len(ids)))
    return found


def _search(candidates: tuple[Candidate, ...], target_cents: int, limit: int) -> list[tuple[int, ...]]:
    """Depth-first search for at most ``limit`` subsets summing to the target (R-7.12)."""
    suffix_sums = _suffix_sums(candidates)
    found: list[tuple[int, ...]] = []

    def walk(start: int, running: int, chosen: list[int]) -> None:
        if running == target_cents:
            found.append(tuple(chosen))
            return
        for index in range(start, len(candidates)):
            if len(found) >= limit:
                return
            if running + suffix_sums[index] < target_cents:
                return
            claimed = candidates[index].claimed_cents
            if running + claimed > target_cents:
                continue
            chosen.append(candidates[index].invoice_id)
            walk(index + 1, running + claimed, chosen)
            chosen.pop()

    walk(0, 0, [])
    return found


def _suffix_sums(candidates: tuple[Candidate, ...]) -> tuple[int, ...]:
    """Return the sum of ``claimed_cents`` from each index to the end, plus a zero (R-7.12)."""
    sums = [0] * (len(candidates) + 1)
    for index in range(len(candidates) - 1, -1, -1):
        sums[index] = sums[index + 1] + candidates[index].claimed_cents
    return tuple(sums)
