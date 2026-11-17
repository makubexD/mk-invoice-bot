"""Tests for the Google OAuth token provider (R-6.4, R-4.3, R-9.9, R-9.16, BR-D01, D128)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import urllib.parse
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pyrage
import pytest

from invoicebot.auth.google_oauth import TOKEN_URL, GoogleTokens, save_refresh_token
from invoicebot.auth.msal_cache import AuthRequired
from invoicebot.sinks.google_api import GoogleError, GoogleTransient

ACCOUNT = "owner"
CLIENT_ID = "synthetic-client-id"
CLIENT_SECRET = "synthetic-client-secret"  # noqa: S105  # a synthetic test value
REFRESH_TOKEN = "synthetic-refresh-token"  # noqa: S105  # a synthetic test value
ACCESS_TOKEN = "synthetic-access-token"  # noqa: S105  # a synthetic test value
TIMEOUT_S = 5.0

Handler = Callable[[httpx.Request], httpx.Response]


class Clock:
    """A monotonic clock the test moves by hand, so no test ever sleeps (D128)."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _account(tmp_path: Path, *, seeded: bool = True) -> tuple[Path, pyrage.x25519.Identity]:
    """Return a token path and identity, with the refresh token saved unless told otherwise."""
    identity = pyrage.x25519.Identity.generate()
    token_path = tmp_path / "tokens" / f"{ACCOUNT}.google.age"
    if seeded:
        save_refresh_token(token_path, identity, REFRESH_TOKEN)
    return token_path, identity


def _refresh_handler(
    calls: list[httpx.Request],
    *,
    status: int = 200,
    expires_in: int = 3600,
    error: str | None = None,
) -> Handler:
    """Answer every refresh with the same canned response and record the request."""

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if error is not None:
            return httpx.Response(status, json={"error": error})
        return httpx.Response(status, json={"access_token": ACCESS_TOKEN, "expires_in": expires_in})

    return handle


@contextlib.asynccontextmanager
async def _make_tokens(
    token_path: Path,
    identity: pyrage.x25519.Identity,
    clock: Clock,
    handler: Handler,
) -> AsyncIterator[GoogleTokens]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        yield GoogleTokens(
            http,
            CLIENT_ID,
            CLIENT_SECRET,
            ACCOUNT,
            token_path,
            identity,
            TIMEOUT_S,
            clock=clock,
        )


def _form(request: httpx.Request) -> dict[str, str]:
    """Return the URL-encoded form the client posted."""
    return dict(urllib.parse.parse_qsl(request.content.decode("ascii")))


async def test_refresh_returns_the_token_and_reuses_it_within_the_ttl(tmp_path: Path) -> None:
    """R-6.4: a refresh returns the access token, and a second call near the start makes no request."""
    calls: list[httpx.Request] = []
    clock = Clock(1000.0)
    token_path, identity = _account(tmp_path)

    async with _make_tokens(token_path, identity, clock, _refresh_handler(calls)) as tokens:
        assert await tokens.token() == ACCESS_TOKEN
        clock.now += 3000
        assert await tokens.token() == ACCESS_TOKEN

    assert len(calls) == 1
    assert calls[0].method == "POST"
    assert calls[0].url == httpx.URL(TOKEN_URL)
    assert _form(calls[0]) == {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
    }


async def test_token_within_sixty_seconds_of_expiry_is_refreshed(tmp_path: Path) -> None:
    """R-4.3: a cached token with 60 s or less left is refreshed before use."""
    calls: list[httpx.Request] = []
    clock = Clock(0.0)
    token_path, identity = _account(tmp_path)

    async with _make_tokens(token_path, identity, clock, _refresh_handler(calls)) as tokens:
        await tokens.token()
        clock.now = 3540.0
        assert await tokens.token() == ACCESS_TOKEN

    assert len(calls) == 2


async def test_unchanged_token_file_reuses_the_cached_token(tmp_path: Path) -> None:
    """BR-D01: a token file nobody rewrote buys no second access token."""
    calls: list[httpx.Request] = []
    clock = Clock(0.0)
    token_path, identity = _account(tmp_path)

    async with _make_tokens(token_path, identity, clock, _refresh_handler(calls)) as tokens:
        assert await tokens.token() == ACCESS_TOKEN
        clock.now += 3000
        assert await tokens.token() == ACCESS_TOKEN

    assert len(calls) == 1


