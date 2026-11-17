"""Tests for the Google consent URL, PKCE pair, redirect parse and scopes (R-6.5, D128, D129)."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from invoicebot.auth.google_consent import (
    AUTHORIZATION_URL,
    ConsentError,
    consent_url,
    parse_redirect,
    pkce_pair,
)
from invoicebot.auth.providers import (
    DRIVE_FILE_SCOPE,
    EMAIL_SCOPE,
    GMAIL_READ_SCOPE,
    OPENID_SCOPE,
    google_accounts,
    google_scopes_for,
)
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "example.yaml"

#: Synthetic values only; nothing here is a real state, code or redirect.
STATE = "synthetic-state"
REDIRECT_URI = "http://127.0.0.1:8080"


@pytest.fixture(scope="module")
def example_config() -> Config:
    """Load the shipped example config the way the app does (R-9.6)."""
    return load_config(EXAMPLE)


def test_example_config_owner_signs_in_with_drive_file_only(example_config: Config) -> None:
    """R-6.5: a Google sink's account gets drive.file, not gmail.readonly."""
    assert google_scopes_for(example_config, "owner") == [OPENID_SCOPE, EMAIL_SCOPE, DRIVE_FILE_SCOPE]


def test_example_config_invoices_a_signs_in_with_gmail_read_only(example_config: Config) -> None:
    """R-6.5: a gmail inbox's account gets gmail.readonly, not drive.file."""
    assert google_scopes_for(example_config, "invoices_a") == [
        OPENID_SCOPE,
        EMAIL_SCOPE,
        GMAIL_READ_SCOPE,
    ]


def test_consent_url_asks_for_openid_and_email(example_config: Config) -> None:
    """R-6.5, D138: the sign-in asks for openid and email besides the account's data scopes."""
    _, challenge = pkce_pair()
    scopes = google_scopes_for(example_config, "owner")

    url = consent_url("client-id", REDIRECT_URI, scopes, STATE, challenge)

    requested = parse_qs(urlsplit(url).query)["scope"][0].split()
    assert requested == [OPENID_SCOPE, EMAIL_SCOPE, DRIVE_FILE_SCOPE]


def test_google_accounts_lists_every_named_account(example_config: Config) -> None:
    """R-6.5, D129: the sign-in command offers the sink and gmail inbox accounts."""
    assert google_accounts(example_config) == {"owner", "invoices_a"}


def test_unknown_google_account_is_refused(example_config: Config) -> None:
    """R-6.5, D129: an account no sink or gmail inbox names has no scopes."""
    with pytest.raises(ValueError, match="unknown_account"):
        google_scopes_for(example_config, "unknown_account")


def test_consent_url_carries_pkce_and_offline_access() -> None:
    """R-6.5: the URL asks for offline access with prompt=consent and an S256 challenge."""
    verifier, challenge = pkce_pair()

    url = consent_url("client-id", REDIRECT_URI, [DRIVE_FILE_SCOPE, GMAIL_READ_SCOPE], STATE, challenge)

    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == AUTHORIZATION_URL
    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["response_type"] == ["code"]
    assert query["scope"] == [f"{DRIVE_FILE_SCOPE} {GMAIL_READ_SCOPE}"]
    assert query["state"] == [STATE]
    assert query["code_challenge"] == [challenge]
    assert query["code_challenge_method"] == ["S256"]
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert verifier


def test_pkce_challenge_is_the_base64url_sha256_of_the_verifier() -> None:
    """R-6.5, D128: the challenge is the unpadded base64url SHA-256 of the verifier."""
    verifier, challenge = pkce_pair()

    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    assert "=" not in challenge


def test_parse_redirect_returns_the_code_on_a_matching_state() -> None:
    """R-6.5: a redirect with the expected state and a code yields the code."""
    assert parse_redirect(f"/?state={STATE}&code=synthetic-code", STATE) == "synthetic-code"


def test_parse_redirect_refuses_a_state_mismatch() -> None:
    """R-6.5: a redirect whose state differs from this sign-in is refused."""
    with pytest.raises(ConsentError, match="state"):
        parse_redirect("/?state=other&code=synthetic-code", STATE)


def test_parse_redirect_refuses_an_error_response() -> None:
    """R-6.5: Google's error parameter (e.g. the owner declined) is refused."""
    with pytest.raises(ConsentError, match="access_denied"):
        parse_redirect(f"/?state={STATE}&error=access_denied", STATE)


def test_parse_redirect_refuses_a_missing_code() -> None:
    """R-6.5: a redirect with the right state but no code is refused."""
    with pytest.raises(ConsentError, match="code"):
        parse_redirect(f"/?state={STATE}", STATE)
