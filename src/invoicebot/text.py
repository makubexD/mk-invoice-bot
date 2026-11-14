"""Characters untrusted text may not carry into mail, XML or drive paths (R-12.8)."""

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
