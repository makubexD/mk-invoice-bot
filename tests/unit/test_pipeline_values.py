"""Tests for the template values one reading produces (R-6.2, R-4.2)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from invoicebot.extract.fields import Field, Reading
from invoicebot.pipeline.values import invoice_values

# Both values come from tests/synthetic_rucs.txt.
SYNTHETIC_RUC = "20000000001"
FULL_CONFIDENCE = 1.0

PHOTO_FIELDS: dict[str, str] = {
    "ruc_emisor": SYNTHETIC_RUC,
    "tipo": "01",
    "serie": "F014",
    "numero": "15160",
    "fecha_emision": "2026-11-13",
    "razon_social": "RESTAURANTE EJEMPLO S.A.C.",
    "base": "40.90",
    "igv": "7.36",
    "service_charge": "1.64",
    "total": "49.90",
}


def _reading(**overrides: str | None) -> Reading:
    """Return a reading with the synthetic photo's fields, applying ``overrides``."""
    values = {**PHOTO_FIELDS, **overrides}
    fields: dict[str, Field] = {}
    for name, value in values.items():
        if value is not None:
            fields[name] = Field(value, "qr", FULL_CONFIDENCE)
    return Reading(fields=fields, document_url=None)


def test_invoice_values_carries_the_synthetic_reading() -> None:
    """R-6.2: a synthetic photo reading yields every template value."""
    values = invoice_values(_reading(), None, None)

    assert values["fecha"] == date(2026, 11, 13)
    assert values["tipo"] == "01"
    assert values["tipo_desc"] == "Factura"
    assert values["serie"] == "F014"
    assert values["numero"] == "15160"
    assert values["ruc_emisor"] == SYNTHETIC_RUC
    assert values["razon_social"] == "RESTAURANTE EJEMPLO S.A.C."
    assert values["base"] == Decimal("40.90")
    assert values["igv"] == Decimal("7.36")
    assert values["service_charge"] == Decimal("1.64")
    assert values["total"] == Decimal("49.90")


def test_invoice_values_fills_a_field_the_photo_lacks_from_the_xml() -> None:
    """R-6.2: the XML fills the field the photo did not read."""
    photo = _reading(razon_social=None)
    xml = _reading(razon_social="COMERCIAL EJEMPLO S.A.C.")

    values = invoice_values(photo, xml, None)

    assert values["razon_social"] == "COMERCIAL EJEMPLO S.A.C."
    assert values["serie"] == "F014"


def test_invoice_values_names_an_accepted_comprobante() -> None:
    """R-4.2: estado_cp 1 reads as Aceptado."""
    status: dict[str, object] = {"estado_cp": 1, "estado_ruc": "00", "cond_domi_ruc": "00"}

    assert invoice_values(_reading(), None, status)["estado_sunat"] == "Aceptado"


def test_invoice_values_without_a_sunat_status_leaves_estado_sunat_empty() -> None:
    """R-4.2: no stored status or no SUNAT answer leaves estado_sunat empty."""
    without_status = invoice_values(_reading(), None, None)
    without_answer = invoice_values(_reading(), None, {"estado_cp": None})

    assert without_status["estado_sunat"] == ""
    assert without_answer["estado_sunat"] == ""
