"""Integration tests for the SUNAT validez API token cache (R-4.3)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, ValidezApi
from invoicebot.extract.fields import Field, Reading
from invoicebot.pipeline.validate import ask_sunat
from invoicebot.secrets import Secrets, SecretStore
from invoicebot.store import kv
from invoicebot.store.invoices import CpStatus
from invoicebot.sunat.token import (
    ADAPTER,
    CACHE_KEY,
    MAX_RESPONSE_BYTES,
    MAX_TOKEN_TTL_S,
    SECRETS,
    SunatUnavailable,
    forget,
    token,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

CLIENT_ID = "synthetic-client-id"
CLIENT_CREDENTIAL = "synthetic-client-secret"
ACCESS_VALUE = "synthetic-access-token"
REFRESHED_VALUE = "synthetic-refreshed-token"
# Both values come from tests/synthetic_rucs.txt; no real RUC appears in this file.
OWNER_RUC = "20000000001"
ISSUER_RUC = "51900000000"

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"

#: The validez body a retried request gets back.
_VALIDEZ_ANSWER: dict[str, object] = {
    "success": True,
    "data": {"estadoCp": 1, "estadoRuc": "00", "condDomiRuc": "00"},
}

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


def _config() -> Config:
    """The app config ``ask_sunat`` reads, pointing at the synthetic owner and test APIs."""
    cfg = load_config(EXAMPLE)
    return cfg.model_copy(
        update={
            "owner": cfg.owner.model_copy(update={"ruc": OWNER_RUC}),
            "sunat": cfg.sunat.model_copy(update={"validez_api": _cfg()}),
        }
    )


def _reading() -> Reading:
    """Build a complete reading for the validez request."""
    return Reading(
        fields={
            "ruc_emisor": Field(value=ISSUER_RUC, source="qr", confidence=1.0),
            "tipo": Field(value="01", source="qr", confidence=1.0),
            "serie": Field(value="F001", source="qr", confidence=1.0),
            "numero": Field(value="123", source="qr", confidence=1.0),
            "fecha_emision": Field(value="2026-11-13", source="qr", confidence=1.0),
            "total": Field(value="118.50", source="ocr", confidence=1.0),
        },
        document_url=None,
    )


def _assert_secret_free(message: str) -> None:
    """R-4.3: an exception text names no client id, secret or token."""
    assert CLIENT_ID not in message
    assert CLIENT_CREDENTIAL not in message
    assert ACCESS_VALUE not in message


def _unreachable(request: httpx.Request) -> httpx.Response:
    """Fail a test whose token call makes an unexpected HTTP request."""
    pytest.fail(f"unexpected HTTP request: {request.method} {request.url}")


async def test_first_call_posts_the_form_and_caches_the_token(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.3: a kv miss POSTs the client_credentials form and caches the token."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"access_token": ACCESS_VALUE, "expires_in": 3600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        found = await token(db_pool, http, _secrets(), _cfg())

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
        first = await token(db_pool, http, _secrets(), _cfg())
        second = await token(db_pool, http, _secrets(), _cfg())

    assert calls == 1
    assert first == ACCESS_VALUE
    assert second == ACCESS_VALUE


async def test_cache_hit_makes_no_http_request(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.3: a live cached token is returned without any HTTP request."""
    async with db_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO kv (key, value, expires_at) VALUES (%s, %s, now() + interval '1 hour')",
            (CACHE_KEY, ACCESS_VALUE),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(_unreachable)) as http:
        found = await token(db_pool, http, _secrets(), _cfg())

    assert found == ACCESS_VALUE


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

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        found = await token(db_pool, http, _secrets(), _cfg())

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

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(SunatUnavailable) as excinfo:
            await token(db_pool, http, _secrets(), _cfg())

    message = str(excinfo.value)
    assert "401" in message
    _assert_secret_free(message)


