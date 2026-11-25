"""Tests for matching portal form fields to invoice values (D193)."""

from __future__ import annotations

from datetime import date

import pytest

from invoicebot.fetchers.browser.field_match import date_text, tipo_option, value_name


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Serie", "serie"),
        ("N° Correlativo", "numero"),
        ("Tipo de Documento", "tipo"),
        ("Fecha de Emisión", "fecha"),
        ("Monto Total", "total"),
        ("RUC del Emisor", "ruc_emisor"),
    ],
)
def test_value_name_reads_a_label(label: str, expected: str) -> None:
    """D193: a label's words name the invoice value it carries."""
    assert value_name(label, "") == expected


@pytest.mark.parametrize(
    ("html_name", "expected"),
    [
        ("txtSerie", "serie"),
        ("txtCorrelativo", "numero"),
        ("cboTipoDoc", "tipo"),
        ("txtFecha", "fecha"),
        ("txtMonto", "total"),
        ("cboEmisor", "ruc_emisor"),
        ("txtRUCEmisor", "ruc_emisor"),
        ("ctl00$txtSerie", "serie"),
        ("nro_correlativo", "numero"),
    ],
)
def test_value_name_reads_an_html_name(html_name: str, expected: str) -> None:
    """D193: an HTML name's CamelCase, digit and separator parts name the value."""
    assert value_name("", html_name) == expected


@pytest.mark.parametrize(
    ("label", "html_name"),
    [
        ("RUC del Cliente", ""),
        ("Código Captcha", ""),
        ("Usuario", ""),
        ("Comentario", ""),
        ("RUC del Cliente", "txtSerie"),
        ("", "txtRucCliente"),
        ("", ""),
    ],
)
def test_value_name_returns_none(label: str, html_name: str) -> None:
    """D193: an excluded word or an empty label and name gives no value."""
    assert value_name(label, html_name) is None


@pytest.mark.parametrize(
    ("label", "html_name", "expected"),
    [
        ("Número de Serie", "", "serie"),
        ("Serie", "txtCodigo", "serie"),
        ("Comentario", "txtSerie", "serie"),
    ],
)
def test_value_name_prefers_label_then_name(label: str, html_name: str, expected: str) -> None:
    """D193: a label match wins over the name, and the name backs an unmatched label."""
    assert value_name(label, html_name) == expected


@pytest.mark.parametrize(
    ("options", "tipo", "expected"),
    [
        (["Seleccione", "FACTURA ELECTRONICA", "BOLETA DE VENTA"], "01", "FACTURA ELECTRONICA"),
        (["Seleccione", "FACTURA ELECTRONICA", "BOLETA DE VENTA"], "03", "BOLETA DE VENTA"),
        (["Seleccione", "FACTURA ELECTRONICA", "BOLETA DE VENTA"], "07", None),
        (["FACTURA ELECTRONICA", "FACTURA"], "01", None),
        (["NOTA DE CREDITO"], "01", None),
        (["NOTA DE CREDITO"], "03", None),
        (["FACTURA", "FACTURA DE EXPORTACION"], "01", "FACTURA"),
        (["FACTURA/BOLETA"], "01", None),
        ([], "01", None),
        (["Boleta Electrónica"], "03", "Boleta Electrónica"),
        (["Boléta"], "03", "Boléta"),
    ],
)
def test_tipo_option_names_the_tipo_alone(options: list[str], tipo: str, expected: str | None) -> None:
    """D193: exactly one option naming the tipo, not the other, is returned as-is."""
    assert tipo_option(options, tipo) == expected


@pytest.mark.parametrize(
    ("placeholder", "expected"),
    [
        ("dd/mm/yyyy", "05/11/2026"),
        ("DD-MM-YYYY", "05-11-2026"),
        ("yyyy-mm-dd", "2026-11-05"),
        ("", "2026-11-05"),
        ("fecha", "05/11/2026"),
        ("dd/mm/aaaa", "05/11/2026"),
        ("dd-mm-aaaa", "05-11-2026"),
    ],
)
def test_date_text_follows_the_placeholder(placeholder: str, expected: str) -> None:
    """D193: the stripped placeholder's format decides the date's text."""
    assert date_text(date(2026, 11, 5), placeholder) == expected
