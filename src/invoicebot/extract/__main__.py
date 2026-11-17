"""Extract helper process: read one invoice photo or PDF and print one JSON line (D78, D122).

The extract handler starts this module as ``python -m invoicebot.extract <path> <max_px>
<max_bytes> <engine> <lang> <psm> <timeout_s> <rules_path>``, optionally followed by
``<kind> <max_pdf_pages> <pdf_min_text_chars>`` where ``kind`` is ``photo`` or ``pdf``. With no
trailing arguments the input is a photo, which keeps the original call working. It loads the
input once, reads its SUNAT QR, reads its text and parses the fields with the configured rules,
then merges both readings (D81). A text-layer PDF is parsed directly and its fields carry the
``pdf`` source; a PDF without enough text is rendered on page 1 and read as a photo (R-2.15).
It writes exactly one JSON object to stdout: ``{"reading": {...} | null, "variant": ...,
"decoder": ..., "ocr": bool, "error": null | "unreadable" | "too_many_pages"}``, where ``ocr``
says whether Tesseract returned text. OpenCV, Tesseract, zxing and pypdfium2 live here and are
released when the process exits (D78). Nothing else is printed: stderr is discarded by the
handler because it could carry decoded text (R-9.16). A path or rules file that cannot be read
exits non-zero, so the caller raises and the queue retries. The process caps its own address
space, CPU time and file writes before it decodes anything, and an input above the byte cap
prints the same "unreadable" result as an undecodable image. An OpenCV error while reading the
image, or an unreadable PDF, is reported the same way, as an unreadable input (BR-E01).
"""

from __future__ import annotations

import json
import resource
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import cv2
import yaml
from pypdfium2 import PdfDocument

from invoicebot.config.models import Ocr
from invoicebot.extract.fields import FULL_CONFIDENCE, Reading, reading_to_json
from invoicebot.extract.image import ImageUnreadable, load_gray
from invoicebot.extract.merge import merge
from invoicebot.extract.ocr import preprocess, read_text
from invoicebot.extract.ocr_parse import parse_ocr
from invoicebot.extract.parsing_rules import ParsingRules, load_rules
from invoicebot.extract.pdf import TooManyPages, open_pdf, page_text, render_gray, text_to_ocr
from invoicebot.extract.qr import QrHit, read_invoice_qr

#: Exit code for a missing file or a bad invocation; the handler retries these (R-9.4).
EXIT_FAILURE = 1
#: Photo, pixel cap, byte cap, OCR engine, language, PSM, timeout and rules path.
ARGUMENT_COUNT = 8
#: Optional input kind, PDF page cap and text-layer threshold after those arguments (D122).
OPTIONAL_ARGUMENT_COUNT = 3
DEFAULT_INPUT_KIND = "photo"
PDF_INPUT_KIND = "pdf"
INPUT_KINDS = frozenset({DEFAULT_INPUT_KIND, PDF_INPUT_KIND})
#: Values stored for the PDF options when the older photo-only invocation omits them.
UNUSED_PDF_OPTION = 0
#: The helper's value for "there is no reading to return" (BR-E01).
UNREADABLE = "unreadable"
#: The helper's value for "the PDF has too many pages" (BR-E07, R-2.15).
TOO_MANY_PAGES = "too_many_pages"
#: Address space one decode may use (R-9.1). OpenCV maps about 1.5 GiB for its bundled
#: codecs before it decodes one pixel, and its thread pool reserves more per core, so a
#: 512 MiB cap makes every decode fail to allocate. This value leaves headroom over the
#: measured peak while still stopping a runaway allocation.
MAX_ADDRESS_SPACE_BYTES = 3 * 1024 * 1024 * 1024
#: CPU seconds the helper may burn before the kernel ends it (R-9.2).
MAX_CPU_SECONDS = 60
#: The helper writes to stdout only; a file write means something is wrong (R-9.2).
MAX_FILE_BYTES = 0


@dataclass(frozen=True)
class _Options:
    """The validated argv of one helper run (D122)."""

    path: Path
    max_px: int
    max_bytes: int
    ocr: Ocr
    rules: ParsingRules
    kind: str
    max_pdf_pages: int
    pdf_min_text_chars: int


