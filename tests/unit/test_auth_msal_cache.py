"""Tests for the encrypted per-account MSAL token cache (R-7.9, BR-D01, R-9.9)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pyrage
import pytest

from invoicebot.auth.msal_cache import AuthRequired, TokenProvider

#: The config's account name; MSAL knows the account by its mailbox, which differs (R-7.9).
ACCOUNT = "personal"
MAILBOX = "owner@example.test"
CLIENT_ID = "synthetic-client-id"
#: MSAL adds offline_access, openid and profile itself and rejects them as input.
SCOPES = ("Files.ReadWrite", "Mail.Send")
SIGNED_IN_ACCESS = "synthetic-access-token"
REFRESHED_ACCESS = "synthetic-refreshed-access-token"
DEVICE_MESSAGE = "Use code ABCD-1234 at https://microsoft.com/devicelogin"
IDP_BASE = "https://login.microsoftonline.com/consumers"
DEVICE_CODE_ENDPOINT = f"{IDP_BASE}/oauth2/v2.0/devicecode"
OPENID_CONFIGURATION = f"{IDP_BASE}/v2.0/.well-known/openid-configuration"


class FakeResponse:
    """The minimal response shape MSAL reads: status, text and headers."""

    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self.text = json.dumps(payload)
        self.headers = {"content-type": "application/json"}

    def json(self) -> dict[str, Any]:
        return json.loads(self.text)


class FakeHttpClient:
    """Canned identity-provider answers by URL; records every call, never dials out."""

    def __init__(self, account: str, *, access_token_lifetime_s: int = 3600) -> None:
        self._account = account
        self.access_token_lifetime_s = access_token_lifetime_s
        self.invalid_grant = False
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("GET", url, kwargs))
        if url == OPENID_CONFIGURATION:
            return FakeResponse(
                200,
                {
                    "authorization_endpoint": f"{IDP_BASE}/oauth2/v2.0/authorize",
                    "token_endpoint": f"{IDP_BASE}/oauth2/v2.0/token",
                    "device_authorization_endpoint": DEVICE_CODE_ENDPOINT,
                },
            )
        if "/common/discovery/instance" in url:
            return FakeResponse(
                200,
                {
                    "tenant_discovery_endpoint": OPENID_CONFIGURATION,
                    "metadata": [
                        {
                            "preferred_network": "login.microsoftonline.com",
                            "preferred_cache": "login.microsoftonline.com",
                            "aliases": ["login.microsoftonline.com"],
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("POST", url, kwargs))
        if url == DEVICE_CODE_ENDPOINT:
            return FakeResponse(
                200,
                {
                    "user_code": "ABCD-1234",
                    "device_code": "synthetic-device-code",
                    "verification_uri": "https://microsoft.com/devicelogin",
                    "expires_in": 900,
                    "interval": 1,
                    "message": DEVICE_MESSAGE,
                },
            )
        if url == f"{IDP_BASE}/oauth2/v2.0/token":
            if self.invalid_grant:
                return FakeResponse(400, {"error": "invalid_grant", "error_description": "synthetic"})
            data: dict[str, Any] = kwargs.get("data") or {}
            return FakeResponse(200, self._tokens(data))
        raise AssertionError(f"unexpected POST {url}")

    def token_posts(self) -> list[dict[str, Any]]:
        """Return the token-endpoint request bodies, in order."""
        return [
            kwargs["data"]
            for method, url, kwargs in self.calls
            if method == "POST" and url == f"{IDP_BASE}/oauth2/v2.0/token"
        ]

    def _tokens(self, data: dict[str, Any]) -> dict[str, Any]:
        is_refresh = data.get("grant_type") == "refresh_token"
        return {
            "token_type": "Bearer",
            "scope": " ".join(SCOPES),
            "expires_in": 3600 if is_refresh else self.access_token_lifetime_s,
            "access_token": REFRESHED_ACCESS if is_refresh else SIGNED_IN_ACCESS,
            "refresh_token": ("synthetic-refresh-token-2" if is_refresh else "synthetic-refresh-token-1"),
            "id_token": _id_token(MAILBOX),
        }


def _b64url(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _id_token(mailbox: str) -> str:
    """Build an unsigned JWT whose claims give MSAL the mailbox as its username."""
    header = _b64url({"alg": "none", "typ": "JWT"})
    claims = _b64url({"sub": "synthetic-subject", "preferred_username": mailbox})
    return f"{header}.{claims}."


def _provider(tmp_path: Path, fake: FakeHttpClient) -> tuple[TokenProvider, pyrage.x25519.Identity, Path]:
    identity = pyrage.x25519.Identity.generate()
    cache_path = tmp_path / "owner.msal.age"
    provider = TokenProvider(CLIENT_ID, ACCOUNT, SCOPES, cache_path, identity, fake)
    return provider, identity, cache_path


def _login(provider: TokenProvider) -> None:
    shown: list[str] = []
    provider.login_device_code(shown.append)
    assert shown == [DEVICE_MESSAGE]


async def test_missing_cache_requires_sign_in(tmp_path: Path) -> None:
    """BR-D01: without a cache file the account is unknown, so a sign-in is needed."""
    fake = FakeHttpClient(ACCOUNT)
    provider, _, _ = _provider(tmp_path, fake)

    with pytest.raises(AuthRequired) as raised:
        await provider.token()

    assert raised.value.account == ACCOUNT
    assert str(raised.value) == f"Microsoft sign-in needed for {ACCOUNT}"
    assert fake.token_posts() == []


async def test_seeded_cache_returns_the_token_without_a_token_request(tmp_path: Path) -> None:
    """R-7.9: a cache seeded by a device-code login yields the token with no token POST."""
    fake = FakeHttpClient(ACCOUNT)
    provider, _, _ = _provider(tmp_path, fake)
    _login(provider)
    fake.calls.clear()

    token = await provider.token()

    assert token == SIGNED_IN_ACCESS
    assert fake.token_posts() == []


async def test_offline_access_scope_is_accepted(tmp_path: Path) -> None:
    """R-7.9: the spec's scope list names offline_access, which MSAL adds itself and refuses."""
    fake = FakeHttpClient(ACCOUNT)
    identity = pyrage.x25519.Identity.generate()
    scopes = ("offline_access", *SCOPES)
    provider = TokenProvider(CLIENT_ID, ACCOUNT, scopes, tmp_path / "owner.msal.age", identity, fake)
    _login(provider)

    assert await provider.token() == SIGNED_IN_ACCESS


