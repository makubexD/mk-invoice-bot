"""Fetch and cache a GitHub App installation token (R-11.10, R-9.16, BR-D05).

A :class:`GitHubApp` signs a short-lived RS256 JWT with the App's private key,
exchanges it for an installation access token and reuses that token until five
minutes before it expires. The private key never appears in an exception or a
``repr``; every failure carries one of four reasons.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

GITHUB_API_BASE_URL = "https://api.github.com"
#: The JWT's issued-at is backdated this far, in seconds, to cover clock skew.
JWT_CLOCK_SKEW_S = 60
#: The JWT lifetime GitHub recommends: no more than ten minutes.
JWT_LIFETIME_S = 540
#: A cached token is fetched again once it has this much life left.
REFRESH_MARGIN = timedelta(minutes=5)

TokenFailure = Literal["bad_key", "uninstalled", "no_permission", "unreachable"]

_KEY_ERRORS = (ValueError, TypeError, UnsupportedAlgorithm)


class GitHubTokenError(Exception):
    """A GitHub App installation token could not be obtained (R-11.10, BR-D05)."""

    def __init__(self, reason: TokenFailure) -> None:
        self.reason = reason
        super().__init__(reason)


class GitHubApp:
    """A long-lived cache of one installation's access token (R-11.10)."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        app_id: int,
        installation_id: int,
        private_key_pem: str,
        timeout_s: float,
    ) -> None:
        self._http = http
        self._app_id = app_id
        self._installation_id = installation_id
        self._private_key_pem = private_key_pem
        self._timeout_s = timeout_s
        self._key: RSAPrivateKey | None = None
        self._token: str | None = None
        self._expires_at: datetime | None = None

    def __repr__(self) -> str:
        return f"GitHubApp(app_id={self._app_id}, installation_id={self._installation_id})"

    async def token(self) -> str:
        """Return an access token, fetching one only when the cache is near expiry."""
        cached = self._cached_token()
        if cached is not None:
            return cached
        return await self._fetch_token()

    def invalidate(self) -> None:
        """Drop the cached token, so the next :meth:`token` exchange fetches a new one (R-11.10)."""
        self._token = None
        self._expires_at = None

    def _cached_token(self) -> str | None:
        if self._token is None or self._expires_at is None:
            return None
        if self._expires_at - datetime.now(UTC) <= REFRESH_MARGIN:
            return None
        return self._token

    async def _fetch_token(self) -> str:
        signed_jwt = self._signed_jwt()
        try:
            response = await self._http.post(
                f"{GITHUB_API_BASE_URL}/app/installations/{self._installation_id}/access_tokens",
                headers={"Authorization": f"Bearer {signed_jwt}"},
                timeout=self._timeout_s,
            )
        except httpx.TransportError:
            raise GitHubTokenError("unreachable") from None

        reason = _reason_for_status(response.status_code)
        if reason is not None:
            raise GitHubTokenError(reason)

        body = _json_object(response)
        if body is None:
            raise GitHubTokenError("unreachable")
        if not _can_write_issues(body.get("permissions")):
            raise GitHubTokenError("no_permission")

        token = body.get("token")
        expires_at = _parse_expires_at(body.get("expires_at"))
        if not isinstance(token, str) or expires_at is None:
            raise GitHubTokenError("unreachable")

        self._token = token
        self._expires_at = expires_at
        return token

    def _signed_jwt(self) -> str:
        key = self._load_key()
        issued_at = int(time.time())
        payload = {
            "iat": issued_at - JWT_CLOCK_SKEW_S,
            "exp": issued_at + JWT_LIFETIME_S,
            "iss": str(self._app_id),
        }
        try:
            return jwt.encode(payload, key, algorithm="RS256")
        except jwt.exceptions.PyJWTError:
            raise GitHubTokenError("bad_key") from None

    def _load_key(self) -> RSAPrivateKey:
        if self._key is not None:
            return self._key
        try:
            key = load_pem_private_key(self._private_key_pem.encode(), password=None)
        except _KEY_ERRORS:
            raise GitHubTokenError("bad_key") from None
        if not isinstance(key, RSAPrivateKey):
            raise GitHubTokenError("bad_key")
        self._key = key
        return key


def _reason_for_status(status: int) -> TokenFailure | None:
    if 200 <= status < 300:
        return None
    if status == 401:
        return "bad_key"
    if status == 404:
        return "uninstalled"
    return "unreachable"


def _json_object(response: httpx.Response) -> dict[str, object] | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _can_write_issues(permissions: object) -> bool:
    return isinstance(permissions, dict) and permissions.get("issues") == "write"


def _parse_expires_at(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
