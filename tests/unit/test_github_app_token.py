"""Tests for the GitHub App installation token (R-11.10, R-9.16, BR-D05)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from invoicebot.github.app_token import GitHubApp, GitHubTokenError

APP_ID = 123456
INSTALLATION_ID = 987654
TIMEOUT_S = 10.0
TOKEN_URL = f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(scope="module")
def private_key_pem() -> str:
    """Generate a disposable RSA key in the PEM format GitHub Apps use."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@contextlib.asynccontextmanager
async def _make_app(handler: Handler, private_key_pem: str) -> AsyncIterator[GitHubApp]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        yield GitHubApp(http, APP_ID, INSTALLATION_ID, private_key_pem, TIMEOUT_S)


def _token_body(
    token: str,
    *,
    expires_in: timedelta,
    permissions: object = None,
) -> dict[str, object]:
    expires_at = datetime.now(UTC) + expires_in
    return {
        "token": token,
        "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "permissions": {"issues": "write"} if permissions is None else permissions,
    }


async def test_token_is_fetched_once_and_reused(private_key_pem: str) -> None:
    """R-11.10: the installation token is fetched once and reused on the second call."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_token_body("token-1", expires_in=timedelta(hours=1)))

    async with _make_app(handler, private_key_pem) as app:
        first = await app.token()
        second = await app.token()

    assert (first, second) == ("token-1", "token-1")
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == TOKEN_URL
    claims = jwt.decode(
        requests[0].headers["Authorization"][len("Bearer ") :], options={"verify_signature": False}
    )
    assert claims["iss"] == str(APP_ID)
    assert claims["exp"] - claims["iat"] == 600


async def test_token_is_fetched_again_after_invalidate(private_key_pem: str) -> None:
    """R-11.10: after invalidate the next token() fetches a new token instead of the cache."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_token_body(f"token-{len(requests)}", expires_in=timedelta(hours=1)),
        )

    async with _make_app(handler, private_key_pem) as app:
        first = await app.token()
        app.invalidate()
        second = await app.token()

    assert (first, second) == ("token-1", "token-2")
    assert len(requests) == 2


async def test_token_four_minutes_from_expiry_is_refreshed(private_key_pem: str) -> None:
    """R-11.10: a token inside the five-minute margin is fetched again."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_token_body(f"token-{len(requests)}", expires_in=timedelta(minutes=4)),
        )

    async with _make_app(handler, private_key_pem) as app:
        first = await app.token()
        second = await app.token()

    assert (first, second) == ("token-1", "token-2")
    assert len(requests) == 2


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (401, {}, "bad_key"),
        (404, {}, "uninstalled"),
        (
            200,
            _token_body(
                "token-1",
                expires_in=timedelta(hours=1),
                permissions={"issues": "read"},
            ),
            "no_permission",
        ),
    ],
)
async def test_token_failure_maps_to_its_reason(
    status: int,
    body: dict[str, object],
    reason: str,
    private_key_pem: str,
) -> None:
    """BR-D05: 401, 404 and a token without ``issues: write`` map to their reasons."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    async with _make_app(handler, private_key_pem) as app:
        with pytest.raises(GitHubTokenError) as raised:
            await app.token()

    assert raised.value.reason == reason


async def test_timeout_maps_to_unreachable(private_key_pem: str) -> None:
    """BR-D05: a timeout is an unreachable GitHub, not a bad key."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with _make_app(handler, private_key_pem) as app:
        with pytest.raises(GitHubTokenError) as raised:
            await app.token()

    assert raised.value.reason == "unreachable"


async def test_repr_and_errors_never_contain_the_private_key(private_key_pem: str) -> None:
    """R-9.16: the PEM never appears in ``repr(app)``, ``str(exc)`` or ``repr(exc)``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    async with _make_app(handler, private_key_pem) as app:
        with pytest.raises(GitHubTokenError) as raised:
            await app.token()

    assert private_key_pem not in str(raised.value)
    assert private_key_pem not in repr(raised.value)
    assert private_key_pem not in repr(app)
