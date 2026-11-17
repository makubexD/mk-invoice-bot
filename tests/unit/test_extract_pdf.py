"""Tests for reading an invoice PDF's text layer, render and page cap (R-2.15, D122, BR-E07)."""

from __future__ import annotations

import ctypes
import io
import json
from pathlib import Path

import cv2
import numpy as np
import pypdfium2 as pdfium
import pypdfium2.raw as raw
import pytest

from invoicebot.extract import __main__ as helper
from invoicebot.extract.fields import FULL_CONFIDENCE
from invoicebot.extract.ocr import OcrWord
from invoicebot.extract.pdf import TooManyPages, open_pdf, render_gray, text_to_ocr

# Synthetic RUC from tests/synthetic_rucs.txt. No real RUC may appear here.
SYNTHETIC_RUC = "20000000001"
TEXT_LAYER_LINES = (
    f"RUC: {SYNTHETIC_RUC}",
    "FACTURA ELECTRONICA F001-15160",
    "IMPORTE TOTAL S/ 49.90",
)
TEXT_LEFT = 40
FIRST_LINE_TOP = 700
LINE_HEIGHT = 50
FONT_SIZE = 14
TEXT_PAGE = (600, 800)
IMAGE_CANVAS = (400, 1200)  # height, width
IMAGE_PAGE = (600, 200)
DRAW_FONT = cv2.FONT_HERSHEY_SIMPLEX
DRAW_SCALE = 2.0
DRAW_THICKNESS = 3
BLACK = 0
WHITE = 255
MAX_PDF_PAGES = 5
PDF_MIN_TEXT_CHARS = 40
MAX_PX = 2000
MAX_BYTES = 10_000_000
RULES_PATH = "config/parsing_rules.yaml"
HELPER_TIMEOUT_S = 30
HELPER_PSM = 4


def _add_text(page: pdfium.PdfPage, document: pdfium.PdfDocument, text: str, top: int) -> None:
    """Draw ``text`` on ``page`` as real PDF text, so it lands in the page's text layer."""
    font = raw.FPDFText_LoadStandardFont(document.raw, b"Helvetica")
    text_object = raw.FPDFPageObj_CreateTextObj(document.raw, font, FONT_SIZE)
    encoded = (text + "\x00").encode("utf-16-le")
    raw.FPDFText_SetText(text_object, ctypes.cast(encoded, raw.FPDF_WIDESTRING))
    raw.FPDFPageObj_Transform(text_object, 1, 0, 0, 1, TEXT_LEFT, top)
    raw.FPDFPage_InsertObject(page.raw, text_object)


def _write_text_pdf(path: Path) -> None:
    """Write a one-page PDF whose lines are drawn with pdfium's text API."""
    document = pdfium.PdfDocument.new()
    page = document.new_page(width=TEXT_PAGE[0], height=TEXT_PAGE[1])
    for index, line in enumerate(TEXT_LAYER_LINES):
        _add_text(page, document, line, FIRST_LINE_TOP - index * LINE_HEIGHT)
    page.gen_content()
    document.save(str(path))
    document.close()


def _invoice_image() -> np.ndarray:
    """Draw a clean grayscale invoice canvas with a readable RUC and total."""
    canvas = np.full(IMAGE_CANVAS, WHITE, dtype=np.uint8)
    lines = ((f"RUC {SYNTHETIC_RUC}", 140), ("TOTAL S/ 12.50", 300))
    for line, top in lines:
        cv2.putText(canvas, line, (60, top), DRAW_FONT, DRAW_SCALE, BLACK, DRAW_THICKNESS)
    return canvas


def _write_image_pdf(path: Path) -> None:
    """Write a one-page PDF that is one JPEG image and has no text layer."""
    encoded, buffer = cv2.imencode(".jpg", _invoice_image())
    assert encoded
    document = pdfium.PdfDocument.new()
    page = document.new_page(width=IMAGE_PAGE[0], height=IMAGE_PAGE[1])
    image = pdfium.PdfImage.new(document)
    image.load_jpeg(io.BytesIO(buffer.tobytes()), inline=True)
    image.set_matrix(pdfium.PdfMatrix(IMAGE_PAGE[0], 0, 0, IMAGE_PAGE[1], 0, 0))
    page.insert_obj(image)
    page.gen_content()
    document.save(str(path))
    document.close()


def _write_blank_pdf(path: Path, pages: int) -> None:
    """Write a PDF with ``pages`` blank pages."""
    document = pdfium.PdfDocument.new()
    for _ in range(pages):
        document.new_page(width=200, height=200)
    document.save(str(path))
    document.close()


