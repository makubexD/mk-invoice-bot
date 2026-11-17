"""What untrusted text may not carry into mail, XML, drive paths or spreadsheet cells (R-12.8, R-6.6)."""

import unicodedata

#: The Unicode categories untrusted text may not carry: controls, format characters
#: such as bidi overrides and zero-width, surrogates, and line and paragraph separators.
UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def has_unsafe_char(value: str) -> bool:
    """Return True when ``value`` carries a character from an unsafe category (R-12.8)."""
    return any(unicodedata.category(char) in UNSAFE_CATEGORIES for char in value)


def replace_unsafe_chars(value: str) -> str:
    """Replace every unsafe character with a space and strip the edges (R-12.8)."""
    return "".join(" " if unicodedata.category(char) in UNSAFE_CATEGORIES else char for char in value).strip()


#: Leading characters that make Excel or Sheets read a text cell as a formula (R-6.6, R-7.10).
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def escape_formula(value: str) -> str:
    """Return ``value`` with a leading apostrophe when a spreadsheet would read a formula (R-6.6)."""
    if value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value
