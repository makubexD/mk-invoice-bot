"""Synthetic Excel template builder for the Excel sink tests (R-7.2, D71).

Builds a small but valid SpreadsheetML package in memory with the standard library only. Every value is
invented: no real RUC, invoice, name or photo. Zip entries carry a fixed timestamp, so the bytes are stable.
"""

from __future__ import annotations

import io
import struct
import zipfile
import zlib

SHEET_NAME = "Template"

CELL_MOTIVO = "C13"
CELL_FECHA = "B16"
CELL_DESCRIPCION = "C16"
CELL_SERIE_NUMERO = "E16"
CELL_BASE = "F16"
CELL_IGV = "G16"
CELL_SERVICE_CHARGE = "H16"
CELL_TOTAL = "I16"
CELL_TOTAL_CAPPED = "J16"
CELL_UNRELATED_FORMULA = "K20"

CONTENT_TYPES_PART = "[Content_Types].xml"
PACKAGE_RELS_PART = "_rels/.rels"
CORE_PROPERTIES_PART = "docProps/core.xml"
WORKBOOK_PART = "xl/workbook.xml"
WORKBOOK_RELS_PART = "xl/_rels/workbook.xml.rels"
STYLES_PART = "xl/styles.xml"
SHARED_STRINGS_PART = "xl/sharedStrings.xml"
SHEET_PART = "xl/worksheets/sheet1.xml"
SHEET_RELS_PART = "xl/worksheets/_rels/sheet1.xml.rels"
DRAWING_PART = "xl/drawings/drawing1.xml"
IMAGE_PART = "xl/media/image1.png"
CALC_CHAIN_PART = "xl/calcChain.xml"
CUSTOM_XML_PART = "customXml/item1.xml"
PRINTER_SETTINGS_PART = "xl/printerSettings/printerSettings1.bin"

ALL_PARTS = (
    CONTENT_TYPES_PART,
    PACKAGE_RELS_PART,
    CORE_PROPERTIES_PART,
    WORKBOOK_PART,
    WORKBOOK_RELS_PART,
    STYLES_PART,
    SHARED_STRINGS_PART,
    SHEET_PART,
    SHEET_RELS_PART,
    DRAWING_PART,
    IMAGE_PART,
    CALC_CHAIN_PART,
    CUSTOM_XML_PART,
    PRINTER_SETTINGS_PART,
)

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CORE_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_XDR_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_SML_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml"
_PKG_MIME = "application/vnd.openxmlformats-package"

_TEMPLATE_DATE_TIME = (2026, 1, 1, 0, 0, 0)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'

_CONTENT_TYPES_XML = f"""{_XML_DECLARATION}
<Types xmlns="{_PKG_MIME}/content-types">
  <Default Extension="rels" ContentType="{_PKG_MIME}.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Default Extension="bin" ContentType="{_SML_MIME}.printerSettings"/>
  <Override PartName="/{WORKBOOK_PART}" ContentType="{_SML_MIME}.sheet.main+xml"/>
  <Override PartName="/{SHEET_PART}" ContentType="{_SML_MIME}.worksheet+xml"/>
  <Override PartName="/{STYLES_PART}" ContentType="{_SML_MIME}.styles+xml"/>
  <Override PartName="/{SHARED_STRINGS_PART}" ContentType="{_SML_MIME}.sharedStrings+xml"/>
  <Override PartName="/{CALC_CHAIN_PART}" ContentType="{_SML_MIME}.calcChain+xml"/>
  <Override PartName="/{DRAWING_PART}"
      ContentType="application/vnd.openxmlformats-officedocument.drawing+xml"/>
  <Override PartName="/{CORE_PROPERTIES_PART}" ContentType="{_PKG_MIME}.core-properties+xml"/>
</Types>
"""

_PACKAGE_RELS_XML = f"""{_XML_DECLARATION}
<Relationships xmlns="{_PKG_REL_NS}">
  <Relationship Id="rId1" Type="{_REL_NS}/officeDocument" Target="{WORKBOOK_PART}"/>
  <Relationship Id="rId2" Type="{_PKG_REL_NS}/metadata/core-properties"
      Target="{CORE_PROPERTIES_PART}"/>
</Relationships>
"""

