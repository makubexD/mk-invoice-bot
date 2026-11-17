"""Tests for the shared Google REST pieces (R-6.4, R-9.16)."""

from __future__ import annotations

import httpx
import pytest

from invoicebot.auth.msal_cache import AuthRequired
from invoicebot.sinks.google_api import GoogleError, GoogleTransient, raise_google_error

ACCOUNT = "owner"
BODY = "synthetic-error-body-must-not-leak"


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, text=BODY)


def test_401_raises_auth_required_with_the_account() -> None:
    """R-6.4: a 401 means the account needs an interactive sign-in."""
    with pytest.raises(AuthRequired) as caught:
        raise_google_error(_response(401), ACCOUNT)
    assert caught.value.account == ACCOUNT


@pytest.mark.parametrize("status", [429, 503])
def test_transient_statuses_raise_google_transient(status: int) -> None:
    """R-6.4: a 429 or a 5xx is worth retrying."""
    with pytest.raises(GoogleTransient) as caught:
        raise_google_error(_response(status), ACCOUNT)
    assert caught.value.status == status


def test_403_raises_google_error_with_the_status_and_not_the_body() -> None:
    """R-9.16: a refusal carries the status only, never the response body."""
    with pytest.raises(GoogleError) as caught:
        raise_google_error(_response(403), ACCOUNT)
    assert not isinstance(caught.value, GoogleTransient)
    assert str(caught.value.status) in str(caught.value)
    assert BODY not in str(caught.value)


def test_200_returns_without_raising() -> None:
    """R-6.4: a successful response is not an error."""
    raise_google_error(_response(200), ACCOUNT)
