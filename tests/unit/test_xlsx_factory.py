"""Tests for the synthetic Excel template factory (R-7.2)."""

from __future__ import annotations

import io
import zipfile

import pytest
from lxml import etree

from xlsx_factory import ALL_PARTS, DRAWING_PART, IMAGE_PART, build_template

_XDR_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@pytest.fixture(scope="module")
def template_bytes() -> bytes:
    return build_template()


def test_template_opens_with_zipfile(template_bytes: bytes) -> None:
    """R-7.2: the synthetic template is a readable zip package."""
    with zipfile.ZipFile(io.BytesIO(template_bytes)) as package:
        assert package.testzip() is None


def test_template_contains_every_part(template_bytes: bytes) -> None:
    """R-7.2: every part the fixture promises is present in the package."""
    with zipfile.ZipFile(io.BytesIO(template_bytes)) as package:
        assert set(package.namelist()) == set(ALL_PARTS)
        assert len(package.namelist()) == len(ALL_PARTS)


def test_template_xml_parts_parse_with_lxml(template_bytes: bytes) -> None:
    """R-7.2: every XML and relationship part is well-formed."""
    xml_parts = [name for name in ALL_PARTS if name.endswith((".xml", ".rels"))]

    with zipfile.ZipFile(io.BytesIO(template_bytes)) as package:
        for name in xml_parts:
            etree.fromstring(package.read(name))


def test_template_drawing_has_two_shapes_and_one_picture(template_bytes: bytes) -> None:
    """R-7.2: the drawing holds exactly two shapes and one picture."""
    with zipfile.ZipFile(io.BytesIO(template_bytes)) as package:
        drawing = etree.fromstring(package.read(DRAWING_PART))

    namespaces = {"xdr": _XDR_NS}
    assert len(drawing.findall(".//xdr:sp", namespaces)) == 2
    assert len(drawing.findall(".//xdr:pic", namespaces)) == 1


def test_template_image_is_a_png(template_bytes: bytes) -> None:
    """R-7.2: the embedded image starts with the PNG signature."""
    with zipfile.ZipFile(io.BytesIO(template_bytes)) as package:
        assert package.read(IMAGE_PART).startswith(_PNG_SIGNATURE)
