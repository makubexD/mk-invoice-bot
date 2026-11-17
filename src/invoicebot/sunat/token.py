"""The SUNAT validez API OAuth token, cached in ``kv`` until 60 s before expiry (R-4.3).

The client credentials come from the adapter's scoped secrets view; the token
and the secrets never reach a log or an exception text.
"""

from __future__ import annotations

import json
from datetime import timedelta

import httpx

from invoicebot.config.models import ValidezApi
from invoicebot.secrets import Secrets
from invoicebot.store import kv
from invoicebot.store.db import Pool

#: The adapter name its scoped secrets are declared under (R-9.10).
ADAPTER = "sunat_validez"

#: The secrets this adapter declares (R-9.10).
SECRETS = frozenset({"sunat_client_id", "sunat_client_secret"})

#: The ``kv`` key that holds the live token (R-4.3).
CACHE_KEY = "sunat_token"

#: The OAuth scope SUNAT's contribuyente API expects.
SCOPE = "https://api.sunat.gob.pe/v1/contribuyente/contribuyentes"

#: The token is fetched again this long before its real expiry (R-4.3).
REFRESH_MARGIN_S = 60

#: The longest a token is cached, whatever ``expires_in`` claims (R-4.3).
MAX_TOKEN_TTL_S = 24 * 60 * 60

#: The most of a SUNAT response this module reads (R-4.1).
MAX_RESPONSE_BYTES = 64 * 1024


class SunatUnavailable(Exception):
    """The SUNAT validez API could not be reached or answered unusably (R-4.1, R-4.3)."""


class BodyTooLarge(Exception):
    """A SUNAT response outgrew ``MAX_RESPONSE_BYTES`` (R-4.1)."""


async def token(
    pool: Pool,
    http: httpx.AsyncClient,
    secrets: Secrets,
    cfg: ValidezApi,
) -> str:
    """Return the SUNAT access token, fetching and caching one on a miss (R-4.3).

    The cache is read and written on short connections of their own, so the fetch
    runs with no connection or transaction held. The token is cached for
    ``expires_in``, capped at :data:`MAX_TOKEN_TTL_S` and 60 s short so it is
    refreshed before it expires; a lifetime that leaves no time after the margin
    returns the token uncached.
    """
    async with pool.connection() as conn:
        cached = await kv.get(conn, CACHE_KEY)
    if cached is not None:
        return cached

    access_token, expires_in = await _fetch(http, secrets, cfg)
    ttl_s = min(expires_in, MAX_TOKEN_TTL_S) - REFRESH_MARGIN_S
    if ttl_s <= 0:
        return access_token
    async with pool.connection() as conn, conn.transaction():
        await kv.put(conn, CACHE_KEY, access_token, timedelta(seconds=ttl_s))
    return access_token


async def forget(pool: Pool) -> None:
    """Drop the cached token so the next :func:`token` call fetches a fresh one (R-4.3).

    Uses a short connection and transaction of its own, so no connection is held
    across the caller's HTTP calls.
    """
    async with pool.connection() as conn, conn.transaction():
        await kv.delete(conn, CACHE_KEY)


async def _fetch(http: httpx.AsyncClient, secrets: Secrets, cfg: ValidezApi) -> tuple[str, int]:
    """POST the client_credentials form and return the validated token fields (R-4.3)."""
    url = cfg.token_url.format(client_id=secrets.get("sunat_client_id"))
    form = {
        "grant_type": "client_credentials",
        "scope": SCOPE,
        "client_id": secrets.get("sunat_client_id"),
        "client_secret": secrets.get("sunat_client_secret"),
    }
    try:
        async with http.stream("POST", url, data=form, timeout=cfg.timeout_s) as response:
            if response.status_code != httpx.codes.OK:
                raise SunatUnavailable(f"sunat token request failed with status {response.status_code}")
            raw = await read_capped(response)
    except httpx.TimeoutException:
        raise SunatUnavailable("sunat token request timed out") from None
    except httpx.HTTPError:
        raise SunatUnavailable("sunat token request failed") from None
    except BodyTooLarge:
        raise SunatUnavailable("sunat token response is too large") from None

    try:
        payload = json.loads(raw)
    except ValueError:
        raise SunatUnavailable("sunat token response is not valid JSON") from None
    if not isinstance(payload, dict):
        raise SunatUnavailable("sunat token response is not a JSON object")

    access_token = payload.get("access_token")
    expires_in = payload.get("expires_in")
    if not isinstance(access_token, str) or not access_token:
        raise SunatUnavailable("sunat token response has no access_token")
    if isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in <= 0:
        raise SunatUnavailable("sunat token response has no positive expires_in")
    return access_token, expires_in


async def read_capped(response: httpx.Response) -> bytes:
    """Read the body under ``MAX_RESPONSE_BYTES``, refusing it once past the cap (R-4.1).

    Raises:
        BodyTooLarge: the body passed the cap before its end.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise BodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)
