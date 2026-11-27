"""The fetcher chain decision: which fetcher runs next, and when (D65).

Pure: there is no clock and no I/O. ``decide`` reads the attempt history and
says whether to run a fetcher, wait after a wait reason, stop, or move on;
the queue's ``run_after`` holds the actual time.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import timedelta

from invoicebot.fetchers.base import TECHNICAL_REASONS, FetchReason

#: Reasons after which a fetcher waits for its next scheduled retry instead of ending (D65, D100).
#: A technical reason is not here: it gets one retry of its own (D167).
WAIT_REASONS = frozenset({FetchReason.NOT_FOUND_YET, FetchReason.AUTH_REQUIRED})


@dataclass(frozen=True)
class Attempt:
    """One recorded fetch attempt and its reason code."""

    fetcher: str
    reason: FetchReason


@dataclass(frozen=True)
class Run:
    """Run this fetcher now."""

    fetcher: str


@dataclass(frozen=True)
class Wait:
    """Run this fetcher again: its latest try waits or was a first technical failure (D65, D100, D167)."""

    fetcher: str


@dataclass(frozen=True)
class Done:
    """This fetcher already succeeded; the chain stops (R-5.1)."""

    fetcher: str


@dataclass(frozen=True)
class Exhausted:
    """No fetcher is left to try (R-5.1)."""


type Decision = Run | Wait | Done | Exhausted


CAPTCHA_REASONS = frozenset({FetchReason.CAPTCHA_ASKED, FetchReason.CAPTCHA_WRONG})


def decide(
    order: Sequence[str],
    registered: Collection[str],
    attempts: Sequence[Attempt],
    schedule_for: Callable[[str], Sequence[timedelta]],
    *,
    no_retry: Collection[str],
    captcha_tries: int,
) -> Decision:
    """Choose the next step of the chain (R-5.1, D65, D170).

    Walks ``order`` keeping only registered fetchers. A fetcher with no attempt
    runs; one whose latest attempt is ``ok`` is done; one whose latest attempt
    is in ``WAIT_REASONS`` waits on its schedule until it is used up; one whose
    latest attempt is technical waits once on its first schedule entry (D167),
    unless it is in ``no_retry`` (an open broken gap, D170); any other reason is
    final and the next fetcher gets its turn. ``no_retry`` is required so no
    caller loses D170's brake by omission. A ``captcha_asked`` latest attempt
    is the resume and runs the same fetcher; a ``captcha_wrong`` one runs it
    again while its asks number fewer than ``captcha_tries`` (D209), else the
    fetcher is over; ``captcha_timeout`` is final.
    """
    for fetcher in order:
        if fetcher not in registered:
            continue
        decision = _decide_one(fetcher, attempts, schedule_for, no_retry, captcha_tries)
        if decision is not None:
            return decision
    return Exhausted()


def _decide_one(
    fetcher: str,
    attempts: Sequence[Attempt],
    schedule_for: Callable[[str], Sequence[timedelta]],
    no_retry: Collection[str],
    captcha_tries: int,
) -> Run | Wait | Done | None:
    """Decide for one fetcher; ``None`` means it is used up, try the next."""
    history = [attempt for attempt in attempts if attempt.fetcher == fetcher]
    if not history:
        return Run(fetcher)
    latest = history[-1].reason
    if latest is FetchReason.OK:
        return Done(fetcher)
    if latest in CAPTCHA_REASONS:
        return _captcha_or_none(fetcher, history, captcha_tries)
    if latest in WAIT_REASONS:
        return _wait_or_none(fetcher, history, schedule_for)
    if latest in TECHNICAL_REASONS:
        return _technical_or_none(fetcher, history, schedule_for, no_retry)
    return None


def _captcha_or_none(fetcher: str, history: Sequence[Attempt], captcha_tries: int) -> Run | None:
    """Run again after an ask (the resume) or a wrong answer with tries left; else ``None`` (D209)."""
    if history[-1].reason is FetchReason.CAPTCHA_ASKED:
        return Run(fetcher)
    asks = sum(1 for attempt in history if attempt.reason is FetchReason.CAPTCHA_ASKED)
    return Run(fetcher) if asks < captcha_tries else None


def _technical_or_none(
    fetcher: str,
    history: Sequence[Attempt],
    schedule_for: Callable[[str], Sequence[timedelta]],
    no_retry: Collection[str],
) -> Wait | None:
    """Wait once after a technical failure, or ``None`` when no retry remains (D167, D170)."""
    if technical_retry_delay(fetcher, history[:-1], schedule_for(fetcher), no_retry=no_retry) is None:
        return None
    return Wait(fetcher)


def _wait_or_none(
    fetcher: str,
    history: Sequence[Attempt],
    schedule_for: Callable[[str], Sequence[timedelta]],
) -> Wait | None:
    """Wait after the latest wait reason, or ``None`` if ``retry_delay`` queued no retry (D65, D100).

    ``retry_delay`` is the one rule: it is asked about the history without the
    run that produced the latest wait reason.
    """
    if retry_delay(fetcher, history[:-1], schedule_for(fetcher)) is None:
        return None
    return Wait(fetcher)


def retry_delay(
    fetcher: str,
    attempts: Sequence[Attempt],
    schedule: Sequence[timedelta],
) -> timedelta | None:
    """Return the wait after this wait reason, or ``None`` when retries are used up (D65, D100).

    ``n`` counts this fetcher's attempts whose reason is in ``WAIT_REASONS`` so far,
    so the first one waits ``schedule[0]``.
    """
    retries = sum(1 for attempt in attempts if attempt.fetcher == fetcher and attempt.reason in WAIT_REASONS)
    return schedule[retries] if retries < len(schedule) else None


def technical_retry_delay(
    fetcher: str,
    attempts: Sequence[Attempt],
    schedule: Sequence[timedelta],
    *,
    no_retry: Collection[str],
) -> timedelta | None:
    """Return the wait after this technical failure, or ``None`` when its one retry is spent (D167, D170).

    ``attempts`` excludes the run being judged: an earlier technical or timeout
    attempt for the same fetcher uses up the retry. Any other earlier reason,
    a wait reason included, leaves it, so the wait is ``schedule[0]``; an empty
    schedule has no retry. A fetcher in ``no_retry`` gets no retry at all: its
    portal has an open broken gap (D170). ``no_retry`` is required so no caller
    loses D170's brake by omission.
    """
    if fetcher in no_retry:
        return None
    if not schedule:
        return None
    if any(attempt.fetcher == fetcher and attempt.reason in TECHNICAL_REASONS for attempt in attempts):
        return None
    return schedule[0]
