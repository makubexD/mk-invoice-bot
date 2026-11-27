"""Tests for the routed summary's issuer and source lines (D188)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import get_args

from invoicebot.config.checks import ISSUER_NAME_MAX
from invoicebot.config.models import Extraction, Ocr
from invoicebot.extract.fields import FULL_CONFIDENCE, Field, Flag, Reading, Source
from invoicebot.pipeline.summary import issuer_desc, review_fields, summary_text
from invoicebot.rules.messages_es import SOURCE_DESC

# From tests/synthetic_rucs.txt; no real RUC may appear here.
SYNTHETIC_RUC = "20000000001"
TODAY = date(2026, 11, 13)
FLOW_LINE = "➡️ Trabajo (Microsoft)"
REQUIRED_VALUES: dict[str, str] = {
    "ruc_emisor": SYNTHETIC_RUC,
    "tipo": "01",
    "serie": "F001",
    "numero": "123",
    "fecha_emision": TODAY.isoformat(),
    "total": "30.90",
}


def _extraction() -> Extraction:
    """Return an extraction config under which the synthetic reading raises no warning."""
    return Extraction(
        max_image_px=2000,
        ocr=Ocr(engine="tesseract", lang="spa", psm=4, timeout_s=5),
        review_threshold=0.8,
        max_invoice_age_days=365,
        igv_rates=[Decimal("0.18")],
        supported_tipos=["01", "03"],
        helper_timeout_s=10,
    )


def _reading(
    *,
    razon_social: str | None = None,
    sources: Mapping[str, Source] | None = None,
    missing: Sequence[str] = (),
    flags: Mapping[str, Flag] | None = None,
    overrides: Mapping[str, Field] | None = None,
) -> Reading:
    """Return a reading of every required field, from ``sources`` unless ``overrides`` says otherwise."""
    chosen = dict(sources or {})
    field_flags = dict(flags or {})
    fields = {
        name: Field(value, chosen.get(name, "qr"), FULL_CONFIDENCE, field_flags.get(name))
        for name, value in REQUIRED_VALUES.items()
        if name not in missing
    }
    fields.update(overrides or {})
    if razon_social is not None:
        fields["razon_social"] = Field(razon_social, "padron", FULL_CONFIDENCE)
    return Reading(fields=fields, document_url=None)


def _summary(reading: Reading) -> str:
    """Return the formatted routed summary for ``reading``."""
    return summary_text(reading, FLOW_LINE, TODAY, _extraction())


def test_every_source_has_a_description() -> None:
    """D188: SOURCE_DESC names every field source, so the line never prints a raw key."""
    assert set(SOURCE_DESC) == set(get_args(Source))


def test_summary_names_the_issuer_from_the_padron() -> None:
    """D188: a reading with a razón social names it with the RUC and the QR source."""
    lines = _summary(_reading(razon_social="ACME S.A.C.")).splitlines()

    assert lines[1] == f"Emisor: ACME S.A.C. (RUC {SYNTHETIC_RUC})"
    assert lines[3] == "🔎 Leído de: QR"


def test_summary_without_razon_social_names_only_the_ruc() -> None:
    """D188: a reading with no razón social names the issuer by its RUC alone."""
    lines = _summary(_reading()).splitlines()

    assert lines[1] == f"Emisor: RUC {SYNTHETIC_RUC}"


def test_summary_names_each_source_that_read_a_required_field() -> None:
    """D188: QR holding most fields comes first; OCR follows with the fields it read."""
    reading = _reading(sources={"fecha_emision": "ocr", "total": "ocr"})

    assert _summary(reading).splitlines()[3] == "🔎 Leído de: QR; texto: fecha, total"


def test_sources_tie_breaks_on_the_required_order() -> None:
    """D188: with a tie, the source whose first field comes earliest in REQUIRED leads."""
    reading = _reading(
        sources={
            "ruc_emisor": "qr",
            "tipo": "qr",
            "serie": "qr",
            "numero": "ocr",
            "fecha_emision": "ocr",
            "total": "ocr",
        }
    )

    assert _summary(reading).splitlines()[3] == "🔎 Leído de: QR; texto: número, fecha, total"


def test_source_with_most_fields_is_named_alone() -> None:
    """D188: the leading source is named alone even when another source's first field is earlier."""
    reading = _reading(
        sources={
            "ruc_emisor": "qr",
            "tipo": "ocr",
            "serie": "ocr",
            "numero": "ocr",
            "fecha_emision": "ocr",
            "total": "ocr",
        }
    )

    assert _summary(reading).splitlines()[3] == "🔎 Leído de: texto; QR: RUC del emisor"


def test_missing_required_fields_are_not_counted() -> None:
    """D188: a required field the reading lacks is neither named nor counted for its source."""
    reading = _reading(sources={"fecha_emision": "ocr"}, missing=("total",))

    assert _summary(reading).splitlines()[3] == "🔎 Leído de: QR; texto: fecha"


def test_inferred_and_review_lines_still_follow_the_flow_line() -> None:
    """D188, D79, D80: the source line is added without moving the inferred and review lines."""
    reading = _reading(
        flags={"tipo": "inferred"},
        overrides={"total": Field("30.90", "qr", 0.5)},
    )
    lines = _summary(reading).splitlines()

    assert lines[3] == "🔎 Leído de: QR"
    assert lines[4] == FLOW_LINE
    assert lines[5] == "ℹ️ Deducido del comprobante: tipo de comprobante"
    assert lines[6] == "⚠️ Revisa: total"


def test_issuer_desc_cuts_a_long_name() -> None:
    """D188: an issuer name longer than ISSUER_NAME_MAX is cut with an ellipsis, keeping the RUC."""
    reading = Reading(
        fields={
            "ruc_emisor": Field("20100000001", "xml", FULL_CONFIDENCE),
            "razon_social": Field("A" * 300, "xml", FULL_CONFIDENCE),
        },
        document_url=None,
    )

    assert issuer_desc(reading) == "A" * (ISSUER_NAME_MAX - 1) + "… (RUC 20100000001)"


def test_review_fields_empty_when_every_field_is_confident() -> None:
    """D203: a reading with every field at full confidence and today's date needs no review."""
    assert review_fields(_reading(), _extraction(), TODAY) == ()


def test_review_fields_names_a_low_confidence_field() -> None:
    """D203: a field below review_threshold is named."""
    reading = _reading(overrides={"serie": Field(REQUIRED_VALUES["serie"], "ocr", 0.43)})

    assert review_fields(reading, _extraction(), TODAY) == ("serie",)


def test_review_fields_names_a_missing_date() -> None:
    """D203: a reading without fecha_emision names the date."""
    reading = _reading(missing=("fecha_emision",))

    assert review_fields(reading, _extraction(), TODAY) == ("fecha_emision",)


def test_review_fields_names_an_old_date() -> None:
    """D203: a date older than max_invoice_age_days names the date."""
    old = (TODAY - timedelta(days=400)).isoformat()
    reading = _reading(overrides={"fecha_emision": Field(old, "qr", FULL_CONFIDENCE)})

    assert "fecha_emision" in review_fields(reading, _extraction(), TODAY)
