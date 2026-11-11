"""Tests for WhatsApp webhook signature, handshake and sender hash (R-1.2, R-1.3)."""

from __future__ import annotations

import hashlib
import hmac
import re

import pytest

from invoicebot.channels.whatsapp.signature import (
    sender_hash,
    verify_handshake,
    verify_signature,
)

APP_KEY = "synthetic-app-key"
HANDSHAKE = "synthetic-verify-token"
BODY = b'{"a":1}'


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_valid_signature_is_accepted() -> None:
    """R-1.3: a correct sha256 signature over the raw body verifies."""
    assert verify_signature(APP_KEY, BODY, _sign(APP_KEY, BODY)) is True


def test_changed_body_byte_is_rejected() -> None:
    """R-1.3: changing one body byte invalidates the signature."""
    assert verify_signature(APP_KEY, b'{"a":2}', _sign(APP_KEY, BODY)) is False


@pytest.mark.parametrize("header", [None, "", "sha1=abc", "sha256=zz"])
def test_malformed_header_is_rejected(header: str | None) -> None:
    """R-1.3: a missing, wrong-algorithm or non-hex signature header never verifies."""
    assert verify_signature(APP_KEY, BODY, header) is False


def test_non_ascii_header_is_rejected_without_raising() -> None:
    """R-1.3: a non-hex non-ASCII signature header returns False instead of raising."""
    assert verify_signature(APP_KEY, BODY, "sha256=contraseña") is False


def test_empty_secret_is_rejected() -> None:
    """R-1.3: an empty app secret cannot verify even a digest computed with itself."""
    assert verify_signature("", BODY, _sign("", BODY)) is False


def test_valid_handshake_is_accepted() -> None:
    """R-1.3: subscribe mode with the configured verify token succeeds."""
    assert verify_handshake("subscribe", HANDSHAKE, HANDSHAKE) is True


@pytest.mark.parametrize(
    ("mode", "token", "expected_token"),
    [
        ("subscribe", "wrong-token", HANDSHAKE),
        (None, HANDSHAKE, HANDSHAKE),
        ("unsubscribe", HANDSHAKE, HANDSHAKE),
        ("subscribe", HANDSHAKE, ""),
    ],
)
def test_invalid_handshake_is_rejected(mode: str | None, token: str | None, expected_token: str) -> None:
    """R-1.3: a wrong token, wrong mode or empty expected token never verifies."""
    assert verify_handshake(mode, token, expected_token) is False


def test_non_ascii_handshake_token_does_not_raise() -> None:
    """R-1.3: a non-ASCII token is compared as bytes and simply fails."""
    assert verify_handshake("subscribe", "contraseña", "otra") is False


def test_sender_hash_is_short_stable_and_hides_the_id() -> None:
    """R-1.2: a sender id is logged only as a stable 12-character hash."""
    digest = sender_hash("51999000001")
    assert re.fullmatch(r"[0-9a-f]{12}", digest) is not None
    assert digest == sender_hash("51999000001")
    assert "51999000001" not in digest
