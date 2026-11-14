"""Tests for patching cells inside the Excel package (R-7.4, R-7.10, D71)."""

from __future__ import annotations

import io
import zipfile
from decimal import Decimal

import pytest
from lxml import etree

from invoicebot.sinks.xlsx_patch import TemplateError, patch_cells
from xlsx_factory import (
    ALL_PARTS,
    CALC_CHAIN_PART,
    CELL_BASE,
    CELL_DESCRIPCION,
    CELL_IGV,
    CELL_MOTIVO,
    CELL_SERIE_NUMERO,
    CELL_SERVICE_CHARGE,
    CELL_TOTAL,
    CELL_TOTAL_CAPPED,
    CELL_UNRELATED_FORMULA,
    DRAWING_PART,
    SHEET_NAME,
    SHEET_PART,
    build_template,
)

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XDR_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
_XML_NS = "http://www.w3.org/XML/1998/namespace"

MAIN_WRITES: dict[str, str | Decimal] = {
    CELL_MOTIVO: "Support release",
    CELL_BASE: Decimal("40.00"),
    CELL_IGV: Decimal("7.20"),
    CELL_SERIE_NUMERO: "F001-123",
    CELL_DESCRIPCION: "=cmd",
}


def _zip_entries(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        return {info.filename: package.read(info) for info in package.infolist()}


def _parse(data: bytes) -> etree._Element:
    return etree.fromstring(data)


def _cell(root: etree._Element, ref: str) -> etree._Element:
    for row in root.findall(f"{{{_MAIN_NS}}}sheetData/{{{_MAIN_NS}}}row"):
        for cell in row.findall(f"{{{_MAIN_NS}}}c"):
            if cell.get("r") == ref:
                return cell
    raise AssertionError(f"cell {ref} not found")


def _cached_value(root: etree._Element, ref: str) -> str:
    cached = _cell(root, ref).find(f"{{{_MAIN_NS}}}v")
    assert cached is not None
    assert cached.text is not None
    return cached.text


def _calc_chain_refs(data: bytes) -> set[str]:
    root = _parse(_zip_entries(data)[CALC_CHAIN_PART])
    return {entry.get("r") for entry in root.findall(f"{{{_MAIN_NS}}}c")}


def _replace_sheet(template: bytes, old: bytes, new: bytes) -> bytes:
    """Return ``template`` with the first ``old`` byte string in the sheet replaced by ``new``."""
    with zipfile.ZipFile(io.BytesIO(template)) as package:
        infos = package.infolist()
        contents = {info.filename: package.read(info) for info in infos}
    contents[SHEET_PART] = contents[SHEET_PART].replace(old, new)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for info in infos:
            package.writestr(info, contents[info.filename])
    return buffer.getvalue()


_RANGE_FORMULA = "ROUND(SUM(B1:B5),2)"
_RANGE_CACHED = "15"


def _range_template(formula: str = _RANGE_FORMULA) -> bytes:
    """Return the template with B1..B5 = 1..5 and C1 holding ``formula`` with a cached value."""
    row = (
        '<row r="1">'
        '<c r="B1"><v>1</v></c><c r="B2"><v>2</v></c><c r="B3"><v>3</v></c>'
        '<c r="B4"><v>4</v></c><c r="B5"><v>5</v></c>'
        f'<c r="C1"><f>{formula}</f><v>{_RANGE_CACHED}</v></c>'
        "</row>"
    )
    return _replace_sheet(build_template(), b"<sheetData>", f"<sheetData>{row}".encode())


def test_patch_cells_keeps_untouched_parts_byte_identical() -> None:
    """D71: every part except the sheet and calcChain is copied byte-for-byte."""
    original = _zip_entries(build_template())
    patched = _zip_entries(patch_cells(build_template(), SHEET_NAME, MAIN_WRITES))

    untouched = set(ALL_PARTS) - {SHEET_PART, CALC_CHAIN_PART}
    assert all(patched[name] == original[name] for name in untouched)


def test_patch_cells_writes_text_as_inline_string() -> None:
    """R-7.10: a text write becomes an inline string with preserved whitespace."""
    root = _parse(_zip_entries(patch_cells(build_template(), SHEET_NAME, MAIN_WRITES))[SHEET_PART])
    cell = _cell(root, CELL_MOTIVO)

    assert cell.get("t") == "inlineStr"
    text = cell.find(f"{{{_MAIN_NS}}}is/{{{_MAIN_NS}}}t")
    assert text is not None
    assert text.text == "Support release"
    assert text.get(f"{{{_XML_NS}}}space") == "preserve"


def test_patch_cells_escapes_a_text_that_looks_like_a_formula() -> None:
    """R-7.10: a text starting with ``=`` gets a leading apostrophe."""
    root = _parse(_zip_entries(patch_cells(build_template(), SHEET_NAME, MAIN_WRITES))[SHEET_PART])
    text = _cell(root, CELL_DESCRIPCION).find(f"{{{_MAIN_NS}}}is/{{{_MAIN_NS}}}t")

    assert text is not None
    assert text.text == "'=cmd"


def test_patch_cells_writes_decimals_without_a_formula() -> None:
    """R-7.4: a decimal write is a plain ``<v>`` with no ``t`` and no ``<f>``."""
    root = _parse(_zip_entries(patch_cells(build_template(), SHEET_NAME, MAIN_WRITES))[SHEET_PART])

    assert _cell(root, CELL_BASE).find(f"{{{_MAIN_NS}}}f") is None
    assert _cell(root, CELL_BASE).get("t") is None
    assert _cached_value(root, CELL_BASE) == "40.00"
    assert _cached_value(root, CELL_IGV) == "7.20"


def test_patch_cells_recomputes_dependent_cached_values() -> None:
    """R-7.4: formula cells that read a written cell are recomputed, others are left alone."""
    root = _parse(_zip_entries(patch_cells(build_template(), SHEET_NAME, MAIN_WRITES))[SHEET_PART])

    assert Decimal(_cached_value(root, CELL_SERVICE_CHARGE)) == Decimal("4.0")
    assert Decimal(_cached_value(root, CELL_TOTAL)) == Decimal("51.2")
    assert Decimal(_cached_value(root, CELL_TOTAL_CAPPED)) == Decimal("50")
    assert _cached_value(root, CELL_UNRELATED_FORMULA) == "2"
    unrelated = _cell(root, CELL_UNRELATED_FORMULA).find(f"{{{_MAIN_NS}}}f")
    assert unrelated is not None
    assert unrelated.text == "1+1"


def test_writing_a_formula_cell_removes_its_formula_and_calc_chain_entry() -> None:
    """R-7.4: writing H16 drops its formula, drops its calcChain entry and recomputes I16 and J16."""
    output = patch_cells(build_template(), SHEET_NAME, {CELL_SERVICE_CHARGE: Decimal("5.00")})
    root = _parse(_zip_entries(output)[SHEET_PART])

    assert _cell(root, CELL_SERVICE_CHARGE).find(f"{{{_MAIN_NS}}}f") is None
    assert _cached_value(root, CELL_SERVICE_CHARGE) == "5.00"
    assert Decimal(_cached_value(root, CELL_TOTAL)) == Decimal("52.2")
    assert Decimal(_cached_value(root, CELL_TOTAL_CAPPED)) == Decimal("50")
    assert _calc_chain_refs(output) == {CELL_TOTAL, CELL_TOTAL_CAPPED, CELL_UNRELATED_FORMULA}


def test_patch_cells_keeps_the_drawing_and_cell_styles() -> None:
    """D71: the drawing keeps its shapes and picture, and cell styles are untouched."""
    output = patch_cells(build_template(), SHEET_NAME, MAIN_WRITES)
    root = _parse(_zip_entries(output)[SHEET_PART])
    drawing = _parse(_zip_entries(output)[DRAWING_PART])

    namespaces = {"xdr": _XDR_NS}
    assert len(drawing.findall(".//xdr:sp", namespaces)) == 2
    assert len(drawing.findall(".//xdr:pic", namespaces)) == 1
    assert _cell(root, CELL_DESCRIPCION).get("s") == "1"
    assert _cell(root, CELL_SERIE_NUMERO).get("s") == "1"
    assert _cell(root, CELL_BASE).get("s") is None


def test_patch_cells_keeps_sheet_page_setup_and_drawing() -> None:
    """D71: the sheet keeps its pageSetup and drawing elements after the patch."""
    root = _parse(_zip_entries(patch_cells(build_template(), SHEET_NAME, MAIN_WRITES))[SHEET_PART])

    assert root.find(f"{{{_MAIN_NS}}}pageSetup") is not None
    assert root.find(f"{{{_MAIN_NS}}}drawing") is not None


def test_patch_cells_creates_missing_row_and_cell_in_order() -> None:
    """R-7.4: a missing row gets only ``r`` and a missing cell gets only ``r`` plus its content."""
    output = patch_cells(build_template(), SHEET_NAME, {"D16": "new", "C21": Decimal("9")})
    root = _parse(_zip_entries(output)[SHEET_PART])
    rows = root.findall(f"{{{_MAIN_NS}}}sheetData/{{{_MAIN_NS}}}row")

    assert [row.get("r") for row in rows] == ["13", "16", "20", "21"]
    row_16 = next(row for row in rows if row.get("r") == "16")
    assert [cell.get("r") for cell in row_16.findall(f"{{{_MAIN_NS}}}c")] == [
        "B16",
        CELL_DESCRIPCION,
        "D16",
        CELL_SERIE_NUMERO,
        CELL_BASE,
        CELL_IGV,
        CELL_SERVICE_CHARGE,
        CELL_TOTAL,
        CELL_TOTAL_CAPPED,
    ]
    assert next(row for row in rows if row.get("r") == "21").attrib == {"r": "21"}
    assert _cell(root, "D16").attrib == {"r": "D16", "t": "inlineStr"}
    assert _cell(root, "C21").attrib == {"r": "C21"}


def test_unknown_sheet_raises() -> None:
    """R-7.4: a sheet that is not in the workbook is refused."""
    with pytest.raises(TemplateError, match="not in the workbook"):
        patch_cells(build_template(), "Missing", {})


def test_non_zip_input_raises() -> None:
    """D71: input that is not a zip package is refused."""
    with pytest.raises(TemplateError, match="not a zip"):
        patch_cells(b"not a zip", SHEET_NAME, {})


def test_dependent_unsupported_formula_raises() -> None:
    """R-7.4: a dependent formula outside the supported subset is refused."""
    template = _replace_sheet(
        build_template(),
        f">{CELL_BASE}+{CELL_IGV}+{CELL_SERVICE_CHARGE}<".encode(),
        f">IF({CELL_BASE}&gt;1,1,0)<".encode(),
    )

    with pytest.raises(TemplateError, match="cannot recompute I16"):
        patch_cells(template, SHEET_NAME, {CELL_BASE: Decimal("40")})


def test_write_inside_an_unsupported_formula_range_raises() -> None:
    """R-7.4, BR-D02: a write inside a range read by an unsupported formula is refused, not left stale."""
    with pytest.raises(TemplateError, match="cannot recompute C1"):
        patch_cells(_range_template(), SHEET_NAME, {"B3": Decimal("9")})


def test_write_outside_an_unsupported_formula_range_keeps_cached_value() -> None:
    """R-7.4, BR-D02: a write outside the range leaves the unsupported formula's cached value alone."""
    output = patch_cells(_range_template(), SHEET_NAME, {"D9": Decimal("1")})
    root = _parse(_zip_entries(output)[SHEET_PART])

    assert _cached_value(root, "C1") == _RANGE_CACHED


def test_unsupported_formula_range_past_the_cap_raises() -> None:
    """R-7.4, BR-D02: a range past the 1000-cell cap on an unsupported formula fails closed."""
    template = _range_template("ROUND(SUM(A1:Z100),2)")

    with pytest.raises(TemplateError, match="at most 1000"):
        patch_cells(template, SHEET_NAME, {"D9": Decimal("1")})


def test_dependent_shared_formula_raises() -> None:
    """R-7.4: a dependent shared formula is refused."""
    template = _replace_sheet(
        build_template(),
        f"<f>{CELL_BASE}*0.1</f>".encode(),
        f'<f t="shared" ref="H16:H17" si="0">{CELL_BASE}*0.1</f>'.encode(),
    )

    with pytest.raises(TemplateError, match="shared or array"):
        patch_cells(template, SHEET_NAME, {CELL_BASE: Decimal("40")})


def test_shared_formula_copy_raises() -> None:
    """R-7.4: a shared formula copy has no text, so what it reads is unknown and it is refused."""
    template = _replace_sheet(
        build_template(),
        b"<f>1+1</f>",
        b'<f t="shared" si="0"/>',
    )

    with pytest.raises(TemplateError, match="shared or array"):
        patch_cells(template, SHEET_NAME, {CELL_BASE: Decimal("40")})


def test_dependent_formula_cycle_raises() -> None:
    """R-7.4: a cycle among the dependent formulas is refused."""
    template = _replace_sheet(
        build_template(),
        f">{CELL_BASE}+{CELL_IGV}+{CELL_SERVICE_CHARGE}<".encode(),
        f">{CELL_TOTAL_CAPPED}+{CELL_BASE}<".encode(),
    )
    template = _replace_sheet(
        template,
        f">MIN({CELL_TOTAL}, 50)<".encode(),
        f">{CELL_TOTAL}+{CELL_BASE}<".encode(),
    )

    with pytest.raises(TemplateError, match="cycle"):
        patch_cells(template, SHEET_NAME, {CELL_BASE: Decimal("40")})


def test_calc_chain_would_be_empty_raises() -> None:
    """R-7.4: dropping every calcChain entry is refused instead of removing the part."""
    writes = {
        CELL_SERVICE_CHARGE: Decimal("1"),
        CELL_TOTAL: Decimal("2"),
        CELL_TOTAL_CAPPED: Decimal("3"),
        CELL_UNRELATED_FORMULA: Decimal("4"),
    }

    with pytest.raises(TemplateError, match="calcChain would be empty"):
        patch_cells(build_template(), SHEET_NAME, writes)


def test_patch_cells_rejects_too_many_parts() -> None:
    """D71: a package with more than 200 parts is refused."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        for number in range(201):
            package.writestr(f"part{number}", b"")

    with pytest.raises(TemplateError, match="200"):
        patch_cells(buffer.getvalue(), SHEET_NAME, {})


def test_patch_cells_rejects_a_package_expanding_past_the_cap() -> None:
    """D71: a package whose declared contents expand past 20 MB is refused."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("big.bin", b"x" * (20 * 1024 * 1024 + 1))

    with pytest.raises(TemplateError, match="expands past"):
        patch_cells(buffer.getvalue(), SHEET_NAME, {})
