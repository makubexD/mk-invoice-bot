"""Tests for the channel-neutral sender hash (R-1.2, R-9.12)."""

from __future__ import annotations

import hashlib
import hmac
import re

import pytest

from invoicebot.channels.sender_hash import sender_hash

HASH_KEY = "synthetic-hash-key"


def test_sender_hash_is_short_stable_and_hides_the_id() -> None:
    """R-1.2, R-9.12: a sender id is logged only as a stable 12-character hash."""
    digest = sender_hash(HASH_KEY, "51999000001")
    assert re.fullmatch(r"[0-9a-f]{12}", digest) is not None
    assert digest == sender_hash(HASH_KEY, "51999000001")
    assert "51999000001" not in digest


def test_sender_hash_is_the_truncated_hmac_digest() -> None:
    """R-1.2, R-9.12: the digest is the first 12 hex characters of the HMAC-SHA256."""
    expected = hmac.new(HASH_KEY.encode(), b"51999000001", hashlib.sha256).hexdigest()[:12]
    assert sender_hash(HASH_KEY, "51999000001") == expected


def test_sender_hash_differs_from_the_unkeyed_digest() -> None:
    """R-1.2, R-9.12: the keyed digest is not the brute-forceable unkeyed SHA-256."""
    assert sender_hash(HASH_KEY, "51999000001") != hashlib.sha256(b"51999000001").hexdigest()[:12]


def test_sender_hash_under_two_keys_differs() -> None:
    """R-1.2, R-9.12: the same sender id hashes differently under two different keys."""
    assert sender_hash(HASH_KEY, "51999000001") != sender_hash("another-hash-key", "51999000001")


def test_sender_hash_rejects_an_empty_key() -> None:
    """R-1.2, R-9.12: an empty key raises ValueError instead of producing an unkeyed digest."""
    with pytest.raises(ValueError, match="empty"):
        sender_hash("", "51999000001")
