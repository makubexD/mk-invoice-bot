"""Shared pieces of the Google REST clients (R-6.4, R-9.16).

The Google clients decode their answers and map failures the same way: a
refusal that no retry fixes is a :class:`GoogleError`, a 429, a 5xx or a
network failure is a :class:`GoogleTransient` the queue retries, and a 401 means
the account needs a sign-in. Errors carry the HTTP status only, never a URL, a
path or a body (R-9.16).
"""

from __future__ import annotations

import httpx

from invoicebot.auth.msal_cache import AuthRequired


class GoogleError(Exception):
    """A Google request failed and no retry fixes it (R-6.4)."""

    def __init__(self, status: int | None = None) -> None:
        self.status = status
        reason = "on the network" if status is None else f"with status {status}"
        super().__init__(f"Google request failed {reason}")


class GoogleTransient(GoogleError):
    """A Google failure the queue retries (429, 5xx, or the network with no status; R-6.4)."""


def raise_google_error(response: httpx.Response, account: str) -> None:
    """Raise the exception a failed Google response maps to (R-6.4).

    A 2xx returns; a 401 means the account needs a sign-in; 429 and 5xx are
    worth retrying; anything else is final. Only the status reaches the message
    (R-9.16).
    """
    status = response.status_code
    if 200 <= status < 300:
        return
    if status == 401:
        raise AuthRequired(account)
    if status == 429 or status >= 500:
        raise GoogleTransient(status)
    raise GoogleError(status)


def auth_headers(token: str) -> dict[str, str]:
    """Return the bearer header for one Google call (R-6.4)."""
    return {"Authorization": f"Bearer {token}"}
