"""The digest a gap stores to tell whether its config entry changed (D157)."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel


def config_digest(model: BaseModel) -> str:
    """Return the SHA-256 hex of ``model``'s JSON dump, with sorted keys (D157).

    Sorting the keys makes the digest depend on the entry's values, not on the
    order its fields or mappings happen to be written in.
    """
    payload = json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
