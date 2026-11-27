"""Updates a photo reading with the fields a matching invoice XML confirms (R-5.6, D202)."""

from __future__ import annotations

from dataclasses import replace

from invoicebot.extract.fields import AMOUNT_FIELDS, Flag, Reading
from invoicebot.extract.ubl import integer

#: The flag a read field gets when the XML disagrees with it (D202, S3.7).
CONFLICT: Flag = "conflict"


def confirmed_by_xml(reading: Reading, xml: Reading) -> Reading:
    """Return ``reading`` with each field the XML adds or agrees with taken from the XML (D202).

    A field the reading lacks or left empty is added; one whose value agrees is replaced
    by the XML's (source ``xml``, full confidence, no flag); one that differs keeps the
    read value, flagged ``conflict`` so the review set names it.
    """
    fields = dict(reading.fields)
    for name, field in xml.fields.items():
        if reading.value(name) is None or _agree(name, reading, xml):
            fields[name] = field
        else:
            fields[name] = replace(fields[name], flag=CONFLICT)
    return replace(reading, fields=fields)


def confirmed_names(before: Reading, after: Reading) -> tuple[str, ...]:
    """Return the names the XML confirmed or filled, in ``after`` order (D202, BR-F16).

    Those are the fields now sourced ``xml`` that were absent, empty or from another
    source in ``before``.
    """
    return tuple(
        name
        for name, field in after.fields.items()
        if field.source == "xml" and (before.value(name) is None or before.fields[name].source != "xml")
    )


def _agree(name: str, reading: Reading, xml: Reading) -> bool:
    """Return whether ``reading`` and ``xml`` hold the same value for ``name`` (D202)."""
    if name == "numero":
        number = integer(reading.value(name))
        return number is not None and number == integer(xml.value(name))
    if name in AMOUNT_FIELDS:
        amount = reading.amount(name)
        return amount is not None and amount == xml.amount(name)
    return reading.fields[name].value == xml.fields[name].value
