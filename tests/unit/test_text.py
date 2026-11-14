"""Tests for the unsafe-character check (R-12.8)."""

from __future__ import annotations

import pytest

from invoicebot.text import has_unsafe_char, replace_unsafe_chars


@pytest.mark.parametrize(
    ("value", "cleaned"),
    [
        pytest.param("ACME\u202eSAC", "ACME SAC", id="bidi-override"),
        pytest.param("a\u200bb", "a b", id="zero-width-space"),
        pytest.param("a\u2028b", "a b", id="line-separator"),
        pytest.param("a\u2029b", "a b", id="paragraph-separator"),
        pytest.param("a\ud800b", "a b", id="surrogate"),
    ],
)
def test_unsafe_character_is_detected_and_replaced(value: str, cleaned: str) -> None:
    """R-12.8: text with an unsafe character is flagged and gets a space in its place."""
    assert has_unsafe_char(value) is True
    assert replace_unsafe_chars(value) == cleaned


def test_safe_text_is_accepted_and_unchanged() -> None:
    """R-12.8: text with no unsafe character is accepted and left as it is."""
    assert has_unsafe_char("Pollería Ñandú") is False
    assert replace_unsafe_chars("Pollería Ñandú") == "Pollería Ñandú"


def test_replace_unsafe_chars_strips_the_edges() -> None:
    """R-12.8: a replaced character at either edge is stripped rather than left as a space."""
    assert replace_unsafe_chars(" \x00x\x07 ") == "x"