def _limit_resources() -> None:
    """Cap address space, CPU time and file writes before any byte is decoded (R-9.1, R-9.2)."""
    resource.setrlimit(resource.RLIMIT_AS, (MAX_ADDRESS_SPACE_BYTES, MAX_ADDRESS_SPACE_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (MAX_CPU_SECONDS, MAX_CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES, MAX_FILE_BYTES))


def main(argv: list[str] | None = None) -> int:
    """Read the input named by ``argv`` and print the helper result (R-2.1, R-2.15)."""
    # Single-threaded cv2: a preexec_fn after fork must not meet a lock held by a cv2 worker
    # thread, or the child deadlocks (R-9.2).
    cv2.setNumThreads(0)
    _limit_resources()
    args = sys.argv[1:] if argv is None else argv
    options = _parse_arguments(args)
    if options is None:
        return EXIT_FAILURE
    try:
        if options.path.stat().st_size > options.max_bytes:
            _emit(_unreadable(ocr=False))
            return 0
        payload = _read_invoice(options)
    except OSError:
        return EXIT_FAILURE
    _emit(payload)
    return 0


def _parse_arguments(args: list[str]) -> _Options | None:
    """Return ``args`` as options, or ``None`` when they are not a valid invocation."""
    trailing = _trailing_options(args)
    if trailing is None:
        return None
    kind, max_pdf_pages, pdf_min_text_chars = trailing
    try:
        max_px = int(args[1])
        max_bytes = int(args[2])
        ocr_cfg = Ocr(engine=args[3], lang=args[4], psm=int(args[5]), timeout_s=int(args[6]))
        rules = load_rules(Path(args[7]))
    except (OSError, ValueError, yaml.YAMLError):
        return None
    return _Options(
        path=Path(args[0]),
        max_px=max_px,
        max_bytes=max_bytes,
        ocr=ocr_cfg,
        rules=rules,
        kind=kind,
        max_pdf_pages=max_pdf_pages,
        pdf_min_text_chars=pdf_min_text_chars,
    )


def _trailing_options(args: list[str]) -> tuple[str, int, int] | None:
    """Return the input kind and PDF caps, defaulting to a photo when they are absent (D122)."""
    if len(args) == ARGUMENT_COUNT:
        return DEFAULT_INPUT_KIND, UNUSED_PDF_OPTION, UNUSED_PDF_OPTION
    if len(args) != ARGUMENT_COUNT + OPTIONAL_ARGUMENT_COUNT:
        return None
    kind = args[ARGUMENT_COUNT]
    try:
        max_pdf_pages = int(args[ARGUMENT_COUNT + 1])
        pdf_min_text_chars = int(args[ARGUMENT_COUNT + 2])
    except ValueError:
        return None
    if kind not in INPUT_KINDS or max_pdf_pages < 1 or pdf_min_text_chars < 0:
        return None
    return kind, max_pdf_pages, pdf_min_text_chars


def _read_invoice(options: _Options) -> dict[str, Any]:
    """Read the input as a photo or a PDF and return the helper result (R-2.1, R-2.15)."""
    if options.kind == PDF_INPUT_KIND:
        return _read_pdf(options)
    return _read_photo(options.path, options.max_px, options.ocr, options.rules)


def _read_photo(path: Path, max_px: int, ocr_cfg: Ocr, rules: ParsingRules) -> dict[str, Any]:
    """Read one photo as before and return the helper result (R-2.1, R-2.13)."""
    try:
        gray = load_gray(path.read_bytes(), max_px)
        hit = read_invoice_qr(gray)
        processed = preprocess(gray)
    except (ImageUnreadable, cv2.error):
        return _unreadable(ocr=False)
    text = read_text(processed, ocr_cfg)
    ocr_reading = None if text is None else parse_ocr(text, rules).reading
    reading = merge(ocr_reading, None if hit is None else hit.reading)
    return _payload(reading, hit, ocr_ran=text is not None)


def _read_pdf(options: _Options) -> dict[str, Any]:
    """Read page 1 of a PDF from its text layer or, failing that, its render (R-2.15, D122)."""
    try:
        document = open_pdf(options.path, options.max_pdf_pages)
    except TooManyPages:
        return _too_many_pages()
    except (ValueError, MemoryError):
        return _unreadable(ocr=False)
    try:
        return _pdf_payload(document, options)
    except MemoryError:
        return _unreadable(ocr=False)
    finally:
        document.close()


def _pdf_payload(document: PdfDocument, options: _Options) -> dict[str, Any]:
    """Merge the page-1 QR with the text layer or the OCR of its render (R-2.15, D122)."""
    try:
        layer = page_text(document)
        gray = render_gray(document, options.max_px)
        hit = read_invoice_qr(gray)
    except (ValueError, cv2.error):
        return _unreadable(ocr=False)
    qr_reading = None if hit is None else hit.reading
    if _non_space_chars(layer) >= options.pdf_min_text_chars:
        return _payload(merge(_pdf_reading(layer, options.rules), qr_reading), hit, ocr_ran=False)
    try:
        processed = preprocess(gray)
    except cv2.error:
        return _unreadable(ocr=False)
    text = read_text(processed, options.ocr)
    ocr_reading = None if text is None else parse_ocr(text, options.rules).reading
    return _payload(merge(ocr_reading, qr_reading), hit, ocr_ran=text is not None)


def _pdf_reading(text: str, rules: ParsingRules) -> Reading:
    """Parse a text layer and mark every field it yields as read from the page (D122, R-2.15)."""
    reading = parse_ocr(text_to_ocr(text), rules).reading
    fields = {
        name: replace(field, source="pdf", confidence=FULL_CONFIDENCE)
        for name, field in reading.fields.items()
    }
    return Reading(fields=fields, document_url=reading.document_url)


def _non_space_chars(text: str) -> int:
    """Return how many characters of ``text`` are not whitespace (R-2.15)."""
    return sum(1 for char in text if not char.isspace())


def _payload(reading: Reading, hit: QrHit | None, *, ocr_ran: bool) -> dict[str, Any]:
    """Return the helper's result object for ``reading`` and its QR hit (R-2.1, R-2.15)."""
    if not reading.fields:
        return _unreadable(ocr=ocr_ran)
    return {
        "reading": reading_to_json(reading),
        "variant": None if hit is None else hit.variant,
        "decoder": None if hit is None else hit.decoder,
        "ocr": ocr_ran,
        "error": None,
    }


def _unreadable(*, ocr: bool) -> dict[str, Any]:
    """Return the result for an input where neither reader found a field."""
    return {"reading": None, "variant": None, "decoder": None, "ocr": ocr, "error": UNREADABLE}


def _too_many_pages() -> dict[str, Any]:
    """Return the result for a PDF over the page cap (BR-E07, R-2.15)."""
    return {"reading": None, "variant": None, "decoder": None, "ocr": False, "error": TOO_MANY_PAGES}


def _emit(payload: dict[str, Any]) -> None:
    """Write the result as the helper's one stdout line."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
