"""Unit tests for BR-E01's unreadable texts and the sender's edit keys (BR-E01, D188)."""

import pytest

from invoicebot.extract.fields import FULL_CONFIDENCE, REQUIRED, Field, Reading
from invoicebot.pipeline.edits import _canonical_key
from invoicebot.pipeline.unreadable import edit_key, unreadable_text

SYNTHETIC_RUC = "20000000001"

GENERIC_TEXT = (
    "No pude leer este comprobante. Envíame una foto más cerca y con buena luz, "
    "o escríbeme los datos, uno por línea:\n"
    "ruc=…\n"
    "serie=F001\n"
    "numero=123\n"
    "fecha=dd/mm/aaaa\n"
    "total=…"
)


def _partial_reading() -> Reading:
    """Return a reading with every required field but ``fecha_emision`` and ``total``."""
    return Reading(
        fields={
            "ruc_emisor": Field(SYNTHETIC_RUC, "qr", FULL_CONFIDENCE),
            "tipo": Field("01", "qr", FULL_CONFIDENCE),
            "serie": Field("F001", "qr", FULL_CONFIDENCE),
            "numero": Field("123", "qr", FULL_CONFIDENCE),
        },
        document_url=None,
    )


def test_unreadable_text_without_a_reading_returns_the_generic_text() -> None:
    """D188, BR-E01: an invoice with no stored reading gets the nothing-read text."""
    assert unreadable_text(14, None) == GENERIC_TEXT


def test_unreadable_text_with_a_partial_reading_names_read_and_missing_fields() -> None:
    """D188, BR-E01: the text lists the fields read with their values and the missing ones."""
    assert unreadable_text(14, _partial_reading()) == (
        "Leí parte del comprobante #14.\n"
        f"✅ Leído: RUC del emisor {SYNTHETIC_RUC}; tipo de comprobante Factura; "
        "serie F001; número 123\n"
        "❓ Falta: fecha, total\n"
        "Envíame otra foto más cerca y con buena luz, o escríbeme solo lo que falta, "
        "un dato por línea:\n"
        "fecha=dd/mm/aaaa\n"
        "total=…"
    )


def test_unreadable_text_with_only_a_ruc_lists_every_missing_line() -> None:
    """D188, BR-E01: a reading with only the RUC gets one line for each of the five missing fields."""
    reading = Reading(
        fields={"ruc_emisor": Field(SYNTHETIC_RUC, "qr", FULL_CONFIDENCE)},
        document_url=None,
    )
    assert unreadable_text(14, reading) == (
        "Leí parte del comprobante #14.\n"
        f"✅ Leído: RUC del emisor {SYNTHETIC_RUC}\n"
        "❓ Falta: tipo de comprobante, serie, número, fecha, total\n"
        "Envíame otra foto más cerca y con buena luz, o escríbeme solo lo que falta, "
        "un dato por línea:\n"
        "tipo=…\n"
        "serie=…\n"
        "numero=…\n"
        "fecha=dd/mm/aaaa\n"
        "total=…"
    )


@pytest.mark.parametrize("field", REQUIRED)
def test_edit_key_round_trips_through_the_canonical_key(field: str) -> None:
    """D188, BR-E01: the key edit_key gives for a required field parses back to that field."""
    assert _canonical_key(edit_key(field)) == field
