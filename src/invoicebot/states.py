"""The invoice state names and the sets the conversation uses (spec 03 §4).

A state is a string in ``invoices.state``; this module gives each one a name and groups
them by what the conversation may do with an invoice in it: terminal, waiting for the
user's answer, needing a rule, cancellable and editable. ``set_state`` enforces the
``NEEDS_RULE`` and ``StateMoved`` contracts.
"""

from typing import Final

RECEIVED: Final = "RECEIVED"
EXTRACTING: Final = "EXTRACTING"
EXTRACT_FAILED: Final = "EXTRACT_FAILED"
ROUTING: Final = "ROUTING"
CONFIRMING: Final = "CONFIRMING"
VALIDATING: Final = "VALIDATING"
CONFIRM_INVALID: Final = "CONFIRM_INVALID"
FETCHING_DOCS: Final = "FETCHING_DOCS"
WAITING_CAPTCHA: Final = "WAITING_CAPTCHA"
MANUAL_REQUIRED: Final = "MANUAL_REQUIRED"
PENDING_USER: Final = "PENDING_USER"
PENDING_AUTH: Final = "PENDING_AUTH"
DELIVERING: Final = "DELIVERING"
BATCHED: Final = "BATCHED"
DONE: Final = "DONE"
CANCELLED: Final = "CANCELLED"
FAILED: Final = "FAILED"

#: Every invoice state (spec 03 §4).
ALL_STATES: Final[frozenset[str]] = frozenset(
    {
        RECEIVED,
        EXTRACTING,
        EXTRACT_FAILED,
        ROUTING,
        CONFIRMING,
        VALIDATING,
        CONFIRM_INVALID,
        FETCHING_DOCS,
        WAITING_CAPTCHA,
        MANUAL_REQUIRED,
        PENDING_USER,
        PENDING_AUTH,
        DELIVERING,
        BATCHED,
        DONE,
        CANCELLED,
        FAILED,
    }
)
#: No further job moves an invoice out of these states.
TERMINAL: Final[frozenset[str]] = frozenset({DONE, CANCELLED, FAILED})
#: Terminal states a dead job leaves as they are: the invoice is filed or voided (BR-S03, D190).
SETTLED: Final[frozenset[str]] = frozenset({DONE, CANCELLED})

#: The states where the bot waits for the user's next message (D94).
WAITING_FOR_USER: Final[frozenset[str]] = frozenset({CONFIRMING, EXTRACT_FAILED, CONFIRM_INVALID})

#: The states a transition may only enter with the business rule that put it there (S5.2).
NEEDS_RULE: Final[frozenset[str]] = frozenset(
    {
        CANCELLED,
        FAILED,
        CONFIRMING,
        EXTRACT_FAILED,
        CONFIRM_INVALID,
        PENDING_USER,
        MANUAL_REQUIRED,
        PENDING_AUTH,
        WAITING_CAPTCHA,
    }
)

#: The states ``/cancelar`` may leave: every non-terminal state before DELIVERING (D96).
CANCELLABLE: Final[frozenset[str]] = frozenset(
    {
        RECEIVED,
        EXTRACTING,
        EXTRACT_FAILED,
        ROUTING,
        CONFIRMING,
        VALIDATING,
        CONFIRM_INVALID,
        FETCHING_DOCS,
        WAITING_CAPTCHA,
        MANUAL_REQUIRED,
        PENDING_USER,
        PENDING_AUTH,
    }
)

#: The states a ``campo=valor`` edit is accepted in (D97).
EDITABLE: Final[frozenset[str]] = frozenset(
    {
        EXTRACT_FAILED,
        ROUTING,
        CONFIRMING,
        CONFIRM_INVALID,
        FETCHING_DOCS,
        MANUAL_REQUIRED,
        PENDING_USER,
    }
)


class StateMoved(Exception):
    """An ``expected`` transition found the invoice in another state (D95).

    The UPDATE matched no row, so the caller's write lost to an earlier one; the
    handler's transaction must roll back and the job completes without retrying.
    """

    def __init__(self, invoice_id: int, expected: str, actual: str | None) -> None:
        super().__init__(f"invoice {invoice_id} moved on: expected {expected}, found {actual}")
        self.invoice_id = invoice_id
        self.expected = expected
        self.actual = actual
