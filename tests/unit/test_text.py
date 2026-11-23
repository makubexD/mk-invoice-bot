"""Tests for the unsafe-character check, the formula escape and email normalization (R-12.8, R-6.6, D138)."""

from __future__ import annotations

import pytest

from invoicebot.text import (
    FORMULA_PREFIXES,
    escape_formula,
    has_unsafe_char,
    normalize_email,
    replace_unsafe_chars,
)


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


@pytest.mark.parametrize("prefix", FORMULA_PREFIXES)
def test_escape_formula_prefixes_each_formula_lead(prefix: str) -> None:
    """R-6.6: a value starting with a formula character gets a leading apostrophe."""
    assert escape_formula(prefix + "1") == "'" + prefix + "1"


def test_escape_formula_keeps_a_hyperlink_as_text() -> None:
    """R-6.6: a business name like =HYPERLINK(...) is stored as text."""
    assert escape_formula('=HYPERLINK("x")') == '\'=HYPERLINK("x")'


@pytest.mark.parametrize("value", [" =1+1", "＝1", "＋1"])
def test_escape_formula_escapes_a_whitespace_lead_and_full_width_prefixes(value: str) -> None:
    """R-6.6, R-7.10: a formula lead after whitespace or in full-width form gets the apostrophe."""
    assert escape_formula(value) == "'" + value


@pytest.mark.parametrize("value", ["La Leña", "abc", ""])
def test_escape_formula_leaves_plain_text(value: str) -> None:
    """R-6.6, R-7.10: plain text and an empty string are unchanged."""
    assert escape_formula(value) == value


@pytest.mark.parametrize(
    ("address", "normalized"),
    [
        pytest.param("Owner@Example.COM", "owner@example.com", id="case"),
        pytest.param("  owner@example.com  ", "owner@example.com", id="surrounding-spaces"),
        pytest.param("Straße@Example.com", "strasse@example.com", id="sharp-s-casefolds-to-ss"),
    ],
)
def test_normalize_email_returns_the_compared_form(address: str, normalized: str) -> None:
    """D138: a mailbox normalizes to the case-folded, stripped form mailbox pins are compared in."""
    assert normalize_email(address) == normalized
