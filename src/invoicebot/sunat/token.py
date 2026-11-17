"""The SUNAT validez API OAuth token, cached in ``kv`` until 60 s before expiry (R-4.3).

The client credentials come from the adapter's scoped secrets view; the token
and the secrets never reach a log or an exception text.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
from psycopg import AsyncConnection

from invoicebot.config.models import ValidezApi
from invoicebot.secrets import Secrets
from invoicebot.store import kv

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


class SunatUnavailable(Exception):
    """The SUNAT validez API could not be reached or answered unusably (R-4.1, R-4.3)."""


async def token(
    conn: AsyncConnection[Any],
    http: httpx.AsyncClient,
    secrets: Secrets,
    cfg: ValidezApi,
) -> str:
    """Return the SUNAT access token, fetching and caching one on a miss (R-4.3).

    Does not commit: the caller owns the transaction. The token is stored with
    a TTL of ``expires_in - 60 s`` so it is refreshed before it expires.
    """
    cached = await kv.get(conn, CACHE_KEY)
    if cached is not None:
        return cached

    access_token, expires_in = await _fetch(http, secrets, cfg)
    await kv.put(conn, CACHE_KEY, access_token, timedelta(seconds=expires_in - REFRESH_MARGIN_S))
    return access_token


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
        response = await http.post(url, data=form, timeout=cfg.timeout_s)
    except httpx.TimeoutException:
        raise SunatUnavailable("sunat token request timed out") from None
    except httpx.HTTPError:
        raise SunatUnavailable("sunat token request failed") from None

    if response.status_code != httpx.codes.OK:
        raise SunatUnavailable(f"sunat token request failed with status {response.status_code}")

    try:
        payload = response.json()
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
