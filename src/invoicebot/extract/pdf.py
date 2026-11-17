"""Read an invoice PDF's text layer, page count and page-1 render (R-2.15, D122).

The untrusted file is opened only inside the extract helper process, with ``pypdfium2``: one
wheel, no system library, and its rlimits already apply. A document over the page cap is
refused before any page is read. A page whose text layer is empty yields no words, which is
what sends the caller to the OCR path; a rendering is returned as a detached array so it stays
valid after the bitmap is released. Any ``pypdfium2`` failure becomes ``ValueError``, the same
"unreadable" answer the photo path gives (BR-E01).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import cast

import numpy as np
from pypdfium2 import PdfDocument, PdfiumError

from invoicebot.extract.fields import FULL_CONFIDENCE
from invoicebot.extract.ocr import OcrText, OcrWord

#: PDF user-space units per inch; pdfium renders at a scale relative to this (D122).
PDF_POINTS_PER_INCH = 72
#: Resolution page 1 is rendered at for the QR cascade and the OCR fallback (D122).
DEFAULT_RENDER_DPI = 200
UNREADABLE_MESSAGE = "bytes are not a readable PDF"


class TooManyPages(ValueError):
    """The document has more pages than the configured cap (BR-E07, D122)."""


def open_pdf(path: str | Path, max_pages: int) -> PdfDocument:
    """Open ``path`` and return it, or raise when it is unreadable or too long (D122).

    Raises:
        TooManyPages: when the document has more than ``max_pages`` pages.
        ValueError: when the bytes are not a readable PDF.
    """
    try:
        document = PdfDocument(path)
        page_count = len(document)
    except PdfiumError as err:
        raise ValueError(UNREADABLE_MESSAGE) from err
    if page_count == 0:
        document.close()
        raise ValueError(UNREADABLE_MESSAGE)
    if page_count > max_pages:
        document.close()
        raise TooManyPages(f"document has {page_count} pages, at most {max_pages} allowed")
    return document


def page_text(document: PdfDocument) -> str:
    """Return page 1's text layer, or an empty string when the page carries no text (D122).

    Raises:
        ValueError: when pdfium cannot read the page.
    """
    try:
        text_page = document[0].get_textpage()
        return cast(str, text_page.get_text_range())
    except PdfiumError as err:
        raise ValueError(UNREADABLE_MESSAGE) from err


def render_gray(document: PdfDocument, max_px: int, dpi: int = DEFAULT_RENDER_DPI) -> np.ndarray:
    """Render page 1 as a detached grayscale ``uint8`` array at ``dpi`` (D122, R-9.1).

    A page whose long side at ``dpi`` would exceed ``max_px`` pixels is rendered at a lower
    scale, the same long-side cap a photo gets, so a huge page cannot allocate a huge bitmap.

    Args:
        document: the open PDF whose page 1 is rendered.
        max_px: the long-side pixel cap the photo path also uses (R-9.1).
        dpi: resolution to render at when the page fits under ``max_px``.

    Raises:
        ValueError: when pdfium cannot read the page or render it.
    """
    try:
        width, height = document[0].get_size()
    except PdfiumError as err:
        raise ValueError(UNREADABLE_MESSAGE) from err
    if width <= 0 or height <= 0:
        raise ValueError(UNREADABLE_MESSAGE)
    # pdfium rounds each side up, so a scale a hair under the exact fit keeps the long side at max_px.
    scale = min(dpi / PDF_POINTS_PER_INCH, math.nextafter(max_px / max(width, height), 0.0))
    try:
        bitmap = document[0].render(scale=scale, grayscale=True)
        return np.array(bitmap.to_numpy(), dtype=np.uint8, copy=True)
    except PdfiumError as err:
        raise ValueError(UNREADABLE_MESSAGE) from err


def text_to_ocr(text: str) -> OcrText:
    """Return ``text`` as the lines and words the OCR parser reads, all at full confidence (D122).

    A text layer is already exact, so every word it yields is certain; blank lines carry no
    words and are dropped.
    """
    lines: list[tuple[OcrWord, ...]] = []
    for raw_line in text.splitlines():
        words = _words(raw_line)
        if words:
            lines.append(words)
    return OcrText(lines=tuple(lines))


def _words(line: str) -> tuple[OcrWord, ...]:
    """Split ``line`` on whitespace into words at full confidence."""
    return tuple(OcrWord(text=word, confidence=FULL_CONFIDENCE) for word in line.split())
