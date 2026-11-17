"""Google's consent URL, PKCE pair and loopback redirect parse (R-6.5, D128).

The installed-app sign-in builds the authorization URL, sends the owner there and
reads the code Google redirects back with. This module is pure: it does no I/O and
knows nothing about the token endpoint or the token store.

A redirect whose ``state`` does not match the one this sign-in issued, that carries
Google's ``error`` parameter, or that has no code raises :class:`ConsentError`; the
command reports it and writes no token (R-6.5).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from collections.abc import Sequence
from urllib.parse import parse_qs, urlencode, urlsplit

#: Google's authorization endpoint for the installed-app flow (D128).
AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
#: The only PKCE method Google accepts for a desktop client (D128).
CODE_CHALLENGE_METHOD = "S256"
#: ``offline`` is what makes Google return a refresh token (D128).
ACCESS_TYPE_OFFLINE = "offline"
#: Force the consent screen, so a re-sign-in still yields a refresh token (D128).
PROMPT_CONSENT = "consent"
#: The authorization code response type (R-6.5).
RESPONSE_TYPE_CODE = "code"
#: The number of random bytes behind a verifier (``secrets.token_urlsafe`` expands them).
VERIFIER_BYTES = 64
#: The query parameters this module reads from the redirect (R-6.5).
STATE_PARAM = "state"
CODE_PARAM = "code"
ERROR_PARAM = "error"


class ConsentError(Exception):
    """The redirect cannot be trusted to carry a usable authorization code (R-6.5)."""


def pkce_pair() -> tuple[str, str]:
    """Return a ``(verifier, challenge)`` pair: the challenge is base64url SHA-256 (D128)."""
    verifier = secrets.token_urlsafe(VERIFIER_BYTES)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def consent_url(
    client_id: str,
    redirect_uri: str,
    scopes: Sequence[str],
    state: str,
    challenge: str,
) -> str:
    """Build the URL the owner opens to grant ``scopes`` to this sign-in (R-6.5, D128)."""
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": RESPONSE_TYPE_CODE,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": CODE_CHALLENGE_METHOD,
            "access_type": ACCESS_TYPE_OFFLINE,
            "prompt": PROMPT_CONSENT,
        }
    )
    return f"{AUTHORIZATION_URL}?{query}"


def parse_redirect(path: str, expected_state: str) -> str:
    """Return the code in a loopback redirect's query, or raise :class:`ConsentError` (R-6.5).

    Raises:
        ConsentError: the state is missing or differs, Google returned an ``error``,
            or the redirect carries no code.
    """
    query = parse_qs(urlsplit(path).query, keep_blank_values=True)
    state = query.get(STATE_PARAM, [""])[0]
    if not hmac.compare_digest(state.encode(), expected_state.encode()):
        raise ConsentError("the redirect's state does not match this sign-in")
    error = query.get(ERROR_PARAM, [""])[0]
    if error:
        raise ConsentError(f"Google refused the consent: {error}")
    code = query.get(CODE_PARAM, [""])[0]
    if not code:
        raise ConsentError("the redirect carries no authorization code")
    return code