def _argv(path: Path, kind: str) -> list[str]:
    """Build the helper argv a phone or PDF input comes in as (D122)."""
    return [
        str(path),
        str(MAX_PX),
        str(MAX_BYTES),
        "tesseract",
        "spa",
        str(HELPER_PSM),
        str(HELPER_TIMEOUT_S),
        RULES_PATH,
        kind,
        str(MAX_PDF_PAGES),
        str(PDF_MIN_TEXT_CHARS),
    ]


def _run_helper(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], pdf: Path
) -> dict[str, object]:
    """Run the helper over ``pdf`` with resource limits disabled and return its payload."""
    monkeypatch.setattr(helper, "_limit_resources", lambda: None)

    assert helper.main(_argv(pdf, helper.PDF_INPUT_KIND)) == 0

    return json.loads(capsys.readouterr().out)


def test_text_pdf_yields_fields_from_its_text_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R-2.15: a text PDF yields the invoice fields with source ``pdf``."""
    pdf = tmp_path / "invoice.pdf"
    _write_text_pdf(pdf)

    payload = _run_helper(monkeypatch, capsys, pdf)

    assert payload["error"] is None
    fields = payload["reading"]["fields"]
    assert fields["ruc_emisor"]["value"] == SYNTHETIC_RUC
    assert fields["serie"]["value"] == "F001"
    assert fields["numero"]["value"] == "15160"
    assert fields["total"]["value"] == "49.90"
    for name in ("ruc_emisor", "serie", "numero", "total"):
        assert fields[name]["source"] == "pdf"
        assert fields[name]["confidence"] == FULL_CONFIDENCE


def test_image_only_pdf_is_read_through_ocr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R-2.15: a PDF without a text layer goes through OCR and its fields have source ``ocr``."""
    pdf = tmp_path / "scan.pdf"
    _write_image_pdf(pdf)

    payload = _run_helper(monkeypatch, capsys, pdf)

    assert payload["error"] is None
    assert payload["ocr"] is True
    fields = payload["reading"]["fields"]
    assert fields["ruc_emisor"]["source"] == "ocr"
    assert fields["total"]["source"] == "ocr"


def test_open_pdf_six_pages_raises_too_many_pages(tmp_path: Path) -> None:
    """R-2.15: a six-page PDF raises ``TooManyPages`` against the five-page cap."""
    pdf = tmp_path / "long.pdf"
    _write_blank_pdf(pdf, MAX_PDF_PAGES + 1)

    with pytest.raises(TooManyPages, match="6 pages"):
        open_pdf(pdf, MAX_PDF_PAGES)


def test_helper_pdf_over_page_cap_reports_too_many_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R-2.15: a PDF over the page cap becomes the helper's ``too_many_pages`` error."""
    pdf = tmp_path / "long.pdf"
    _write_blank_pdf(pdf, MAX_PDF_PAGES + 1)

    payload = _run_helper(monkeypatch, capsys, pdf)

    assert payload["error"] == "too_many_pages"
    assert payload["reading"] is None


def test_open_pdf_random_bytes_raise_value_error(tmp_path: Path) -> None:
    """R-2.15: bytes that are not a PDF raise ``ValueError`` (unreadable)."""
    pdf = tmp_path / "random.pdf"
    pdf.write_bytes(b"not a pdf at all" * 100)

    with pytest.raises(ValueError, match="readable PDF"):
        open_pdf(pdf, MAX_PDF_PAGES)


def test_text_to_ocr_splits_lines_and_words_at_full_confidence() -> None:
    """R-2.15: a text layer becomes OCR lines of words, each word at full confidence."""
    ocr_text = text_to_ocr("RUC: 20000000001\r\nFACTURA ELECTRONICA\r\n\r\n")

    assert ocr_text.lines == (
        (
            OcrWord(text="RUC:", confidence=FULL_CONFIDENCE),
            OcrWord(text="20000000001", confidence=FULL_CONFIDENCE),
        ),
        (
            OcrWord(text="FACTURA", confidence=FULL_CONFIDENCE),
            OcrWord(text="ELECTRONICA", confidence=FULL_CONFIDENCE),
        ),
    )


def test_render_gray_page_one_is_a_gray_uint8_array(tmp_path: Path) -> None:
    """R-2.15: page 1 renders to a detached two-dimensional ``uint8`` array."""
    pdf = tmp_path / "invoice.pdf"
    _write_text_pdf(pdf)
    document = open_pdf(pdf, MAX_PDF_PAGES)
    try:
        gray = render_gray(document, dpi=72)
    finally:
        document.close()

    assert gray.dtype == np.uint8
    assert gray.ndim == 2
    assert gray.shape == (TEXT_PAGE[1], TEXT_PAGE[0])
