"""Shared pieces of the Google REST clients (R-6.4, R-9.16).

The Google clients decode their answers and map failures the same way: a
refusal that no retry fixes is a :class:`GoogleError`; a 429, a 5xx or a 403
whose body names a rate limit is a :class:`GoogleTransient` the queue retries;
a network failure propagates as an :class:`httpx` error, which the queue also
retries; and a 401 means the account needs a sign-in. Errors carry the HTTP
status only, never a URL, a path or a body (R-9.16).
"""

from __future__ import annotations

import httpx

from invoicebot.auth.msal_cache import AuthRequired

#: The ``error.errors[*].reason`` values with which Google reports a rate limit.
RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})


class GoogleError(Exception):
    """A Google request failed and no retry fixes it (R-6.4)."""

    def __init__(self, status: int | None = None) -> None:
        self.status = status
        reason = "with an unusable answer" if status is None else f"with status {status}"
        super().__init__(f"Google request failed {reason}")


class GoogleTransient(GoogleError):
    """A Google failure the queue retries (429, 5xx, or a 403 rate limit; R-6.4)."""


def json_object(response: httpx.Response) -> dict[str, object]:
    """Return the decoded JSON object, or raise when the body is not one (R-6.4)."""
    try:
        body: object = response.json()
    except ValueError as err:
        raise GoogleError() from err
    if not isinstance(body, dict):
        raise GoogleError()
    return body


def raise_google_error(response: httpx.Response, account: str) -> None:
    """Raise the exception a failed Google response maps to (R-6.4).

    A 2xx returns; a 401 means the account needs a sign-in; 429 and 5xx are
    worth retrying, and so is a 403 whose body names a rate limit; anything
    else is final. Only the status reaches the message (R-9.16).
    """
    status = response.status_code
    if 200 <= status < 300:
        return
    if status == 401:
        raise AuthRequired(account)
    if status == 403 and _names_rate_limit(response):
        raise GoogleTransient(status)
    if status == 429 or status >= 500:
        raise GoogleTransient(status)
    raise GoogleError(status)


def _names_rate_limit(response: httpx.Response) -> bool:
    """Return whether a 403 body names a Google rate limit (R-6.4).

    Only an already-read body is inspected, so a streamed response stays a
    plain :class:`GoogleError`; a body that is not JSON in the documented error
    shape is not a rate limit either.
    """
    if not response.is_stream_consumed:
        return False
    try:
        body: object = response.json()
    except (ValueError, httpx.ResponseNotRead):
        return False
    return _rate_limit_error(body)


def _rate_limit_error(body: object) -> bool:
    """Return whether ``error.errors[*].reason`` names a rate limit (R-6.4)."""
    if not isinstance(body, dict):
        return False
    error = body.get("error")
    if not isinstance(error, dict):
        return False
    errors = error.get("errors")
    if not isinstance(errors, list):
        return False
    return any(isinstance(item, dict) and item.get("reason") in RATE_LIMIT_REASONS for item in errors)


def auth_headers(token: str) -> dict[str, str]:
    """Return the bearer header for one Google call (R-6.4)."""
    return {"Authorization": f"Bearer {token}"}
