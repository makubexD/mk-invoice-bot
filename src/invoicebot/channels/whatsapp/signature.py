"""WhatsApp webhook authentication and privacy helpers (R-1.2, R-1.3, R-9.12).

Meta signs every webhook POST with the app secret and proves ownership of the
callback with a verify token, so both checks are pure functions over strings
and never touch I/O. Sender ids are only ever logged as a short keyed hash.
"""

from __future__ import annotations

import hashlib
import hmac

_SIGNATURE_PREFIX = "sha256="
_HASH_LENGTH = 12


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Return whether ``header`` carries a valid HMAC-SHA256 of the raw ``body`` (R-1.3)."""
    if not app_secret or header is None or not header.startswith(_SIGNATURE_PREFIX):
        return False
    try:
        provided = bytes.fromhex(header[len(_SIGNATURE_PREFIX) :])
    except ValueError:
        return False
    expected = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).digest()
    return hmac.compare_digest(expected, provided)


def verify_handshake(mode: str | None, token: str | None, expected_token: str) -> bool:
    """Return whether a callback request proves ownership with the verify token (R-1.3)."""
    if mode != "subscribe" or not expected_token or token is None:
        return False
    return hmac.compare_digest(token.encode("utf-8"), expected_token.encode("utf-8"))


def sender_hash(key: str, sender_id: str) -> str:
    """Return the 12-character keyed hash under which a sender id may be logged (R-1.2, R-9.12)."""
    if not key:
        raise ValueError("sender hash key is empty")
    return hmac.new(key.encode("utf-8"), sender_id.encode("utf-8"), hashlib.sha256).hexdigest()[:_HASH_LENGTH]