async def test_timeout_raises_sunat_unavailable_without_secrets(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.3: a timeout raises SunatUnavailable whose text holds no secret or token."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(SunatUnavailable) as excinfo:
            await token(db_pool, http, _secrets(), _cfg())

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

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(SunatUnavailable) as excinfo:
            await token(db_pool, http, _secrets(), _cfg())

    _assert_secret_free(str(excinfo.value))


async def test_oversized_response_raises_sunat_unavailable(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.3: a token body past MAX_RESPONSE_BYTES raises SunatUnavailable and caches nothing."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(SunatUnavailable, match="too large"):
            await token(db_pool, http, _secrets(), _cfg())

    async with db_pool.connection() as conn:
        cached = await kv.get(conn, CACHE_KEY)
    assert cached is None


async def test_huge_expires_in_caches_at_most_one_day(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.3: expires_in = 10**15 caches with a TTL of at most MAX_TOKEN_TTL_S, without OverflowError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": ACCESS_VALUE, "expires_in": 10**15})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        found = await token(db_pool, http, _secrets(), _cfg())

    assert found == ACCESS_VALUE
    async with db_pool.connection() as conn:
        cur = await conn.execute("SELECT expires_at - now() FROM kv WHERE key = %s", (CACHE_KEY,))
        row = await cur.fetchone()
    assert row is not None
    assert 0 < row[0].total_seconds() <= MAX_TOKEN_TTL_S


async def test_short_expires_in_returns_the_token_without_caching_it(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.3: expires_in = 30 leaves no time after the margin, so the token is not cached."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": ACCESS_VALUE, "expires_in": 30})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        found = await token(db_pool, http, _secrets(), _cfg())

    assert found == ACCESS_VALUE
    async with db_pool.connection() as conn:
        cached = await kv.get(conn, CACHE_KEY)
    assert cached is None


async def test_validez_401_fetches_a_new_token_and_retries_once(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.3: a validez 401 drops the cached token, fetches another and returns the retried answer."""
    token_calls = 0
    validez_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if "oauth2/token" in str(request.url):
            token_calls += 1
            value = ACCESS_VALUE if token_calls == 1 else REFRESHED_VALUE
            return httpx.Response(200, json={"access_token": value, "expires_in": 3600})
        validez_requests.append(request)
        if len(validez_requests) == 1:
            return httpx.Response(401, json={"success": False})
        return httpx.Response(200, json=_VALIDEZ_ANSWER)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        status = await ask_sunat(db_pool, http, _secrets(), _config(), _reading())

    assert status == CpStatus(estado_cp=1, estado_ruc="00", cond_domi_ruc="00")
    assert token_calls == 2
    assert len(validez_requests) == 2
    assert validez_requests[0].headers["Authorization"] == f"Bearer {ACCESS_VALUE}"
    assert validez_requests[1].headers["Authorization"] == f"Bearer {REFRESHED_VALUE}"


async def test_two_validez_401s_are_unavailable_and_leave_no_token_cached(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.3: a second 401 reads as unavailable and leaves no token cached."""
    token_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if "oauth2/token" in str(request.url):
            token_calls += 1
            return httpx.Response(200, json={"access_token": REFRESHED_VALUE, "expires_in": 3600})
        return httpx.Response(401, json={"success": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        status = await ask_sunat(db_pool, http, _secrets(), _config(), _reading())

    assert status is None
    assert token_calls == 2
    async with db_pool.connection() as conn:
        cached = await kv.get(conn, CACHE_KEY)
    assert cached is None


async def test_forget_deletes_the_rejected_token(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.3: forget drops the stored token when it is still the one the 401 rejected."""
    async with db_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO kv (key, value, expires_at) VALUES (%s, %s, now() + interval '1 hour')",
            (CACHE_KEY, ACCESS_VALUE),
        )

    await forget(db_pool, ACCESS_VALUE)

    async with db_pool.connection() as conn:
        cached = await kv.get(conn, CACHE_KEY)
    assert cached is None


async def test_forget_keeps_a_token_fetched_after_the_401(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.3: forget leaves a token another worker stored after the rejected one."""
    async with db_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO kv (key, value, expires_at) VALUES (%s, %s, now() + interval '1 hour')",
            (CACHE_KEY, REFRESHED_VALUE),
        )

    await forget(db_pool, ACCESS_VALUE)

    async with db_pool.connection() as conn:
        cached = await kv.get(conn, CACHE_KEY)
    assert cached == REFRESHED_VALUE