async def test_expired_access_token_is_refreshed_and_the_cache_rewritten(tmp_path: Path) -> None:
    """R-7.9: an expired access token is refreshed and the new state is saved encrypted."""
    fake = FakeHttpClient(ACCOUNT, access_token_lifetime_s=100)
    provider, identity, cache_path = _provider(tmp_path, fake)
    _login(provider)
    fake.calls.clear()

    token = await provider.token()

    assert token == REFRESHED_ACCESS
    assert [post["grant_type"] for post in fake.token_posts()] == ["refresh_token"]
    saved = json.loads(pyrage.decrypt(cache_path.read_bytes(), [identity]).decode("utf-8"))
    assert REFRESHED_ACCESS in [entry["secret"] for entry in saved["AccessToken"].values()]


async def test_cache_file_is_encrypted_and_private(tmp_path: Path) -> None:
    """R-9.9: the cache on disk is encrypted, not JSON, and readable only by its owner."""
    fake = FakeHttpClient(ACCOUNT)
    provider, identity, cache_path = _provider(tmp_path, fake)
    _login(provider)

    raw = cache_path.read_bytes()
    with pytest.raises((json.JSONDecodeError, UnicodeDecodeError)):
        json.loads(raw.decode("utf-8"))
    assert cache_path.stat().st_mode & 0o777 == 0o600
    assert SIGNED_IN_ACCESS in pyrage.decrypt(raw, [identity]).decode("utf-8")


async def test_sign_in_creates_the_missing_cache_directory(tmp_path: Path) -> None:
    """R-7.9, BR-D01: a fresh host has no tokens directory; the sign-in creates it privately."""
    fake = FakeHttpClient(ACCOUNT)
    identity = pyrage.x25519.Identity.generate()
    token_dir = tmp_path / "tokens"
    provider = TokenProvider(CLIENT_ID, ACCOUNT, SCOPES, token_dir / "x.age", identity, fake)

    _login(provider)

    assert (token_dir / "x.age").is_file()
    assert token_dir.stat().st_mode & 0o777 == 0o700


async def test_unreadable_cache_requires_sign_in(tmp_path: Path) -> None:
    """BR-D01: garbage in the cache raises AuthRequired instead of a raw decrypt error."""
    fake = FakeHttpClient(ACCOUNT)
    provider, _, cache_path = _provider(tmp_path, fake)
    cache_path.write_bytes(b"not an age file")

    with pytest.raises(AuthRequired) as raised:
        await provider.token()

    assert raised.value.account == ACCOUNT


async def test_cache_encrypted_to_another_identity_requires_sign_in(tmp_path: Path) -> None:
    """BR-D01: a cache this identity cannot decrypt means the account must sign in again."""
    fake = FakeHttpClient(ACCOUNT)
    provider, _, cache_path = _provider(tmp_path, fake)
    other_identity = pyrage.x25519.Identity.generate()
    cache_path.write_bytes(pyrage.encrypt(b"{}", [other_identity.to_public()]))

    with pytest.raises(AuthRequired) as raised:
        await provider.token()

    assert raised.value.account == ACCOUNT


async def test_login_over_an_unreadable_cache_succeeds(tmp_path: Path) -> None:
    """R-7.9, BR-D01: a fresh sign-in replaces an unreadable cache and leaves a usable one."""
    fake = FakeHttpClient(ACCOUNT)
    provider, identity, cache_path = _provider(tmp_path, fake)
    cache_path.write_bytes(b"not an age file")

    _login(provider)

    saved = pyrage.decrypt(cache_path.read_bytes(), [identity]).decode("utf-8")
    assert SIGNED_IN_ACCESS in saved
    assert await provider.token() == SIGNED_IN_ACCESS


async def test_invalid_grant_requires_sign_in(tmp_path: Path) -> None:
    """BR-D01: a refused refresh token means the account must sign in again."""
    fake = FakeHttpClient(ACCOUNT, access_token_lifetime_s=100)
    provider, _, _ = _provider(tmp_path, fake)
    _login(provider)
    fake.calls.clear()
    fake.invalid_grant = True

    with pytest.raises(AuthRequired):
        await provider.token()

    assert [post["grant_type"] for post in fake.token_posts()] == ["refresh_token"]
