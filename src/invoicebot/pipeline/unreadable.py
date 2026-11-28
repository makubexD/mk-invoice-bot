"""BR-E01's text for an unreadable or partly read invoice, and the keys a sender types (BR-E01, D188)."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import date
from typing import Final

from invoicebot.extract.fields import REQUIRED, Reading
from invoicebot.rules.messages_es import (
    AMOUNT_SHOWN,
    FIELD_DESC,
    MESSAGES,
    SUMMARY_DATE_FORMAT,
    TIPO_DESC,
    UNREADABLE_DOUBTFUL_LINE,
    UNREADABLE_PARTIAL,
    fields_desc,
)

#: No QR, an unreadable photo or a required field missing (BR-E01).
RULE_UNREADABLE: Final = "BR-E01"
#: Short names a sender may use for a field (R-3.5, D97).
ALIASES: Mapping[str, str] = {
    "ruc": "ruc_emisor",
    "fecha": "fecha_emision",
    "cliente": "num_doc_adq",
}
#: The example value BR-E01's ``{missing_lines}`` shows for a missing field (D188).
_MISSING_EXAMPLES: Mapping[str, str] = {"fecha_emision": "dd/mm/aaaa"}


def edit_key(field: str) -> str:
    """Return the key a sender types for ``field`` (D188).

    The key is the field's alias in :data:`ALIASES`, so it round-trips through
    :func:`_canonical_key`; a field with no alias is typed by its own name.
    """
    for alias, name in ALIASES.items():
        if name == field:
            return alias
    return field


def unreadable_text(invoice_id: int, reading: Reading | None, doubtful: Collection[str] = ()) -> str:
    """Return BR-E01's text for an invoice whose reading lacks required fields (BR-E01, D188).

    An invoice with no stored reading, or one that read none of the required fields, gets
    the generic text. Otherwise the text lists the required fields that were read, with
    their values, and one ``key=value`` line to type for each one still missing. A read
    field named in ``doubtful`` leaves the read line for a doubtful line of its own (D212).
    """
    if reading is None:
        return MESSAGES[RULE_UNREADABLE]
    kept = tuple(name for name in REQUIRED if reading.value(name) is not None)
    if not kept:
        return MESSAGES[RULE_UNREADABLE]
    read = tuple(name for name in kept if name not in doubtful)
    unsure = tuple(name for name in kept if name in doubtful)
    missing = reading.missing_required()
    return UNREADABLE_PARTIAL.format(
        id=invoice_id,
        read_desc=_described(reading, read),
        doubtful_line=UNREADABLE_DOUBTFUL_LINE.format(doubtful_desc=_described(reading, unsure))
        if unsure
        else "",
        missing_desc=fields_desc(missing),
        missing_lines="\n".join(f"{edit_key(name)}={_MISSING_EXAMPLES.get(name, '…')}" for name in missing),
    )


def _described(reading: Reading, names: tuple[str, ...]) -> str:
    """Return each field's name and shown value, ``; `` separated (D188)."""
    return "; ".join(f"{FIELD_DESC[name]} {_shown(reading, name)}" for name in names)


def _shown(reading: Reading, name: str) -> str:
    """Return a required field the reading kept as BR-E01 shows it (D188)."""
    value = reading.value(name) or ""
    if name == "total":
        return AMOUNT_SHOWN.format(total=value)
    if name == "tipo":
        return TIPO_DESC.get(value, value)
    if name == "fecha_emision":
        return _fecha_shown(value)
    return value


def _fecha_shown(value: str) -> str:
    """Return an ISO date as ``dd/mm/aaaa``, or ``value`` when it is not one (D188)."""
    try:
        return date.fromisoformat(value).strftime(SUMMARY_DATE_FORMAT)
    except ValueError:
        return value
