"""Retry policy shared by the job queue and the outbox (R-9.4, R-11.5).

One home for the backoff schedule and for how a failure is stored, so the two
queues cannot drift apart.
"""

MAX_BACKOFF_MINUTES = 60

#: ``last_error`` only needs to identify the failure; the traceback is in the log.
MAX_ERROR_CHARS = 500


def backoff_minutes(attempts: int) -> int:
    """Return the retry delay in minutes after the *attempts*-th failure.

    The delay doubles each attempt (2, 4, 8, 16, 32 minutes) up to a cap of
    ``MAX_BACKOFF_MINUTES``.
    """
    return min(1 << attempts, MAX_BACKOFF_MINUTES)


def describe_error(exc: BaseException) -> str:
    """Render ``exc`` as ``Type: message``, capped for a ``last_error`` column."""
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
