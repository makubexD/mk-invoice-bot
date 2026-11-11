"""Job queue: durable jobs with leases, backoff and a crash-loop guard (S1.4)."""

from invoicebot.queue.jobs import (
    MAX_ATTEMPTS,
    MAX_CRASHES,
    Job,
    backoff_minutes,
    claim,
    complete,
    enqueue,
    fail,
    is_dead,
)

__all__ = [
    "MAX_ATTEMPTS",
    "MAX_CRASHES",
    "Job",
    "backoff_minutes",
    "claim",
    "complete",
    "enqueue",
    "fail",
    "is_dead",
]
