"""Patch cells inside an OOXML spreadsheet package (R-7.4, R-7.10, D71).

One job: take an ``.xlsx`` package, write the given cells on one sheet, recompute the
formula cells that read them, drop the matching ``calcChain`` entries, and write a new
package whose untouched parts are byte-identical to the original.
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from collections.abc import Iterator, Mapping
from decimal import Decimal, InvalidOperation

from lxml import etree

from invoicebot.sinks.xlsx_formula import UnsupportedFormula, parse, range_refs
from invoicebot.text import escape_formula

_MAX_ENTRIES = 200
_MAX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024

_WORKBOOK_PART = "xl/workbook.xml"
_WORKBOOK_RELS_PART = "xl/_rels/workbook.xml.rels"
_CALC_CHAIN_PART = "xl/calcChain.xml"

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_XML_NS = "http://www.w3.org/XML/1998/namespace"

_CELL_REF_RE = re.compile(r"\$?[A-Za-z]{1,3}\$?\d+")
_RANGE_REF_RE = re.compile(r"(\$?[A-Za-z]{1,3}\$?\d+):(\$?[A-Za-z]{1,3}\$?\d+)")


class TemplateError(ValueError):
    """The template package cannot be patched as asked (R-7.4, R-7.10, D71)."""


def patch_cells(template: bytes, sheet: str, writes: Mapping[str, str | Decimal]) -> bytes:
    """Return ``template`` with ``writes`` applied to ``sheet`` and dependent formulas recomputed."""
    parts = _read_package(template)
    sheet_part = _find_sheet_part(parts, sheet)
    root = _parse_xml(_part_bytes(parts, sheet_part))
    sheet_data = _sheet_data(root)
    normalized = {_normalize_ref(ref): value for ref, value in writes.items()}
    written_formula_cells = _formula_targets(sheet_data, set(normalized))
    for ref, value in normalized.items():
        _write_cell(sheet_data, ref, value)
    _recompute(sheet_data, set(normalized))
    replacements: dict[str, bytes] = {sheet_part: _serialize(root)}
    calc_chain = _rewrite_calc_chain(parts, written_formula_cells)
    if calc_chain is not None:
        replacements[_CALC_CHAIN_PART] = calc_chain
    return _write_package(parts, replacements)


def _read_package(data: bytes) -> list[tuple[zipfile.ZipInfo, bytes]]:
    """Return the package entries in order, refusing oversized or oversized-expanding input."""
    try:
        package = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise TemplateError("the template is not a zip package") from exc
    with package:
        infos = package.infolist()
        if len(infos) > _MAX_ENTRIES:
            raise TemplateError(f"the template has more than {_MAX_ENTRIES} parts")
        if sum(info.file_size for info in infos) > _MAX_UNCOMPRESSED_BYTES:
            raise TemplateError(f"the template expands past {_MAX_UNCOMPRESSED_BYTES} bytes")
        parts: list[tuple[zipfile.ZipInfo, bytes]] = []
        budget = _MAX_UNCOMPRESSED_BYTES
        for info in infos:
            content = _read_entry(package, info, budget)
            budget -= len(content)
            parts.append((info, content))
        return parts


def _read_entry(package: zipfile.ZipFile, info: zipfile.ZipInfo, budget: int) -> bytes:
    """Read one entry, refusing to expand past the remaining ``budget`` bytes."""
    with package.open(info) as stream:
        content = stream.read(budget + 1)
    if len(content) > budget:
        raise TemplateError(f"the template expands past {_MAX_UNCOMPRESSED_BYTES} bytes")
    return content


def _find_sheet_part(parts: list[tuple[zipfile.ZipInfo, bytes]], sheet: str) -> str:
    """Return the package part name for ``sheet``, or raise ``TemplateError``."""
    workbook = _parse_xml(_part_bytes(parts, _WORKBOOK_PART))
    rels = _parse_xml(_part_bytes(parts, _WORKBOOK_RELS_PART))
    for sheet_el in workbook.findall(f"{{{_MAIN_NS}}}sheets/{{{_MAIN_NS}}}sheet"):
        if sheet_el.get("name") != sheet:
            continue
        rel_id = sheet_el.get(f"{{{_REL_NS}}}id")
        if rel_id is None:
            raise TemplateError(f"sheet {sheet!r} has no relationship id")
        return _resolve_sheet_target(_relationship_target(rels, rel_id))
    raise TemplateError(f"sheet {sheet!r} is not in the workbook")


def _relationship_target(rels: etree._Element, rel_id: str) -> str:
    """Return the target of workbook relationship ``rel_id``."""
    for rel in rels.findall(f"{{{_PKG_REL_NS}}}Relationship"):
        if rel.get("Id") == rel_id:
            target = rel.get("Target")
            if target is None:
                raise TemplateError(f"relationship {rel_id!r} has no target")
            return target
    raise TemplateError(f"relationship {rel_id!r} is missing from the workbook rels")


def _resolve_sheet_target(target: str) -> str:
    """Resolve a workbook relationship target against ``xl/``, honouring a leading ``/``."""
    if target.startswith("/"):
        return target[1:]
    return posixpath.normpath(f"xl/{target}")


def _part_bytes(parts: list[tuple[zipfile.ZipInfo, bytes]], name: str) -> bytes:
    """Return the content of package part ``name``."""
    for info, content in parts:
        if info.filename == name:
            return content
    raise TemplateError(f"the package is missing {name!r}")


def _parse_xml(data: bytes) -> etree._Element:
    """Parse package XML with the hardened parser (no entities, DTD, network or huge trees)."""
    return etree.fromstring(data, _hardened_parser())


def _hardened_parser() -> etree.XMLParser:
    """Return a parser that refuses entities, DTDs, network access and huge trees."""
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
    )


def _serialize(root: etree._Element) -> bytes:
    """Serialize an OOXML part, keeping its namespace prefixes."""
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _sheet_data(root: etree._Element) -> etree._Element:
    """Return the ``sheetData`` element, creating it when it is missing."""
    sheet_data = root.find(f"{{{_MAIN_NS}}}sheetData")
    if sheet_data is None:
        sheet_data = etree.Element(f"{{{_MAIN_NS}}}sheetData")
        root.insert(0, sheet_data)
    return sheet_data


def _iter_cells(sheet_data: etree._Element) -> Iterator[tuple[str, etree._Element]]:
    """Yield ``(normalized_ref, cell)`` for every cell that has an ``r`` attribute."""
    for row in sheet_data.findall(f"{{{_MAIN_NS}}}row"):
        for cell in row.findall(f"{{{_MAIN_NS}}}c"):
            ref = cell.get("r")
            if ref is not None:
                yield _normalize_ref(ref), cell


def _formula_targets(sheet_data: etree._Element, refs: set[str]) -> set[str]:
    """Return the written refs whose existing cell holds a formula."""
    cells = dict(_iter_cells(sheet_data))
    return {ref for ref in refs if ref in cells and cells[ref].find(f"{{{_MAIN_NS}}}f") is not None}


def _write_cell(sheet_data: etree._Element, ref: str, value: str | Decimal) -> None:
    """Write ``value`` into ``ref``, keeping its style and clearing any formula or cached value."""
    column, row = _split_ref(ref)
    row_el = _ensure_row(sheet_data, row)
    cell = _ensure_cell(row_el, ref, column)
    cell.set("r", ref)
    if "t" in cell.attrib:
        del cell.attrib["t"]
    for child_name in ("f", "v", "is"):
        for child in cell.findall(f"{{{_MAIN_NS}}}{child_name}"):
            cell.remove(child)
    if isinstance(value, Decimal):
        cached = etree.SubElement(cell, f"{{{_MAIN_NS}}}v")
        cached.text = format(value, "f")
    else:
        cell.set("t", "inlineStr")
        inline = etree.SubElement(cell, f"{{{_MAIN_NS}}}is")
        text = etree.SubElement(inline, f"{{{_MAIN_NS}}}t")
        text.set(f"{{{_XML_NS}}}space", "preserve")
        text.text = escape_formula(value)


def _ensure_row(sheet_data: etree._Element, row_number: int) -> etree._Element:
    """Return the row with number ``row_number``, creating and ordering it when missing."""
    row_tag = f"{{{_MAIN_NS}}}row"
    for row_el in sheet_data.findall(row_tag):
        if _row_number(row_el) == row_number:
            return row_el
    row_el = etree.Element(row_tag, {"r": str(row_number)})
    index = 0
    for existing in sheet_data.findall(row_tag):
        if _row_number(existing) > row_number:
            break
        index += 1
    sheet_data.insert(index, row_el)
    return row_el


def _ensure_cell(row_el: etree._Element, ref: str, column: int) -> etree._Element:
    """Return the cell ``ref`` in ``row_el``, creating and ordering it when missing."""
    cell_tag = f"{{{_MAIN_NS}}}c"
    for cell in row_el.findall(cell_tag):
        if cell.get("r") == ref:
            return cell
    cell = etree.Element(cell_tag, {"r": ref})
    index = 0
    for existing in row_el.findall(cell_tag):
        if _cell_column(existing) > column:
            break
        index += 1
    row_el.insert(index, cell)
    return cell


def _row_number(row_el: etree._Element) -> int:
    """Return the row number of ``row_el``, refusing a non-numeric ``r``."""
    raw = row_el.get("r")
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError as exc:
        raise TemplateError(f"row {raw!r} has a non-numeric r attribute") from exc


def _cell_column(cell: etree._Element) -> int:
    """Return the column number of ``cell``, using 0 when it has no ``r``."""
    ref = cell.get("r")
    return _split_ref(ref)[0] if ref is not None else 0


def _recompute(sheet_data: etree._Element, written: set[str]) -> None:
    """Rewrite the cached values of the formulas that read a written cell (R-7.4)."""
    formulas = _formula_cells(sheet_data)
    # A shared formula's copies carry no text, so what they read is unknown: refuse any of them
    # rather than leave a cached value silently stale.
    for ref, cell in formulas.items():
        _reject_shared_or_array(cell.find(f"{{{_MAIN_NS}}}f"), ref)
    values = _numeric_values(sheet_data)
    for ref in _dependents(formulas, written):
        cell = formulas[ref]
        f_el = cell.find(f"{{{_MAIN_NS}}}f")
        if f_el is None:
            continue
        try:
            result = parse(f_el.text or "").evaluate(values)
        except UnsupportedFormula as exc:
            raise TemplateError(f"cannot recompute {ref}: {exc}") from exc
        values[ref] = result
        cached = cell.find(f"{{{_MAIN_NS}}}v")
        if cached is None:
            cached = etree.SubElement(cell, f"{{{_MAIN_NS}}}v")
        cached.text = format(result, "f")


def _formula_cells(sheet_data: etree._Element) -> dict[str, etree._Element]:
    """Return every cell that still holds a ``<f>`` element."""
    return {ref: cell for ref, cell in _iter_cells(sheet_data) if cell.find(f"{{{_MAIN_NS}}}f") is not None}


def _numeric_values(sheet_data: etree._Element) -> dict[str, Decimal]:
    """Return the numeric value of every cell that has a ``<v>`` and no text type."""
    values: dict[str, Decimal] = {}
    for ref, cell in _iter_cells(sheet_data):
        cached = cell.find(f"{{{_MAIN_NS}}}v")
        if cached is None or cell.get("t") not in (None, "n"):
            continue
        text = (cached.text or "").strip()
        if not text:
            continue
        try:
            values[ref] = Decimal(text)
        except InvalidOperation:
            continue
    return values


def _dependents(formulas: dict[str, etree._Element], written: set[str]) -> list[str]:
    """Return the formulas that read a written cell, in dependency order."""
    refs = {ref: _formula_refs(_formula_text(cell)) for ref, cell in formulas.items()}
    dirty = set(written)
    dependents: set[str] = set()
    changed = True
    while changed:
        changed = False
        for ref, cell_refs in refs.items():
            if ref not in dependents and cell_refs & dirty:
                dependents.add(ref)
                dirty.add(ref)
                changed = True
    remaining = set(dependents)
    order: list[str] = []
    while remaining:
        ready = sorted(ref for ref in remaining if not (refs[ref] & remaining))
        if not ready:
            raise TemplateError("the dependent formulas form a cycle")
        for ref in ready:
            order.append(ref)
            remaining.remove(ref)
    return order


def _formula_text(cell: etree._Element) -> str:
    """Return the text of a cell's ``<f>`` element."""
    f_el = cell.find(f"{{{_MAIN_NS}}}f")
    return (f_el.text or "") if f_el is not None else ""


