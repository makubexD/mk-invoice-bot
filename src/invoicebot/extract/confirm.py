"""Updates a photo reading with the fields a matching invoice XML confirms (R-5.6, D202)."""

from __future__ import annotations

from dataclasses import replace

from invoicebot.extract.fields import AMOUNT_FIELDS, Reading
from invoicebot.extract.ubl import _integer


def confirmed_by_xml(reading: Reading, xml: Reading) -> Reading:
    """Return ``reading`` with each field the XML adds or agrees with taken from the XML (D202).

    A field the reading lacks is added; one whose value agrees is replaced by the XML's
    (source ``xml``, full confidence, no flag); one that differs stays as read.
    """
    fields = dict(reading.fields)
    for name, field in xml.fields.items():
        current = fields.get(name)
        if current is None or _agree(name, reading, xml):
            fields[name] = field
    return replace(reading, fields=fields)


def confirmed_names(before: Reading, after: Reading) -> tuple[str, ...]:
    """Return the names whose field changed to source ``xml`` from another source, in ``after`` order."""
    return tuple(
        name
        for name, field in after.fields.items()
        if field.source == "xml" and name in before.fields and before.fields[name].source != "xml"
    )


def _agree(name: str, reading: Reading, xml: Reading) -> bool:
    """Return whether ``reading`` and ``xml`` hold the same value for ``name`` (D202)."""
    if name == "numero":
        number = _integer(reading.value(name))
        return number is not None and number == _integer(xml.value(name))
    if name in AMOUNT_FIELDS:
        amount = reading.amount(name)
        return amount is not None and amount == xml.amount(name)
    return reading.fields[name].value == xml.fields[name].value
