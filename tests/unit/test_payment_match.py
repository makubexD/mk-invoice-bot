"""Unit tests for the pure payment matcher (R-7.12, R-7.13, D107)."""

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from invoicebot.pipeline.payment_match import Candidate, claimed_cents, match_sets


def _candidate(invoice_id: int, day: int, claimed_cents: int) -> Candidate:
    return Candidate(invoice_id, datetime(2026, 1, day, tzinfo=UTC), claimed_cents)


def test_match_sets_returns_one_exact_set() -> None:
    """R-7.12: the candidates whose claimed amounts add up to the target form one set."""
    candidates = [_candidate(1, 1, 1000), _candidate(2, 2, 2000), _candidate(3, 3, 4000)]

    assert match_sets(candidates, 3000) == [(1, 2)]


def test_match_sets_returns_two_sets_oldest_first() -> None:
    """R-7.12: two matching sets come back ordered by their oldest invoice."""
    candidates = [
        _candidate(1, 1, 500),
        _candidate(2, 2, 300),
        _candidate(3, 3, 200),
        _candidate(4, 4, 700),
    ]

    assert match_sets(candidates, 500, limit=2) == [(1,), (2, 3)]


def test_match_sets_caps_at_limit() -> None:
    """R-7.12: five possible sets, only the first three are returned."""
    candidates = [
        _candidate(1, 1, 100),
        _candidate(2, 2, 100),
        _candidate(3, 3, 200),
        _candidate(4, 4, 200),
        _candidate(5, 5, 300),
    ]

    sets = match_sets(candidates, 300)

    assert len(sets) == 3
    assert all(
        sum(next(c.claimed_cents for c in candidates if c.invoice_id == i) for i in ids) == 300
        for ids in sets
    )


def test_match_sets_returns_empty_when_no_subset_matches() -> None:
    """R-7.12: no set adds up to the target, so nothing matches."""
    candidates = [_candidate(1, 1, 100), _candidate(2, 2, 200)]

    assert match_sets(candidates, 500) == []


def test_match_sets_returns_empty_for_no_candidates() -> None:
    """R-7.12: an empty candidate list has no matching set."""
    assert match_sets([], 500) == []


def test_match_sets_prunes_twenty_candidates_quickly() -> None:
    """R-7.12: 20 candidates whose total is below the target finish under 1 s (suffix-sum prune)."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candidates = [Candidate(index, start + timedelta(days=index), 100) for index in range(20)]

    started = time.perf_counter()
    sets = match_sets(candidates, 2500)
    elapsed = time.perf_counter() - started

    assert sets == []
    assert elapsed < 1.0


@pytest.mark.parametrize(
    ("total", "cap", "expected"),
    [
        (Decimal("150.00"), Decimal("120.00"), 12000),
        (Decimal("144.60"), None, 14460),
        (Decimal("42.00"), Decimal("50.00"), 4200),
    ],
)
def test_claimed_cents_caps_and_converts_to_cents(total: Decimal, cap: Decimal | None, expected: int) -> None:
    """R-7.13: the claimed amount is min(total, cap), converted to cents."""
    assert claimed_cents(total, cap) == expected