async def test_rewritten_token_file_refreshes_with_the_new_token(tmp_path: Path) -> None:
    """BR-D01: a token file saved again invalidates the cached access token."""
    calls: list[httpx.Request] = []
    access_tokens = iter(["first-access-token", "second-access-token"])
    clock = Clock(0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"access_token": next(access_tokens), "expires_in": 3600})

    token_path, identity = _account(tmp_path)
    async with _make_tokens(token_path, identity, clock, handler) as tokens:
        assert await tokens.token() == "first-access-token"
        save_refresh_token(token_path, identity, "rotated-refresh-token")
        rewritten_at = token_path.stat().st_mtime_ns + 1_000_000_000
        os.utime(token_path, ns=(rewritten_at, rewritten_at))
        assert await tokens.token() == "second-access-token"

    assert len(calls) == 2


async def test_token_file_replaced_with_the_same_mtime_refreshes(tmp_path: Path) -> None:
    """R-4.3: a token file replaced by a new inode with the old mtime is detected as changed."""
    calls: list[httpx.Request] = []
    access_tokens = iter(["first-access-token", "second-access-token"])
    clock = Clock(0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"access_token": next(access_tokens), "expires_in": 3600})

    token_path, identity = _account(tmp_path)
    async with _make_tokens(token_path, identity, clock, handler) as tokens:
        assert await tokens.token() == "first-access-token"
        original_mtime_ns = token_path.stat().st_mtime_ns
        replacement = tmp_path / "replacement.token"
        save_refresh_token(replacement, identity, "rotated-refresh-token")
        replacement.replace(token_path)
        os.utime(token_path, ns=(original_mtime_ns, original_mtime_ns))
        assert await tokens.token() == "second-access-token"

    assert len(calls) == 2


async def test_failed_refresh_after_rewritten_token_file_is_retried(tmp_path: Path) -> None:
    """BR-D01: a 5xx refresh of a rewritten token file never falls back to the old access token."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={"access_token": ACCESS_TOKEN, "expires_in": 3600})
        return httpx.Response(503)

    token_path, identity = _account(tmp_path)
    async with _make_tokens(token_path, identity, Clock(), handler) as tokens:
        assert await tokens.token() == ACCESS_TOKEN
        save_refresh_token(token_path, identity, "rotated-refresh-token")
        rewritten_at = token_path.stat().st_mtime_ns + 1_000_000_000
        os.utime(token_path, ns=(rewritten_at, rewritten_at))
        with pytest.raises(GoogleTransient) as caught:
            await tokens.token()
        with pytest.raises(GoogleTransient):
            await tokens.token()

    assert caught.value.status == 503
    assert len(calls) == 3


async def test_unreachable_refresh_after_rewritten_token_file_is_retried(tmp_path: Path) -> None:
    """BR-D01: a refresh that never reaches Google after a rewrite never serves the old token."""

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={"access_token": ACCESS_TOKEN, "expires_in": 3600})
        raise httpx.ConnectError("synthetic connection failure")

    token_path, identity = _account(tmp_path)
    async with _make_tokens(token_path, identity, Clock(), handler) as tokens:
        assert await tokens.token() == ACCESS_TOKEN
        save_refresh_token(token_path, identity, "rotated-refresh-token")
        rewritten_at = token_path.stat().st_mtime_ns + 1_000_000_000
        os.utime(token_path, ns=(rewritten_at, rewritten_at))
        with pytest.raises(GoogleTransient):
            await tokens.token()
        with pytest.raises(GoogleTransient):
            await tokens.token()

    assert len(calls) == 3


async def test_deleted_token_file_requires_sign_in(tmp_path: Path) -> None:
    """BR-D01: an account whose token file disappeared after a refresh has to sign in."""
    calls: list[httpx.Request] = []
    token_path, identity = _account(tmp_path)

    async with _make_tokens(token_path, identity, Clock(), _refresh_handler(calls)) as tokens:
        await tokens.token()
        token_path.unlink()
        with pytest.raises(AuthRequired) as caught:
            await tokens.token()

    assert caught.value.account == ACCOUNT
    assert len(calls) == 1


async def test_invalid_grant_requires_sign_in(tmp_path: Path) -> None:
    """BR-D01: a refresh token Google refuses needs an interactive sign-in."""
    calls: list[httpx.Request] = []
    token_path, identity = _account(tmp_path)
    handler = _refresh_handler(calls, status=400, error="invalid_grant")

    async with _make_tokens(token_path, identity, Clock(), handler) as tokens:
        with pytest.raises(AuthRequired) as caught:
            await tokens.token()

    assert caught.value.account == ACCOUNT
    assert len(calls) == 1


async def test_missing_token_file_requires_sign_in(tmp_path: Path) -> None:
    """BR-D01: an account with no saved refresh token has to sign in first."""
    calls: list[httpx.Request] = []
    token_path, identity = _account(tmp_path, seeded=False)

    async with _make_tokens(token_path, identity, Clock(), _refresh_handler(calls)) as tokens:
        with pytest.raises(AuthRequired) as caught:
            await tokens.token()

    assert caught.value.account == ACCOUNT
    assert calls == []


async def test_unreadable_token_file_requires_sign_in(tmp_path: Path) -> None:
    """BR-D01: a token file this identity cannot decrypt needs a fresh sign-in."""
    calls: list[httpx.Request] = []
    token_path, identity = _account(tmp_path, seeded=False)
    token_path.parent.mkdir(parents=True)
    token_path.write_bytes(b"not an age file")

    async with _make_tokens(token_path, identity, Clock(), _refresh_handler(calls)) as tokens:
        with pytest.raises(AuthRequired) as caught:
            await tokens.token()

    assert caught.value.account == ACCOUNT
    assert calls == []


async def test_server_error_raises_google_transient(tmp_path: Path) -> None:
    """R-6.4: a 5xx from the token endpoint is worth retrying."""
    calls: list[httpx.Request] = []
    token_path, identity = _account(tmp_path)
    handler = _refresh_handler(calls, status=503)

    async with _make_tokens(token_path, identity, Clock(), handler) as tokens:
        with pytest.raises(GoogleTransient) as caught:
            await tokens.token()

    assert caught.value.status == 503


async def test_network_failure_raises_google_transient_without_a_status(tmp_path: Path) -> None:
    """R-6.4: a refresh that never reaches Google is transient and carries no status."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic connection failure")

    token_path, identity = _account(tmp_path)

    async with _make_tokens(token_path, identity, Clock(), handler) as tokens:
        with pytest.raises(GoogleTransient) as caught:
            await tokens.token()

    assert caught.value.status is None


