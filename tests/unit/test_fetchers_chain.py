"""Tests for the fetcher chain decision (R-5.1, D65)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from invoicebot.fetchers.base import FetchReason, FetchResult
from invoicebot.fetchers.chain import (
    Attempt,
    Done,
    Exhausted,
    Run,
    Wait,
    decide,
    retry_delay,
)

FETCHER = "portal_registry"
ORDER = ["qr_url", FETCHER, "email_inbox"]
REGISTERED = {FETCHER}


def schedule_for(fetcher: str) -> list[timedelta]:
    del fetcher
    return [timedelta(hours=1), timedelta(hours=6)]


def attempts(reason: FetchReason, count: int) -> list[Attempt]:
    return [Attempt(FETCHER, reason) for _ in range(count)]


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        pytest.param([], Run(FETCHER), id="no-attempts"),
        pytest.param(attempts(FetchReason.OK, 1), Done(FETCHER), id="ok"),
        pytest.param(attempts(FetchReason.NOT_FOUND_YET, 1), Wait(timedelta(hours=1), FETCHER), id="wait-1h"),
        pytest.param(attempts(FetchReason.NOT_FOUND_YET, 2), Wait(timedelta(hours=6), FETCHER), id="wait-6h"),
        pytest.param(attempts(FetchReason.NOT_FOUND_YET, 3), Exhausted(), id="schedule-used-up"),
        pytest.param(attempts(FetchReason.TECHNICAL, 1), Exhausted(), id="technical-is-final"),
        pytest.param(attempts(FetchReason.XML_MISMATCH, 1), Exhausted(), id="xml-mismatch-is-final"),
    ],
)
def test_decide_one_registered_fetcher(history: list[Attempt], expected: object) -> None:
    """R-5.1: a fetcher runs, waits on its schedule, is done, or is used up (D65)."""
    assert decide(ORDER, REGISTERED, history, schedule_for) == expected


def test_technical_failure_passes_to_the_next_fetcher() -> None:
    """R-5.1: a fetcher's final failure moves the chain to the next fetcher in order."""
    history = [Attempt(FETCHER, FetchReason.TECHNICAL)]
    assert decide(ORDER, {FETCHER, "email_inbox"}, history, schedule_for) == Run("email_inbox")


def test_empty_order_is_exhausted() -> None:
    """D65: an empty order leaves no fetcher to run."""
    assert decide([], REGISTERED, [], schedule_for) == Exhausted()


def test_retry_delay_first_not_found_is_the_first_wait() -> None:
    """D65: the first not-found-yet waits ``schedule[0]``."""
    assert retry_delay(FETCHER, [], schedule_for(FETCHER)) == timedelta(hours=1)


def test_retry_delay_after_one_not_found_is_the_second_wait() -> None:
    """D65: after one not-found-yet the next wait is ``schedule[1]``."""
    history = attempts(FetchReason.NOT_FOUND_YET, 1)
    assert retry_delay(FETCHER, history, schedule_for(FETCHER)) == timedelta(hours=6)


def test_retry_delay_used_up_is_none() -> None:
    """D65: a used-up schedule has no delay."""
    history = attempts(FetchReason.NOT_FOUND_YET, 2)
    assert retry_delay(FETCHER, history, schedule_for(FETCHER)) is None


def test_fetch_result_ok_only_for_ok_reason() -> None:
    """R-5.1: a fetch result is ok only when its reason is ok."""
    assert FetchResult(FetchReason.OK, (), None).ok is True
    assert FetchResult(FetchReason.NOT_FOUND_YET, (), None).ok is False