_CORE_PROPERTIES_XML = f"""{_XML_DECLARATION}
<cp:coreProperties xmlns:cp="{_CORE_NS}" xmlns:dc="{_DC_NS}">
  <dc:creator>synthetic</dc:creator>
  <cp:lastModifiedBy>synthetic</cp:lastModifiedBy>
</cp:coreProperties>
"""

_WORKBOOK_XML = f"""{_XML_DECLARATION}
<workbook xmlns="{_MAIN_NS}" xmlns:r="{_REL_NS}">
  <sheets>
    <sheet name="{SHEET_NAME}" sheetId="1" r:id="rId1"/>
  </sheets>
  <calcPr fullCalcOnLoad="1"/>
</workbook>
"""

_WORKBOOK_RELS_XML = f"""{_XML_DECLARATION}
<Relationships xmlns="{_PKG_REL_NS}">
  <Relationship Id="rId1" Type="{_REL_NS}/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="{_REL_NS}/styles" Target="styles.xml"/>
  <Relationship Id="rId3" Type="{_REL_NS}/sharedStrings" Target="sharedStrings.xml"/>
  <Relationship Id="rId4" Type="{_REL_NS}/calcChain" Target="calcChain.xml"/>
</Relationships>
"""

_STYLES_XML = f"""{_XML_DECLARATION}
<styleSheet xmlns="{_MAIN_NS}">
  <fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="2">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
  </fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="2">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
  </cellXfs>
</styleSheet>
"""

_SHARED_STRINGS_XML = f"""{_XML_DECLARATION}
<sst xmlns="{_MAIN_NS}" count="3" uniqueCount="3">
  <si><t>Support release</t></si>
  <si><t>Comercio</t></si>
  <si><t>Descripcion</t></si>
</sst>
"""

_SHEET_XML = f"""{_XML_DECLARATION}
<worksheet xmlns="{_MAIN_NS}" xmlns:r="{_REL_NS}">
  <dimension ref="{CELL_MOTIVO}:{CELL_UNRELATED_FORMULA}"/>
  <sheetData>
    <row r="13"><c r="{CELL_MOTIVO}" t="s"><v>0</v></c></row>
    <row r="16">
      <c r="{CELL_FECHA}" s="1"/><c r="{CELL_DESCRIPCION}" s="1"/><c r="{CELL_SERIE_NUMERO}" s="1"/>
      <c r="{CELL_BASE}"><v>40</v></c><c r="{CELL_IGV}"><v>7.2</v></c>
      <c r="{CELL_SERVICE_CHARGE}"><f>{CELL_BASE}*0.1</f><v>4</v></c>
      <c r="{CELL_TOTAL}"><f>{CELL_BASE}+{CELL_IGV}+{CELL_SERVICE_CHARGE}</f><v>51.2</v></c>
      <c r="{CELL_TOTAL_CAPPED}"><f>MIN({CELL_TOTAL}, 50)</f><v>50</v></c>
    </row>
    <row r="20"><c r="{CELL_UNRELATED_FORMULA}"><f>1+1</f><v>2</v></c></row>
  </sheetData>
  <pageSetup r:id="rId2"/>
  <drawing r:id="rId1"/>
</worksheet>
"""

_SHEET_RELS_XML = f"""{_XML_DECLARATION}
<Relationships xmlns="{_PKG_REL_NS}">
  <Relationship Id="rId1" Type="{_REL_NS}/drawing" Target="../drawings/drawing1.xml"/>
  <Relationship Id="rId2" Type="{_REL_NS}/printerSettings"
      Target="../printerSettings/printerSettings1.bin"/>
</Relationships>
"""

