"""The template values one invoice's reading produces (R-6.2, R-7.4).

:func:`invoice_values` turns a photo's reading and its invoice XML into the names a flow's
sinks and messages render. The photo wins; the XML fills the fields the photo lacks. The
values carry no I/O: the caller passes the stored ``sunat_status_json`` object (R-4.2).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from invoicebot.extract.fields import Reading
from invoicebot.rules.messages_es import TIPO_DESC

#: The SUNAT ``estadoCp`` codes and the words the templates show for them (R-4.2).
ESTADO_SUNAT_DESC: Mapping[int, str] = {
    0: "No existe",
    1: "Aceptado",
    2: "Anulado",
    3: "Autorizado",
    4: "No autorizado",
}


def invoice_values(
    reading: Reading,
    xml_reading: Reading | None,
    sunat_status: Mapping[str, object] | None,
) -> dict[str, object]:
    """Return the values a flow's sinks and messages render (R-6.2, R-7.4).

    ``xml_reading`` fills any field the photo lacks, and carries the amounts when present.
    ``sunat_status`` is the stored ``sunat_status_json`` object, or ``None`` when SUNAT was
    never asked (R-4.2).
    """
    amounts = reading if xml_reading is None else xml_reading
    tipo = _text(reading, xml_reading, "tipo")
    return {
        "fecha": _fecha(reading, xml_reading),
        "tipo": tipo,
        "tipo_desc": TIPO_DESC.get(tipo or "", tipo or ""),
        "serie": _text(reading, xml_reading, "serie"),
        "numero": _text(reading, xml_reading, "numero"),
        "ruc_emisor": _text(reading, xml_reading, "ruc_emisor"),
        "razon_social": _text(reading, xml_reading, "razon_social"),
        "base": amounts.amount("base"),
        "igv": amounts.amount("igv"),
        "service_charge": amounts.amount("service_charge"),
        "total": amounts.amount("total"),
        "estado_sunat": _estado_sunat(sunat_status),
    }


def _text(reading: Reading, xml: Reading | None, name: str) -> str | None:
    """Return ``name`` from the photo's reading, falling back to the XML (R-7.4)."""
    value = reading.value(name)
    return value if value is not None or xml is None else xml.value(name)


def _fecha(reading: Reading, xml: Reading | None) -> date | None:
    """Return the issue date from the reading, falling back to the XML (R-7.1)."""
    fecha = reading.fecha()
    return xml.fecha() if fecha is None and xml is not None else fecha


def _estado_sunat(sunat_status: Mapping[str, object] | None) -> str:
    """Return the SUNAT state in words, or "" when SUNAT gave no answer (R-4.2)."""
    code = None if sunat_status is None else sunat_status.get("estado_cp")
    return ESTADO_SUNAT_DESC.get(code, "") if isinstance(code, int) else ""
