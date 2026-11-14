"""Tests for filling the pinned Excel template (R-7.4, R-7.10, BR-D02)."""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from lxml import etree

from invoicebot.config.models import ExcelDefaults, ExcelTemplateSink
from invoicebot.sinks.excel_template import TemplateInvalid, build_excel
from xlsx_factory import (
    CELL_BASE,
    CELL_DESCRIPCION,
    CELL_FECHA,
    CELL_IGV,
    CELL_MOTIVO,
    CELL_SERIE_NUMERO,
    CELL_SERVICE_CHARGE,
    CELL_TOTAL,
    CELL_TOTAL_CAPPED,
    SHEET_NAME,
    SHEET_PART,
    build_template,
)

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

_CELLS = {
    CELL_MOTIVO: "{motivo}",
    CELL_FECHA: "{fecha:%d/%m/%Y}",
    CELL_DESCRIPCION: "{descripcion}",
    CELL_SERIE_NUMERO: "{serie}-{numero}",
    CELL_BASE: "{base}",
    CELL_IGV: "{igv}",
    CELL_SERVICE_CHARGE: "{service_charge}",
    CELL_TOTAL: "{total}",
}

_VALUES: dict[str, object] = {
    "fecha": date(2026, 11, 5),
    "serie": "F001",
    "numero": "123",
    "comercio": "LaLena",
    "base": Decimal("40.00"),
    "igv": Decimal("7.20"),
    "service_charge": Decimal("4.00"),
    "total": Decimal("51.20"),
}


def _write_template(tmp_path: Path) -> tuple[Path, bytes]:
    template = build_template()
    path = tmp_path / "template.xlsx"
    path.write_bytes(template)
    return path, template


def _sink(
    path: Path,
    template: bytes,
    *,
    cells: dict[str, str] | None = None,
    sha256: str | None = None,
) -> ExcelTemplateSink:
    return ExcelTemplateSink(
        kind="excel_template",
        template=path,
        template_sha256=sha256 or hashlib.sha256(template).hexdigest(),
        output="Formato de reembolsos.xlsx",
        sheet=SHEET_NAME,
        cells=_CELLS if cells is None else cells,
        defaults=ExcelDefaults(motivo="Support release", descripcion="Cena en {comercio}"),
    )


def _sheet_cell(data: bytes, ref: str) -> etree._Element:
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        root = etree.fromstring(package.read(SHEET_PART))
    for row in root.findall(f"{{{_MAIN_NS}}}sheetData/{{{_MAIN_NS}}}row"):
        for cell in row.findall(f"{{{_MAIN_NS}}}c"):
            if cell.get("r") == ref:
                return cell
    raise AssertionError(f"cell {ref} not found")


def _inline_text(cell: etree._Element) -> str:
    text = cell.find(f"{{{_MAIN_NS}}}is/{{{_MAIN_NS}}}t")
    assert text is not None
    return text.text or ""


def _cached_number(cell: etree._Element) -> str:
    value = cell.find(f"{{{_MAIN_NS}}}v")
    assert value is not None
    return value.text or ""


def test_build_excel_fills_every_mapped_cell(tmp_path: Path) -> None:
    """R-7.4: the mapped cells get the rendered values from the invoice fields."""
    path, template = _write_template(tmp_path)

    data = build_excel(_sink(path, template), _VALUES, {})

    assert _inline_text(_sheet_cell(data, CELL_MOTIVO)) == "Support release"
    assert _inline_text(_sheet_cell(data, CELL_FECHA)) == "05/11/2026"
    assert _inline_text(_sheet_cell(data, CELL_DESCRIPCION)) == "Cena en LaLena"
    assert _inline_text(_sheet_cell(data, CELL_SERIE_NUMERO)) == "F001-123"


