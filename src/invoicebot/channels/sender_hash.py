"""The keyed hash every channel logs a sender as (R-9.16)."""

from __future__ import annotations

import hashlib
import hmac

_HASH_LENGTH = 12


def sender_hash(key: str, sender_id: str) -> str:
    """Return the 12-character keyed hash under which a sender id may be logged (R-1.2, R-9.12)."""
    if not key:
        raise ValueError("sender hash key is empty")
    return hmac.new(key.encode("utf-8"), sender_id.encode("utf-8"), hashlib.sha256).hexdigest()[:_HASH_LENGTH]