_DRAWING_XML = f"""{_XML_DECLARATION}
<xdr:wsDr xmlns:xdr="{_XDR_NS}" xmlns:a="{_A_NS}" xmlns:r="{_REL_NS}">
  <xdr:twoCellAnchor>
    <xdr:from><xdr:col>0</xdr:col><xdr:colOff>0</xdr:colOff>
      <xdr:row>0</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>
    <xdr:to><xdr:col>2</xdr:col><xdr:colOff>0</xdr:colOff>
      <xdr:row>2</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>
    <xdr:sp>
      <xdr:nvSpPr>
        <xdr:cNvPr id="2" name="Button 1"/>
        <xdr:cNvSpPr/>
      </xdr:nvSpPr>
      <xdr:spPr><a:prstGeom prst="roundRect"><a:avLst/></a:prstGeom></xdr:spPr>
      <xdr:txBody><a:bodyPr/><a:p><a:r><a:t>Print</a:t></a:r></a:p></xdr:txBody>
    </xdr:sp>
    <xdr:clientData/>
  </xdr:twoCellAnchor>
  <xdr:twoCellAnchor>
    <xdr:from><xdr:col>0</xdr:col><xdr:colOff>0</xdr:colOff>
      <xdr:row>3</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>
    <xdr:to><xdr:col>2</xdr:col><xdr:colOff>0</xdr:colOff>
      <xdr:row>5</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>
    <xdr:sp>
      <xdr:nvSpPr>
        <xdr:cNvPr id="3" name="Button 2"/>
        <xdr:cNvSpPr/>
      </xdr:nvSpPr>
      <xdr:spPr><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr>
      <xdr:txBody><a:bodyPr/><a:p><a:r><a:t>Cancel</a:t></a:r></a:p></xdr:txBody>
    </xdr:sp>
    <xdr:clientData/>
  </xdr:twoCellAnchor>
  <xdr:twoCellAnchor>
    <xdr:from><xdr:col>0</xdr:col><xdr:colOff>0</xdr:colOff>
      <xdr:row>6</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>
    <xdr:to><xdr:col>1</xdr:col><xdr:colOff>0</xdr:colOff>
      <xdr:row>7</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>
    <xdr:pic>
      <xdr:nvPicPr>
        <xdr:cNvPr id="4" name="Picture 1"/>
        <xdr:cNvPicPr/>
      </xdr:nvPicPr>
      <xdr:blipFill>
        <a:blip r:embed="rId1"/>
        <a:stretch><a:fillRect/></a:stretch>
      </xdr:blipFill>
      <xdr:spPr>
        <a:xfrm><a:off x="0" y="0"/><a:ext cx="9525" cy="9525"/></a:xfrm>
        <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
      </xdr:spPr>
    </xdr:pic>
    <xdr:clientData/>
  </xdr:twoCellAnchor>
</xdr:wsDr>
"""

_CALC_CHAIN_XML = f"""{_XML_DECLARATION}
<calcChain xmlns="{_MAIN_NS}">
  <c r="{CELL_SERVICE_CHARGE}" i="1"/>
  <c r="{CELL_TOTAL}" i="1"/>
  <c r="{CELL_TOTAL_CAPPED}" i="1"/>
  <c r="{CELL_UNRELATED_FORMULA}" i="1"/>
</calcChain>
"""

_CUSTOM_XML = f"""{_XML_DECLARATION}
<root xmlns="urn:mk-invoice-bot:synthetic">
  <note>Synthetic test fixture. No real data.</note>
</root>
"""

_PRINTER_SETTINGS = b"\x00\x01\x02\x03\x04\x05\x06\x07"


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def _one_pixel_png() -> bytes:
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixel = b"\x00\xff\xff\xff"
    return b"".join(
        (
            _PNG_SIGNATURE,
            _png_chunk(b"IHDR", header),
            _png_chunk(b"IDAT", zlib.compress(pixel, 9)),
            _png_chunk(b"IEND", b""),
        )
    )


_IMAGE_BYTES = _one_pixel_png()


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_TEMPLATE_DATE_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    return info


def build_template() -> bytes:
    """Return the synthetic ``.xlsx`` package as bytes, identical on every call."""
    parts: dict[str, str | bytes] = {
        CONTENT_TYPES_PART: _CONTENT_TYPES_XML,
        PACKAGE_RELS_PART: _PACKAGE_RELS_XML,
        CORE_PROPERTIES_PART: _CORE_PROPERTIES_XML,
        WORKBOOK_PART: _WORKBOOK_XML,
        WORKBOOK_RELS_PART: _WORKBOOK_RELS_XML,
        STYLES_PART: _STYLES_XML,
        SHARED_STRINGS_PART: _SHARED_STRINGS_XML,
        SHEET_PART: _SHEET_XML,
        SHEET_RELS_PART: _SHEET_RELS_XML,
        DRAWING_PART: _DRAWING_XML,
        IMAGE_PART: _IMAGE_BYTES,
        CALC_CHAIN_PART: _CALC_CHAIN_XML,
        CUSTOM_XML_PART: _CUSTOM_XML,
        PRINTER_SETTINGS_PART: _PRINTER_SETTINGS,
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name in ALL_PARTS:
            package.writestr(_zip_info(name), parts[name])
    return buffer.getvalue()
