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
    technical_retry_delay,
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
        pytest.param(attempts(FetchReason.NOT_FOUND_YET, 1), Wait(FETCHER), id="wait-after-first"),
        pytest.param(attempts(FetchReason.NOT_FOUND_YET, 2), Wait(FETCHER), id="wait-after-second"),
        pytest.param(attempts(FetchReason.NOT_FOUND_YET, 3), Exhausted(), id="schedule-used-up"),
        pytest.param(attempts(FetchReason.TECHNICAL, 1), Wait(FETCHER), id="first-technical-waits"),
        pytest.param(attempts(FetchReason.TIMEOUT, 1), Wait(FETCHER), id="first-timeout-waits"),
        pytest.param(attempts(FetchReason.TECHNICAL, 2), Exhausted(), id="second-technical-is-final"),
        pytest.param(
            [Attempt(FETCHER, FetchReason.TECHNICAL), Attempt(FETCHER, FetchReason.TIMEOUT)],
            Exhausted(),
            id="timeout-after-technical-is-final",
        ),
        pytest.param(
            [Attempt(FETCHER, FetchReason.NOT_FOUND_YET), Attempt(FETCHER, FetchReason.TECHNICAL)],
            Wait(FETCHER),
            id="technical-after-a-wait-reason-waits",
        ),
        pytest.param(attempts(FetchReason.XML_MISMATCH, 1), Exhausted(), id="xml-mismatch-is-final"),
    ],
)
def test_decide_one_registered_fetcher(history: list[Attempt], expected: object) -> None:
    """R-5.1: a fetcher runs, waits on its schedule, is done, or is used up (D65, D167)."""
    assert decide(ORDER, REGISTERED, history, schedule_for) == expected


def test_technical_failure_passes_to_the_next_fetcher() -> None:
    """D167: a fetcher's second technical failure is final and the chain moves to the next fetcher."""
    history = attempts(FetchReason.TECHNICAL, 2)
    assert decide(ORDER, {FETCHER, "email_inbox"}, history, schedule_for) == Run("email_inbox")


def test_technical_failure_without_a_schedule_moves_on() -> None:
    """D167: a technical failure with an empty schedule has no retry; the chain moves on."""

    def no_schedule(fetcher: str) -> list[timedelta]:
        del fetcher
        return []

    history = attempts(FetchReason.TECHNICAL, 1)
    assert decide(ORDER, {FETCHER, "email_inbox"}, history, no_schedule) == Run("email_inbox")
    assert decide(ORDER, REGISTERED, history, no_schedule) == Exhausted()


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


@pytest.mark.parametrize("not_found_attempts", range(5))
def test_decide_waits_exactly_when_retry_delay_has_a_delay(not_found_attempts: int) -> None:
    """D65: ``decide`` waits after the next not-found-yet exactly when ``retry_delay`` queued a retry."""
    history = attempts(FetchReason.NOT_FOUND_YET, not_found_attempts)
    has_delay = retry_delay(FETCHER, history, schedule_for(FETCHER)) is not None
    waits = decide(ORDER, REGISTERED, [*history, Attempt(FETCHER, FetchReason.NOT_FOUND_YET)], schedule_for)
    assert (waits == Wait(FETCHER)) is has_delay


def test_fetch_result_ok_only_for_ok_reason() -> None:
    """R-5.1: a fetch result is ok only when its reason is ok."""
    assert FetchResult(FetchReason.OK, (), None).ok is True
    assert FetchResult(FetchReason.NOT_FOUND_YET, (), None).ok is False


def test_auth_required_waits_on_the_first_schedule_entry() -> None:
    """D65, D100: an auth-required attempt waits for the first scheduled retry."""
    history = attempts(FetchReason.AUTH_REQUIRED, 1)
    assert decide(ORDER, REGISTERED, history, schedule_for) == Wait(FETCHER)
    assert retry_delay(FETCHER, [], schedule_for(FETCHER)) == timedelta(hours=1)


def test_auth_required_then_not_found_yet_waits_on_the_second_schedule_entry() -> None:
    """D65, D100: auth-required and not-found-yet attempts share the schedule in order."""
    history = [Attempt(FETCHER, FetchReason.AUTH_REQUIRED), Attempt(FETCHER, FetchReason.NOT_FOUND_YET)]
    assert decide(ORDER, REGISTERED, history, schedule_for) == Wait(FETCHER)
    assert retry_delay(FETCHER, history[:-1], schedule_for(FETCHER)) == timedelta(hours=6)


def test_wait_reasons_exhaust_the_schedule_and_move_on() -> None:
    """D65, D100: once wait-reason attempts fill the schedule the chain moves to the next fetcher."""
    history = [
        Attempt(FETCHER, FetchReason.AUTH_REQUIRED),
        Attempt(FETCHER, FetchReason.NOT_FOUND_YET),
        Attempt(FETCHER, FetchReason.AUTH_REQUIRED),
    ]
    assert decide(ORDER, {FETCHER, "email_inbox"}, history, schedule_for) == Run("email_inbox")


@pytest.mark.parametrize(
    ("history", "schedule", "expected"),
    [
        pytest.param([], schedule_for(FETCHER), timedelta(hours=1), id="no-earlier-attempt"),
        pytest.param(
            attempts(FetchReason.TIMEOUT, 1),
            schedule_for(FETCHER),
            None,
            id="earlier-timeout-spends-it",
        ),
        pytest.param(
            attempts(FetchReason.NOT_FOUND_YET, 1),
            schedule_for(FETCHER),
            timedelta(hours=1),
            id="earlier-not-found-yet-leaves-it",
        ),
        pytest.param([], [], None, id="empty-schedule"),
    ],
)
def test_technical_retry_delay(
    history: list[Attempt],
    schedule: list[timedelta],
    expected: timedelta | None,
) -> None:
    """D167: a technical failure's one retry waits the fetcher's first scheduled delay."""
    assert technical_retry_delay(FETCHER, history, schedule) == expected
