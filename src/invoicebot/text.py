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
#: The full-width forms are what a spreadsheet recognises when a cell is not plain ASCII.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "＝", "＋", "－", "＠")


def escape_formula(value: str) -> str:
    """Return ``value`` with a leading apostrophe when a spreadsheet would read a formula (R-6.6, R-7.10).

    Leading whitespace does not stop a spreadsheet reading the cell as a formula, so the
    check runs on the stripped value; the apostrophe goes before the original text.
    """
    if value.startswith(FORMULA_PREFIXES) or value.lstrip().startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def normalize_email(address: str) -> str:
    """Return the form mailbox pins are compared in (D138, D164)."""
    return address.strip().casefold()
