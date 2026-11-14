"""Per-account MSAL token caches, encrypted with the bot's age identity (R-7.9, D85).

Each account's MSAL cache is one ``<data_dir>/tokens/<account>.msal.age`` file,
encrypted to the recipient of the identity the caller passes in. MSAL only gets
and refreshes tokens here; Graph calls are plain REST (D84).

A missing cache, an account the cache does not hold, or a refused refresh raise
:class:`AuthRequired`, which the pipeline turns into ``PENDING_AUTH`` (BR-D01).
No token, refresh token or cache plaintext ever appears in a message or a log
(R-9.9).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from secrets import token_hex

import msal
import pyrage  # type: ignore[import-untyped]  # pyrage ships no type information

#: Personal Microsoft accounts only (D84).
AUTHORITY = "https://login.microsoftonline.com/consumers"

#: Scopes MSAL always adds itself and refuses as input; R-7.9 lists ``offline_access`` anyway.
_MSAL_RESERVED_SCOPES = frozenset({"offline_access", "openid", "profile"})


class AuthRequired(Exception):
    """The account needs an interactive sign-in before the sink can run (BR-D01).

    Carries the account name alone; never a token.
    """

    def __init__(self, account: str) -> None:
        super().__init__(f"Microsoft sign-in needed for {account}")
        self.account = account


class TokenProvider:
    """One account's encrypted MSAL cache and the tokens it yields (D85).

    Long-lived: one instance per signed-in account. File writes are atomic and
    only happen when MSAL changed the cache.
    """

    def __init__(
        self,
        client_id: str,
        account: str,
        scopes: Sequence[str],
        cache_path: Path,
        identity: pyrage.x25519.Identity,
        http_client: object | None = None,
    ) -> None:
        self._client_id = client_id
        self._account = account
        self._scopes = [scope for scope in scopes if scope not in _MSAL_RESERVED_SCOPES]
        self._cache_path = Path(cache_path)
        self._identity = identity
        self._http_client = http_client

    async def token(self) -> str:
        """Return a current access token, refreshing it silently if needed.

        The whole MSAL conversation runs in one worker thread because MSAL is
        synchronous (D84).

        Raises:
            AuthRequired: no cache, no matching account, or the refresh failed.
        """
        return await asyncio.to_thread(self._token)

    def login_device_code(self, show: Callable[[str], None]) -> None:
        """Sign the account in with the device-code flow and save the cache.

        Args:
            show: called once with the instruction message for the person
                running the CLI (the device code and verification URL).

        Raises:
            AuthRequired: the flow could not start or the sign-in failed.
        """
        cache = self._load_cache()
        app = self._application(cache)
        flow = app.initiate_device_flow(scopes=self._scopes)
        if "user_code" not in flow:
            raise AuthRequired(self._account)
        show(flow["message"])
        result = app.acquire_token_by_device_flow(flow)
        if "error" in result:
            raise AuthRequired(self._account)
        self._save_if_changed(cache)

    def _token(self) -> str:
        cache = self._load_cache()
        app = self._application(cache)
        accounts = app.get_accounts(username=self._account)
        if not accounts:
            raise AuthRequired(self._account)
        result = app.acquire_token_silent(self._scopes, account=accounts[0])
        self._save_if_changed(cache)
        if not result or "error" in result:
            raise AuthRequired(self._account)
        access_token = result.get("access_token")
        if not isinstance(access_token, str):
            raise AuthRequired(self._account)
        return access_token

    def _application(self, cache: msal.SerializableTokenCache) -> msal.PublicClientApplication:
        return msal.PublicClientApplication(
            self._client_id,
            authority=AUTHORITY,
            token_cache=cache,
            http_client=self._http_client,
        )

    def _load_cache(self) -> msal.SerializableTokenCache:
        cache = msal.SerializableTokenCache()
        if self._cache_path.exists():
            ciphertext = self._cache_path.read_bytes()
            plaintext = pyrage.decrypt(ciphertext, [self._identity])
            cache.deserialize(plaintext.decode("utf-8"))
        return cache

    def _save_if_changed(self, cache: msal.SerializableTokenCache) -> None:
        if cache.has_state_changed:
            self._write(cache)

    def _write(self, cache: msal.SerializableTokenCache) -> None:
        ciphertext = pyrage.encrypt(
            cache.serialize().encode("utf-8"),
            [self._identity.to_public()],
        )
        temporary = self._cache_path.with_name(f".{self._cache_path.name}.{token_hex(8)}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(ciphertext)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self._cache_path)
        finally:
            temporary.unlink(missing_ok=True)
