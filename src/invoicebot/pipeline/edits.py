"""Parse the ``campo=valor`` edits a sender types in chat (R-3.5, D97).

An edit line names one invoice field and its new value. Aliases shorten the field's
name, ``motivo``/``descripcion`` stay with :mod:`invoicebot.pipeline.overrides`, and
each value is normalised here so the caller stores a canonical reading field. A value
the field's rule rejects becomes an :class:`EditError` that names the business rule
when there is one; the caller answers with the first error and changes nothing.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType

from invoicebot.extract.validate import RULE_BAD_DATE, RULE_BAD_RUC, check_serie, is_valid_ruc
from invoicebot.pipeline.overrides import MAX_OVERRIDE_CHARS, OVERRIDE_KEYS

#: The most edit lines one message may carry; later lines each get an error.
MAX_EDIT_LINES = 10
#: Short names a sender may use for a field (R-3.5, D97).
ALIASES: Mapping[str, str] = {
    "ruc": "ruc_emisor",
    "fecha": "fecha_emision",
    "cliente": "num_doc_adq",
}
#: The document type implied by a serie's first letter (R-3.5, D97).
TIPO_BY_SERIE_PREFIX: Mapping[str, str] = {"F": "01", "B": "03"}

_DATE_PATTERN = re.compile(r"([0-9]{2})[/-]([0-9]{2})[/-]([0-9]{4})")
_TOTAL_PATTERN = re.compile(r"[0-9]+(?:[.,][0-9]{1,2})?")

_Validator = Callable[[str], tuple[str | None, str | None]]


@dataclass(frozen=True)
class EditError:
    """One edit line the parse rejected, with the rule and field it concerns (R-3.5)."""

    rule_id: str | None
    line: str
    field: str | None
    value: str


@dataclass(frozen=True)
class Edits:
    """The canonical fields and the rejected lines of one edit message (R-3.5, D97)."""

    fields: Mapping[str, str]
    errors: tuple[EditError, ...]


def parse_edits(text: str) -> Edits:
    """Return the fields and errors of the ``campo=valor`` lines in ``text`` (R-3.5, D97).

    A line without ``=`` is ignored. Keys are case-insensitive and accent-free after
    ``strip``; ``motivo`` and ``descripcion`` are skipped. Lines past :data:`MAX_EDIT_LINES`
    and values longer than :data:`MAX_OVERRIDE_CHARS` are errors. A serie without a
    well-formed ``tipo`` gets one from its first letter.
    """
    fields: dict[str, str] = {}
    errors: list[EditError] = []
    assignments = 0
    for raw in text.splitlines():
        if "=" not in raw:
            continue
        assignments += 1
        line = raw.strip()
        key, _, raw_value = line.partition("=")
        name = _canonical_key(key)
        value = raw_value.strip()
        if name in OVERRIDE_KEYS:
            continue
        if assignments > MAX_EDIT_LINES or len(value) > MAX_OVERRIDE_CHARS:
            errors.append(EditError(None, line, name, value))
        elif name is None:
            errors.append(EditError(None, line, None, value))
        else:
            _apply(fields, errors, name, line, value)
    _infer_tipo(fields)
    return Edits(MappingProxyType(fields), tuple(errors))


def _apply(
    fields: dict[str, str],
    errors: list[EditError],
    name: str,
    line: str,
    value: str,
) -> None:
    """Validate one line's value and store it, or record its error (R-3.5)."""
    normalized, rule_id = _VALIDATORS[name](value)
    if normalized is None:
        errors.append(EditError(rule_id, line, name, value))
    else:
        fields[name] = normalized


def _infer_tipo(fields: dict[str, str]) -> None:
    """Add ``tipo`` from the serie's first letter when the sender did not give one (D97)."""
    serie = fields.get("serie")
    if serie is None or "tipo" in fields:
        return
    tipo = TIPO_BY_SERIE_PREFIX.get(serie[0])
    if tipo is not None:
        fields["tipo"] = tipo


def _canonical_key(key: str) -> str | None:
    """Return the canonical field name for ``key``, or None when the key is unknown (D97)."""
    name = _without_accents(key.strip().lower())
    if name in OVERRIDE_KEYS or name in _VALIDATORS:
        return name
    return ALIASES.get(name)


def _without_accents(text: str) -> str:
    """Return ``text`` in NFKD with the combining marks removed (D97)."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _ruc(value: str) -> tuple[str | None, str | None]:
    """Return the RUC, or BR-E02 when its check digit fails (BR-E02)."""
    if not is_valid_ruc(value):
        return None, RULE_BAD_RUC
    return value, None


def _num_doc(value: str) -> tuple[str | None, str | None]:
    """Return the buyer document, accepted at 8 or 11 digits (R-3.5)."""
    if len(value) in (8, 11) and value.isascii() and value.isdigit():
        return value, None
    return None, None


def _fecha(value: str) -> tuple[str | None, str | None]:
    """Return the date in ISO form, or BR-E03 when it is malformed or impossible (BR-E03)."""
    match = _DATE_PATTERN.fullmatch(value)
    if match is None:
        return None, RULE_BAD_DATE
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day).isoformat(), None
    except ValueError:
        return None, RULE_BAD_DATE


def _total(value: str) -> tuple[str | None, str | None]:
    """Return the amount with a dot, at most two decimals, or an error (R-3.5)."""
    if _TOTAL_PATTERN.fullmatch(value) is None:
        return None, None
    return value.replace(",", "."), None


def _serie(value: str) -> tuple[str | None, str | None]:
    """Return the serie upper-cased, or an error when it does not match (R-2.4)."""
    upper = value.upper()
    if not check_serie(upper):
        return None, None
    return upper, None


def _numero(value: str) -> tuple[str | None, str | None]:
    """Return the 1-8 digit number without leading zeros, or an error (R-3.5)."""
    if not 1 <= len(value) <= 8 or not value.isascii() or not value.isdigit():
        return None, None
    return value.lstrip("0") or "0", None


def _tipo(value: str) -> tuple[str | None, str | None]:
    """Return the document type, accepted only as 01 or 03 (R-3.5)."""
    if value in ("01", "03"):
        return value, None
    return None, None


_VALIDATORS: Mapping[str, _Validator] = {
    "ruc_emisor": _ruc,
    "num_doc_adq": _num_doc,
    "fecha_emision": _fecha,
    "total": _total,
    "serie": _serie,
    "numero": _numero,
    "tipo": _tipo,
}