async def test_another_4xx_raises_google_error(tmp_path: Path) -> None:
    """R-6.4: a refusal other than invalid_grant is final, not retried."""
    calls: list[httpx.Request] = []
    token_path, identity = _account(tmp_path)
    handler = _refresh_handler(calls, status=403)

    async with _make_tokens(token_path, identity, Clock(), handler) as tokens:
        with pytest.raises(GoogleError) as caught:
            await tokens.token()

    assert not isinstance(caught.value, GoogleTransient)
    assert caught.value.status == 403


async def test_saved_token_is_encrypted_and_round_trips(tmp_path: Path) -> None:
    """R-9.9: the refresh token on disk is age ciphertext, private, and decrypts back."""
    identity = pyrage.x25519.Identity.generate()
    token_path = tmp_path / "tokens" / f"{ACCOUNT}.google.age"

    save_refresh_token(token_path, identity, REFRESH_TOKEN)

    raw = token_path.read_bytes()
    assert REFRESH_TOKEN.encode("ascii") not in raw
    assert pyrage.decrypt(raw, [identity]).decode("utf-8") == REFRESH_TOKEN
    assert token_path.stat().st_mode & 0o777 == 0o600
    assert token_path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        (400, "invalid_grant", AuthRequired),
        (403, None, GoogleError),
        (503, None, GoogleTransient),
    ],
)
async def test_no_error_text_carries_the_refresh_token(
    tmp_path: Path,
    status: int,
    error: str | None,
    expected: type[Exception],
) -> None:
    """R-9.16: no raised error's text or repr names the refresh token or the client secret."""
    calls: list[httpx.Request] = []
    token_path, identity = _account(tmp_path)
    handler = _refresh_handler(calls, status=status, error=error)

    async with _make_tokens(token_path, identity, Clock(), handler) as tokens:
        with pytest.raises(expected) as caught:
            await tokens.token()

    text = str(caught.value) + repr(caught.value)
    assert REFRESH_TOKEN not in text
    assert CLIENT_SECRET not in text


async def test_concurrent_callers_refresh_once(tmp_path: Path) -> None:
    """D128: callers that race on an expired token share one refresh under the lock."""
    calls: list[httpx.Request] = []
    token_path, identity = _account(tmp_path)

    async with _make_tokens(token_path, identity, Clock(), _refresh_handler(calls)) as tokens:
        results = await asyncio.gather(*(tokens.token() for _ in range(5)))

    assert results == [ACCESS_TOKEN] * 5
    assert len(calls) == 1


async def test_expires_in_is_clamped_to_one_hour(tmp_path: Path) -> None:
    """D128: a token lifetime longer than Google's hour is clamped to that hour."""
    calls: list[httpx.Request] = []
    clock = Clock(0.0)
    token_path, identity = _account(tmp_path)
    handler = _refresh_handler(calls, expires_in=7200)

    async with _make_tokens(token_path, identity, clock, handler) as tokens:
        await tokens.token()
        clock.now = 3539.0
        await tokens.token()
        clock.now = 3540.0
        await tokens.token()

    assert len(calls) == 2