def _formula_refs(text: str) -> frozenset[str]:
    """Return the cells ``text`` reads, falling back to a single-ref and range scan when unsupported.

    The scan expands each ``REF:REF`` range so a write anywhere inside it counts as a dependency;
    a range past the 1000-cell cap raises ``TemplateError`` rather than leave a value stale.
    """
    try:
        return parse(text).refs()
    except UnsupportedFormula:
        refs = {_normalize_ref(match) for match in _CELL_REF_RE.findall(text)}
        try:
            for start, end in _RANGE_REF_RE.findall(text):
                refs.update(range_refs(start, end))
        except UnsupportedFormula as exc:
            raise TemplateError(f"cannot scan formula {text!r}: {exc}") from exc
        return frozenset(refs)


def _reject_shared_or_array(f_el: etree._Element | None, ref: str) -> None:
    """Refuse a shared or array formula (its master or a copy without text)."""
    if f_el is None:
        return
    shared_or_array = f_el.get("t") in {"shared", "array"}
    empty_with_ref = f_el.get("ref") is not None and not (f_el.text or "").strip()
    if shared_or_array or empty_with_ref:
        raise TemplateError(f"{ref} has a shared or array formula, which is not supported")


def _rewrite_calc_chain(parts: list[tuple[zipfile.ZipInfo, bytes]], dropped: set[str]) -> bytes | None:
    """Return the rewritten ``calcChain`` part, ``None`` when nothing changes, or raise if empty."""
    if not dropped:
        return None
    if not any(info.filename == _CALC_CHAIN_PART for info, _ in parts):
        return None
    original = _part_bytes(parts, _CALC_CHAIN_PART)
    root = _parse_xml(original)
    entries = root.findall(f"{{{_MAIN_NS}}}c")
    kept = [entry for entry in entries if entry.get("r") not in dropped]
    if len(kept) == len(entries):
        return None
    if not kept:
        # shortcut: Excel needs at least one calcChain entry, and removing the part would
        # touch the workbook rels and content types, so refuse instead.
        raise TemplateError("calcChain would be empty")
    for entry in entries:
        if entry.get("r") in dropped:
            root.remove(entry)
    return _serialize(root)


def _write_package(parts: list[tuple[zipfile.ZipInfo, bytes]], replacements: Mapping[str, bytes]) -> bytes:
    """Write the package, reusing each entry's original ``ZipInfo`` and order."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for info, content in parts:
            package.writestr(info, replacements.get(info.filename, content))
    return buffer.getvalue()


def _normalize_ref(ref: str) -> str:
    """Return a cell reference without ``$`` and with an upper-case column."""
    return ref.replace("$", "").upper()


def _split_ref(ref: str) -> tuple[int, int]:
    """Return the ``(column, row)`` numbers of a cell reference (A is column 1)."""
    normalized = _normalize_ref(ref)
    letters = normalized.rstrip("0123456789")
    column = 0
    for character in letters:
        column = column * 26 + (ord(character) - ord("A") + 1)
    return column, int(normalized[len(letters) :])
