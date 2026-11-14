"""The comercio an invoice belongs to and the folder numbers it reads back (spec 03 §7).

One job: pick the comercio for one invoice and turn the folder template into the regex that finds
the largest sequence number already used in the delivery folder. Both are pure: no I/O, no clock.
"""

from __future__ import annotations

import re
import string
import unicodedata
from collections.abc import Iterable

from invoicebot.config.models import Store
from invoicebot.sinks.expr import ExprError


def comercio_for(store: Store | None, serie: str, razon_social: str) -> str | None:
    """Return the comercio for an invoice, or ``None`` when it cannot be told (R-7.1, R-5.8).

    A known store resolves by the brand whose ``serie_prefix`` prefixes ``serie``, or by its only
    brand; without a store the first two words of ``razon_social`` become an ASCII CamelCase slug.
    """
    if store is None:
        return _camel_slug(razon_social)
    for brand in store.brands:
        if any(serie.startswith(prefix) for prefix in brand.serie_prefix):
            return brand.comercio
    if len(store.brands) == 1:
        return store.brands[0].comercio
    return None


def seq_pattern(folder_template: str, year: int, month: int) -> re.Pattern[str]:
    """Return the pattern that reads ``seq`` out of folder names for ``year`` and ``month`` (R-7.1).

    ``{fecha:%Y}`` and ``{fecha:%m}`` become that year and month (the month's leading zero is
    optional), ``{seq}`` captures digits and every other field matches lazily. A trailing
    ``_<status>`` suffix is allowed (R-7.11).

    Raises:
        ExprError: the template has no ``{seq}`` field, so no number can be read back.
    """
    parts: list[str] = []
    has_seq = False
    for literal, field_name, format_spec, _conversion in string.Formatter().parse(folder_template):
        parts.append(re.escape(literal))
        if field_name is None:
            continue
        if field_name == "seq":
            parts.append(r"(\d+)")
            has_seq = True
        elif field_name == "fecha" and format_spec == "%Y":
            parts.append(re.escape(str(year)))
        elif field_name == "fecha" and format_spec == "%m":
            parts.append(f"0?{month}")
        else:
            parts.append(".+?")
    if not has_seq:
        raise ExprError(f"folder template has no {{seq}} field: {folder_template!r}")
    return re.compile("^" + "".join(parts) + r"(?:_.*)?$")


def max_seq(names: Iterable[str], pattern: re.Pattern[str]) -> int:
    """Return the largest ``seq`` among ``names`` matching ``pattern``, or 0 (R-7.1)."""
    largest = 0
    for name in names:
        match = pattern.match(name)
        if match is None:
            continue
        number = match.group(1)
        if number is not None:
            largest = max(largest, int(number))
    return largest


def _camel_slug(razon_social: str) -> str | None:
    """Return the ASCII CamelCase of the first two words, or ``None`` if there are none (R-7.1)."""
    ascii_name = unicodedata.normalize("NFKD", razon_social).encode("ascii", "ignore").decode("ascii")
    words = re.findall(r"[A-Za-z0-9]+", ascii_name)
    if not words:
        return None
    return "".join(word[:1].upper() + word[1:].lower() for word in words[:2])
