"""The fetcher chain decision: which fetcher runs next, and when (D65).

Pure: there is no clock and no I/O. ``decide`` reads the attempt history and
says whether to run a fetcher, wait after a ``not_found_yet``, stop, or move on;
the queue's ``run_after`` holds the actual time.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import timedelta

from invoicebot.fetchers.base import FetchReason


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
    """Run this fetcher again: its latest try was not found yet and its scheduled retry is now due (D65)."""

    fetcher: str


@dataclass(frozen=True)
class Done:
    """This fetcher already succeeded; the chain stops (R-5.1)."""

    fetcher: str


@dataclass(frozen=True)
class Exhausted:
    """No fetcher is left to try (R-5.1)."""


type Decision = Run | Wait | Done | Exhausted


def decide(
    order: Sequence[str],
    registered: Collection[str],
    attempts: Sequence[Attempt],
    schedule_for: Callable[[str], Sequence[timedelta]],
) -> Decision:
    """Choose the next step of the chain (R-5.1, D65).

    Walks ``order`` keeping only registered fetchers. A fetcher with no attempt
    runs; one whose latest attempt is ``ok`` is done; one whose latest attempt
    is ``not_found_yet`` waits on its schedule until it is used up; any other
    reason is final and the next fetcher gets its turn.
    """
    for fetcher in order:
        if fetcher not in registered:
            continue
        decision = _decide_one(fetcher, attempts, schedule_for)
        if decision is not None:
            return decision
    return Exhausted()


def _decide_one(
    fetcher: str,
    attempts: Sequence[Attempt],
    schedule_for: Callable[[str], Sequence[timedelta]],
) -> Run | Wait | Done | None:
    """Decide for one fetcher; ``None`` means it is used up, try the next."""
    history = [attempt for attempt in attempts if attempt.fetcher == fetcher]
    if not history:
        return Run(fetcher)
    latest = history[-1].reason
    if latest is FetchReason.OK:
        return Done(fetcher)
    if latest is FetchReason.NOT_FOUND_YET:
        return _wait_or_none(fetcher, history, schedule_for)
    return None


def _wait_or_none(
    fetcher: str,
    history: Sequence[Attempt],
    schedule_for: Callable[[str], Sequence[timedelta]],
) -> Wait | None:
    """Wait after the latest ``not_found_yet``, or ``None`` if ``retry_delay`` queued no retry (D65).

    ``retry_delay`` is the one rule: it is asked about the history without the
    run that produced the latest ``not_found_yet``.
    """
    if retry_delay(fetcher, history[:-1], schedule_for(fetcher)) is None:
        return None
    return Wait(fetcher)


def retry_delay(
    fetcher: str,
    attempts: Sequence[Attempt],
    schedule: Sequence[timedelta],
) -> timedelta | None:
    """Return the wait after this ``not_found_yet``, or ``None`` when retries are used up (D65).

    ``n`` counts this fetcher's ``not_found_yet`` attempts so far, so the first one
    waits ``schedule[0]``.
    """
    retries = sum(
        1
        for attempt in attempts
        if attempt.fetcher == fetcher and attempt.reason is FetchReason.NOT_FOUND_YET
    )
    return schedule[retries] if retries < len(schedule) else None