def test_build_excel_writes_amounts_as_numbers(tmp_path: Path) -> None:
    """R-7.4, R-7.10: amount cells hold numbers with no text type and the cap formula stays."""
    path, template = _write_template(tmp_path)

    data = build_excel(_sink(path, template), _VALUES, {})

    expected = {
        CELL_BASE: "40.00",
        CELL_IGV: "7.20",
        CELL_SERVICE_CHARGE: "4.00",
        CELL_TOTAL: "51.20",
    }
    for ref, amount in expected.items():
        cell = _sheet_cell(data, ref)
        assert cell.get("t") is None
        assert _cached_number(cell) == amount
    formula = _sheet_cell(data, CELL_TOTAL_CAPPED).find(f"{{{_MAIN_NS}}}f")
    assert formula is not None
    assert formula.text == "MIN(I16, 50)"


def test_build_excel_motivo_override_wins_over_default(tmp_path: Path) -> None:
    """R-7.4: a sender's motivo override replaces the configured default."""
    path, template = _write_template(tmp_path)

    data = build_excel(_sink(path, template), _VALUES, {"motivo": "Almuerzo"})

    assert _inline_text(_sheet_cell(data, CELL_MOTIVO)) == "Almuerzo"


def test_build_excel_override_is_written_as_literal_text(tmp_path: Path) -> None:
    """R-7.4: an override is the sender's text, not a template to render."""
    path, template = _write_template(tmp_path)

    data = build_excel(_sink(path, template), _VALUES, {"motivo": "{comercio} cena"})

    assert _inline_text(_sheet_cell(data, CELL_MOTIVO)) == "{comercio} cena"


def test_build_excel_default_descripcion_renders_comercio(tmp_path: Path) -> None:
    """R-7.4: the configured description renders the invoice's comercio."""
    path, template = _write_template(tmp_path)

    data = build_excel(_sink(path, template), _VALUES, {})

    assert _inline_text(_sheet_cell(data, CELL_DESCRIPCION)) == "Cena en LaLena"


def test_build_excel_missing_template_raises_missing(tmp_path: Path) -> None:
    """BR-D02: a template file that does not exist is refused without leaking its path."""
    path = tmp_path / "absent.xlsx"

    with pytest.raises(TemplateInvalid) as raised:
        build_excel(_sink(path, build_template()), _VALUES, {})

    assert raised.value.reason == "missing"
    assert "absent.xlsx" not in str(raised.value)


def test_build_excel_wrong_hash_raises_hash(tmp_path: Path) -> None:
    """R-7.10, BR-D02: a template that does not match its pinned SHA-256 is refused."""
    path, template = _write_template(tmp_path)
    pinned = "0" * 64

    with pytest.raises(TemplateInvalid) as raised:
        build_excel(_sink(path, template, sha256=pinned), _VALUES, {})

    assert raised.value.reason == "hash"
    assert pinned not in str(raised.value)


def test_build_excel_unknown_field_raises_cells(tmp_path: Path) -> None:
    """BR-D02: a cells expression naming an unknown field is a cells error."""
    path, template = _write_template(tmp_path)

    with pytest.raises(TemplateInvalid) as raised:
        build_excel(_sink(path, template, cells={CELL_MOTIVO: "{nope}"}), _VALUES, {})

    assert raised.value.reason == "cells"
    assert "{nope}" not in str(raised.value)


def test_build_excel_missing_amount_writes_zero(tmp_path: Path) -> None:
    """R-7.4: an amount the invoice does not carry is written as zero."""
    path, template = _write_template(tmp_path)
    values = {key: value for key, value in _VALUES.items() if key != "base"}

    data = build_excel(_sink(path, template), values, {})

    cell = _sheet_cell(data, CELL_BASE)
    assert cell.get("t") is None
    assert _cached_number(cell) == "0"


def test_build_excel_non_decimal_amount_raises_cells(tmp_path: Path) -> None:
    """R-7.4: an amount field that is not a Decimal is a cells error."""
    path, template = _write_template(tmp_path)
    values = {**_VALUES, "base": "40.00"}

    with pytest.raises(TemplateInvalid) as raised:
        build_excel(_sink(path, template), values, {})

    assert raised.value.reason == "cells"
