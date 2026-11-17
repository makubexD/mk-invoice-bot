"""Google OAuth refresh tokens and the access token they yield (R-6.4, R-9.9, D128).

An account's refresh token lives in one ``<data_dir>/tokens/<account>.google.age``
file, age-encrypted with the bot's identity as the MSAL cache is (D85). The access
token it buys is cached in memory until 60 s before it expires (R-4.3, D128).

A missing or unreadable token file, or a refresh Google refuses with
``invalid_grant``, raises :class:`AuthRequired`, which the pipeline turns into
``PENDING_AUTH`` (BR-D01). No refresh token, access token or client secret ever
appears in a message, a log line or an exception (R-9.9, R-9.16).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from pathlib import Path
from secrets import token_hex

import httpx
import pyrage  # type: ignore[import-untyped]  # pyrage ships no type information

from invoicebot.auth.msal_cache import AuthRequired
from invoicebot.sinks.google_api import GoogleError, GoogleTransient

#: The token endpoint every Google OAuth client refreshes against (D128).
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105  # nosec B105  # public endpoint, not a secret
#: The adapter and secret the Google OAuth clients sign in with (R-9.10, D128).
GOOGLE_OAUTH_ADAPTER = "google_oauth"
#: The age-store key that holds the OAuth client secret (D128).
GOOGLE_CLIENT_SECRET = "google_client_secret"  # noqa: S105  # nosec B105  # the key's name, not a secret

#: A cached token is refreshed this long before it expires (R-4.3).
EXPIRY_SKEW_S = 60
#: Google's access tokens last one hour; a longer ``expires_in`` is clamped to it (D128).
MAX_TOKEN_LIFETIME_S = 3600
#: The error code Google answers a refused refresh token with (BR-D01).
INVALID_GRANT = "invalid_grant"


class GoogleTokens:
    """One Google account's encrypted refresh token and the access token it buys (D128).

    Long-lived: one instance per signed-in account, sharing one HTTP client.
    Concurrent callers serialize on one lock, so an expired token is refreshed once.
    """

    def __init__(  # noqa: PLR0913  # the plan fixes this constructor's seven settings (D128)
        self,
        http: httpx.AsyncClient,
        client_id: str,
        client_secret: str,
        account: str,
        token_path: Path,
        identity: pyrage.x25519.Identity,
        timeout_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._account = account
        self._token_path = Path(token_path)
        self._identity = identity
        self._timeout_s = timeout_s
        self._clock = clock
        self._lock = asyncio.Lock()
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._token_file_signature: tuple[int, int, int] | None = None

    async def token(self) -> str:
        """Return a current access token, refreshing it when it is near expiry (D128).

        Raises:
            AuthRequired: the token file is missing or unreadable, or the refresh
                was refused with ``invalid_grant``.
            GoogleTransient: the refresh answered 5xx or never reached Google.
            GoogleError: the refresh was refused with another status.
        """
        async with self._lock:
            if (
                self._access_token is not None
                and self._clock() < self._expires_at - EXPIRY_SKEW_S
                and self._token_file_unchanged()
            ):
                return self._access_token
            return await self._refresh()

    async def _refresh(self) -> str:
        """Exchange the stored refresh token for a new access token and cache it."""
        self._access_token = None
        self._token_file_signature = None
        try:
            token_file_signature = _file_signature(self._token_path.stat())
        except OSError as err:
            raise AuthRequired(self._account) from err
        refresh_token = self._load_refresh_token()
        try:
            response = await self._http.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": refresh_token,
                },
                timeout=self._timeout_s,
            )
        except httpx.HTTPError as err:
            raise GoogleTransient() from err
        if response.status_code != httpx.codes.OK:
            _raise_refresh_error(response, self._account)
        access_token, lifetime_s = _access_token(response)
        self._access_token = access_token
        self._expires_at = self._clock() + min(lifetime_s, MAX_TOKEN_LIFETIME_S)
        self._token_file_signature = token_file_signature
        return access_token

    def _token_file_unchanged(self) -> bool:
        """Return whether the token file is the one the cached token was bought with.

        A ``stat`` that fails counts as changed: the file is gone or unreadable, so
        the next refresh raises :class:`AuthRequired` as a missing file does.
        """
        try:
            return _file_signature(self._token_path.stat()) == self._token_file_signature
        except OSError:
            return False

    def _load_refresh_token(self) -> str:
        """Decrypt the account's refresh token; a missing or unreadable file needs a sign-in."""
        try:
            ciphertext = self._token_path.read_bytes()
            plaintext: bytes = pyrage.decrypt(ciphertext, [self._identity])
            refresh_token = plaintext.decode("utf-8").strip()
        except (OSError, pyrage.DecryptError, UnicodeDecodeError) as err:
            raise AuthRequired(self._account) from err
        if not refresh_token:
            raise AuthRequired(self._account)
        return refresh_token


def save_refresh_token(
    path: Path,
    identity: pyrage.x25519.Identity,
    refresh_token: str,
) -> None:
    """Encrypt ``refresh_token`` to ``identity`` and write ``path`` atomically (D128).

    The ciphertext goes through a private temporary file in the same directory and
    is renamed into place, so a reader never sees a partial file (R-9.9).
    """
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    ciphertext = pyrage.encrypt(refresh_token.encode("utf-8"), [identity.to_public()])
    temporary = path.with_name(f".{path.name}.{token_hex(8)}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(ciphertext)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _file_signature(info: os.stat_result) -> tuple[int, int, int]:
    """Identify the token file by inode, nanosecond mtime and size, so any rewrite is seen (R-4.3)."""
    return (info.st_ino, info.st_mtime_ns, info.st_size)


def _raise_refresh_error(response: httpx.Response, account: str) -> None:
    """Raise the sign-in, retryable or final error a refused refresh maps to (R-6.4, BR-D01)."""
    status = response.status_code
    if status == httpx.codes.BAD_REQUEST and _error_code(response) == INVALID_GRANT:
        raise AuthRequired(account)
    if status >= httpx.codes.INTERNAL_SERVER_ERROR:
        raise GoogleTransient(status)
    raise GoogleError(status)


def _error_code(response: httpx.Response) -> str | None:
    """Return the OAuth ``error`` code of a response, or None when it has none."""
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    code = payload.get("error")
    return code if isinstance(code, str) else None


def _access_token(response: httpx.Response) -> tuple[str, int]:
    """Return the token and its lifetime from a successful token response.

    Raises:
        GoogleError: the body is not the token response Google promises; it never
            reaches the caller's message, so a malformed body stays opaque (R-9.16).
    """
    try:
        payload = response.json()
        access_token = payload["access_token"]
        lifetime_s = int(payload["expires_in"])
    except (KeyError, TypeError, ValueError):
        raise GoogleError(response.status_code) from None
    if not isinstance(access_token, str) or not access_token:
        raise GoogleError(response.status_code)
    return access_token, lifetime_s
