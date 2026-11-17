"""Integration tests for the SUNAT validez API token cache (R-4.3)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.config.models import ValidezApi
from invoicebot.secrets import Secrets, SecretStore
from invoicebot.store import kv
from invoicebot.sunat.token import (
    ADAPTER,
    CACHE_KEY,
    SECRETS,
    SunatUnavailable,
    token,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

CLIENT_ID = "synthetic-client-id"
CLIENT_CREDENTIAL = "synthetic-client-secret"
ACCESS_VALUE = "synthetic-access-token"
REFRESHED_VALUE = "synthetic-refreshed-token"

ENDPOINT_TEMPLATE = "https://api-seguridad.example.test/v1/clientesextranet/{client_id}/oauth2/token/"
FILLED_ENDPOINT = "https://api-seguridad.example.test/v1/clientesextranet/synthetic-client-id/oauth2/token/"
API_URL = "https://api.example.test/v1/contribuyente/contribuyentes/{ruc}/validarcomprobante"
SCOPE = "https://api.sunat.gob.pe/v1/contribuyente/contribuyentes"

#: Bodies that lack a usable ``access_token`` or a positive int ``expires_in``.
MALFORMED_BODIES: list[bytes | dict[str, Any]] = [
    b"not json",
    {"expires_in": 3600},
    {"access_token": "", "expires_in": 3600},
    {"access_token": ACCESS_VALUE},
    {"access_token": ACCESS_VALUE, "expires_in": "3600"},
    {"access_token": ACCESS_VALUE, "expires_in": True, "echo": CLIENT_CREDENTIAL},
    {"access_token": ACCESS_VALUE, "expires_in": 0},
]


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def empty_kv(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> AsyncIterator[None]:
    """Start each test from an empty ``kv`` table."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM kv")
    yield


def _secrets() -> Secrets:
    """The adapter's scoped view over synthetic client credentials."""
    return SecretStore({"sunat_client_id": CLIENT_ID, "sunat_client_secret": CLIENT_CREDENTIAL}).scoped(
        ADAPTER, set(SECRETS)
    )


def _cfg() -> ValidezApi:
    """The validez API settings the token client reads."""
    return ValidezApi(enabled=True, token_url=ENDPOINT_TEMPLATE, api_url=API_URL, timeout_s=5)


def _assert_secret_free(message: str) -> None:
    """R-4.3: an exception text names no client id, secret or token."""
    assert CLIENT_ID not in message
    assert CLIENT_CREDENTIAL not in message
    assert ACCESS_VALUE not in message


async def test_first_call_posts_the_form_and_caches_the_token(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.3: a kv miss POSTs the client_credentials form and caches the token."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"access_token": ACCESS_VALUE, "expires_in": 3600})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        db_pool.connection() as conn,
    ):
        found = await token(conn, http, _secrets(), _cfg())

    assert found == ACCESS_VALUE
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == FILLED_ENDPOINT
    assert dict(httpx.QueryParams(requests[0].content.decode())) == {
        "grant_type": "client_credentials",
        "scope": SCOPE,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_CREDENTIAL,
    }

    async with db_pool.connection() as conn:
        cached = await kv.get(conn, CACHE_KEY)
        cur = await conn.execute("SELECT expires_at - now() FROM kv WHERE key = %s", (CACHE_KEY,))
        row = await cur.fetchone()
    assert cached == ACCESS_VALUE
    assert row is not None
    # The 3600 s lifetime is stored 60 s short, so the refresh happens before expiry.
    assert 3480 <= row[0].total_seconds() <= 3600


async def test_second_call_within_the_ttl_makes_no_request(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.3: a cached token is returned without another POST."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"access_token": ACCESS_VALUE, "expires_in": 3600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with db_pool.connection() as conn:
            first = await token(conn, http, _secrets(), _cfg())
        async with db_pool.connection() as conn:
            second = await token(conn, http, _secrets(), _cfg())

    assert calls == 1
    assert first == ACCESS_VALUE
    assert second == ACCESS_VALUE


async def test_cached_token_59_seconds_from_expiry_is_refreshed(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.3: a cached token near expiry is replaced by a freshly fetched one."""
    # kv expires_at already carries the 60 s margin, so a token 59 s from expiry sits 1 s in the past.
    async with db_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO kv (key, value, expires_at) VALUES (%s, %s, now() - interval '1 second')",
            (CACHE_KEY, "synthetic-stale-token"),
        )

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"access_token": REFRESHED_VALUE, "expires_in": 3600})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        db_pool.connection() as conn,
    ):
        found = await token(conn, http, _secrets(), _cfg())

    assert found == REFRESHED_VALUE
    assert len(requests) == 1


async def test_non_200_raises_sunat_unavailable_without_secrets(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.3: a 401 raises SunatUnavailable whose text holds no secret or token."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": "invalid_client", "echo": CLIENT_CREDENTIAL, "token": ACCESS_VALUE},
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        db_pool.connection() as conn,
    ):
        with pytest.raises(SunatUnavailable) as excinfo:
            await token(conn, http, _secrets(), _cfg())

    message = str(excinfo.value)
    assert "401" in message
    _assert_secret_free(message)


async def test_timeout_raises_sunat_unavailable_without_secrets(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.3: a timeout raises SunatUnavailable whose text holds no secret or token."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        db_pool.connection() as conn,
    ):
        with pytest.raises(SunatUnavailable) as excinfo:
            await token(conn, http, _secrets(), _cfg())

    message = str(excinfo.value)
    assert "timed out" in message
    _assert_secret_free(message)


@pytest.mark.parametrize("body", MALFORMED_BODIES)
async def test_malformed_body_raises_sunat_unavailable_without_secrets(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], body: bytes | dict[str, Any]
) -> None:
    """R-4.3: an unusable body raises SunatUnavailable whose text holds no secret or token."""

    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, bytes):
            return httpx.Response(200, content=body)
        return httpx.Response(200, json=body)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        db_pool.connection() as conn,
    ):
        with pytest.raises(SunatUnavailable) as excinfo:
            await token(conn, http, _secrets(), _cfg())

    _assert_secret_free(str(excinfo.value))
