"""Tests for the probe's wire format (D193, R-11.12)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from invoicebot.config.models import MediaMaxBytes
from invoicebot.fetchers.base import FetchInvoice
from invoicebot.fetchers.browser.probe_protocol import (
    ProbedField,
    ProbeOutcome,
    ProbeReport,
    ProbeRequest,
)
from invoicebot.fetchers.browser.protocol import HelperFile


def _invoice() -> FetchInvoice:
    return FetchInvoice(
        ruc_emisor="20123456789",
        tipo="01",
        serie="F001",
        numero="123",
        fecha=date(2026, 11, 5),
        total=Decimal("118.00"),
    )


def _max_bytes() -> MediaMaxBytes:
    return MediaMaxBytes(image=10, pdf=20, xml=5, zip=50)


def _field() -> ProbedField:
    return ProbedField(selector="#txtSerie", kind="text", label="Serie")


def _full_report() -> ProbeReport:
    return ProbeReport(
        outcome=ProbeOutcome.OK,
        form_path="/consulta/comprobante",
        captcha="image",
        fields=(
            ProbedField(selector="#txtSerie", kind="text", label="N° Correlativo", value_name="serie"),
            ProbedField(
                selector="[name='ctl00$txt:Serie']",
                kind="select",
                label="Fecha de Emisión",
                value_name="fecha",
                options=("Seleccione", "FACTURA ELECTRONICA"),
            ),
        ),
        submit="#btnBuscar",
        pdf="#lnkPdf",
        xml="#lnkXml",
        files=(HelperFile(kind="pdf", path=Path("out/comp.pdf"), size=10, sha256="a" * 64),),
        error_class="PlaywrightTimeoutError",
    )


def _full_request() -> ProbeRequest:
    return ProbeRequest(
        url="https://p.example.pe/consulta",
        invoice=_invoice(),
        out_dir=Path("probe"),
        max_bytes=_max_bytes(),
        page_timeout_ms=30000,
    )


def test_probe_report_round_trips_through_json() -> None:
    """D193, R-11.12: a full report survives the wire format unchanged."""
    report = _full_report()

    assert ProbeReport.model_validate_json(report.model_dump_json()) == report


def test_probe_request_round_trips_through_json() -> None:
    """D193, R-11.12: a full probe request survives the wire format unchanged."""
    request = _full_request()

    assert ProbeRequest.model_validate_json(request.model_dump_json()) == request


@pytest.mark.parametrize(
    "selector",
    ["#a b", "#x\n", "[name='a\"]", "#" + "a" * 100, "#ñ"],
)
def test_probed_field_rejects_an_invalid_selector(selector: str) -> None:
    """D193, R-11.12: a selector outside the id or name form is refused."""
    with pytest.raises(ValidationError):
        ProbedField(selector=selector, kind="text", label="Serie")


@pytest.mark.parametrize("selector", ["#txtSerie", "[name='ctl00$txt:Serie']"])
def test_probed_field_accepts_a_valid_selector(selector: str) -> None:
    """D193, R-11.12: an id or a name selector is kept as read."""
    assert ProbedField(selector=selector, kind="text", label="Serie").selector == selector


@pytest.mark.parametrize("label", ["a<b", "a@b", "a`b", "a\nb", "a" * 61])
def test_probed_field_rejects_an_unsafe_label(label: str) -> None:
    """D193, R-11.12: a label with a character outside the allow-list is refused."""
    with pytest.raises(ValidationError):
        ProbedField(selector="#txtSerie", kind="text", label=label)


def test_probed_field_accepts_a_sixty_character_label() -> None:
    """D193, R-11.12: a label at the length limit is accepted."""
    assert ProbedField(selector="#txtSerie", kind="text", label="a" * 60).label == "a" * 60


def test_probed_field_rejects_an_unsafe_option() -> None:
    """D193, R-11.12: an option with a character outside the allow-list is refused."""
    with pytest.raises(ValidationError):
        ProbedField(selector="#cboTipo", kind="select", label="Tipo", options=("a<b",))


@pytest.mark.parametrize("form_path", ["/a?b", "/a(1)", "a/b"])
def test_probe_report_rejects_an_invalid_form_path(form_path: str) -> None:
    """D193, R-11.12: a form path that is not a bare path on the approved host is refused."""
    with pytest.raises(ValidationError):
        ProbeReport(outcome="ok", form_path=form_path)


def test_probe_report_rejects_more_than_twenty_fields() -> None:
    """D193, R-11.12: a report with more than the field limit is refused."""
    with pytest.raises(ValidationError):
        ProbeReport(outcome="ok", fields=tuple(_field() for _ in range(21)))


def test_probe_report_accepts_twenty_fields() -> None:
    """D193, R-11.12: a report at the field limit is accepted."""
    assert len(ProbeReport(outcome="ok", fields=tuple(_field() for _ in range(20))).fields) == 20


@pytest.mark.parametrize("selector", ["button[type='submit']", "input[type='submit']"])
def test_probe_report_accepts_an_unnamed_submit(selector: str) -> None:
    """R-11.12, D193: an unnamed submit selector is accepted as a report's submit."""
    assert ProbeReport(outcome="ok", submit=selector).submit == selector


def test_probe_report_rejects_an_unquoted_submit_type() -> None:
    """R-11.12, D193: a submit selector without the quotes around the type is refused."""
    with pytest.raises(ValidationError):
        ProbeReport(outcome="ok", submit="button[type=submit]")


def test_probed_field_rejects_more_than_twenty_options() -> None:
    """D193, R-11.12: a select with more than the option limit is refused."""
    with pytest.raises(ValidationError):
        ProbedField(selector="#cboTipo", kind="select", label="Tipo", options=("a",) * 21)


def test_probed_field_accepts_twenty_options() -> None:
    """D193, R-11.12: a select at the option limit is accepted."""
    field = ProbedField(selector="#cboTipo", kind="select", label="Tipo", options=("a",) * 20)

    assert len(field.options) == 20


@pytest.mark.parametrize(
    "build",
    [
        lambda: ProbeRequest(
            url="https://p.example.pe",
            invoice=_invoice(),
            out_dir=Path("probe"),
            max_bytes=_max_bytes(),
            page_timeout_ms=1,
            extra=1,
        ),
        lambda: ProbedField(selector="#txtSerie", kind="text", label="Serie", extra=1),
        lambda: ProbeReport(outcome="ok", extra=1),
    ],
)
def test_models_reject_an_unknown_key(build: Callable[[], object]) -> None:
    """D193, R-11.12: an unknown key is refused on every probe model."""
    with pytest.raises(ValidationError):
        build()


def test_probe_report_rejects_an_unknown_outcome() -> None:
    """D193, R-11.12: an outcome outside the fixed set is refused."""
    with pytest.raises(ValidationError):
        ProbeReport(outcome="weird")


def test_probe_request_rejects_a_zero_page_timeout() -> None:
    """D193, R-11.12: a page timeout of zero is refused."""
    with pytest.raises(ValidationError):
        ProbeRequest(
            url="https://p.example.pe",
            invoice=_invoice(),
            out_dir=Path("probe"),
            max_bytes=_max_bytes(),
            page_timeout_ms=0,
        )


def test_probe_report_rejects_an_error_class_with_a_space() -> None:
    """D193, R-11.12: an error class that is not an identifier is refused."""
    with pytest.raises(ValidationError):
        ProbeReport(outcome="technical", error_class="a b")
