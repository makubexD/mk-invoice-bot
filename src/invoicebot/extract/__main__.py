"""Extract helper process: read one invoice photo or PDF from stdin and print one JSON line (D78, D122).

The extract handler starts this module as ``python -m invoicebot.extract <max_px> <max_bytes>
<engine> <lang> <psm> <timeout_s> <rules_path>``, optionally followed by
``<kind> <max_pdf_pages> <pdf_min_text_chars>`` where ``kind`` is ``photo`` or ``pdf``. With no
trailing arguments the input is a photo, which keeps the original call working. The file's bytes
arrive on stdin and the helper never opens a path under the data root (D158). It loads the
input once, reads its SUNAT QR, reads its text and parses the fields with the configured rules,
then merges both readings (D81). A photo whose first read misses a required field is read again
on the plain deskewed photo, then on that photo upscaled, then on the upscaled photo in block
mode, then on a crop of the receipt, within the OCR budget (D198).
A text-layer PDF is parsed directly and its fields carry the
``pdf`` source; a PDF without enough text is rendered on page 1 and read as a photo (R-2.15).
It writes exactly one JSON object to stdout: ``{"reading": {...} | null, "variant": ...,
"decoder": ..., "ocr": bool, "error": null | "unreadable" | "too_many_pages"}``, where ``ocr``
says whether Tesseract returned text. OpenCV, Tesseract, zxing and pypdfium2 live here and are
released when the process exits (D78). Nothing else is printed: stderr is discarded by the
handler because it could carry decoded text (R-9.16). A rules file that cannot be read exits
non-zero, so the caller raises and the queue retries. The process caps its own address space,
CPU time and file writes before it decodes anything, and an input above the byte cap prints the
same "unreadable" result as an undecodable image. An OpenCV error while reading the image, or an
unreadable PDF, is reported the same way, as an unreadable input (BR-E01).
"""

from __future__ import annotations

import json
import resource
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml
from pypdfium2 import PdfDocument

from invoicebot.config.models import Ocr
from invoicebot.extract.fields import FULL_CONFIDENCE, Reading, reading_to_json
from invoicebot.extract.image import ImageUnreadable, crop_receipt, load_gray
from invoicebot.extract.merge import fill_missing, merge
from invoicebot.extract.ocr import OcrText, binarize, deskew, preprocess, read_text
from invoicebot.extract.ocr_parse import parse_ocr
from invoicebot.extract.parsing_rules import ParsingRules, load_rules
from invoicebot.extract.pdf import TooManyPages, open_pdf, page_text, render_gray, text_to_ocr
from invoicebot.extract.qr import QrHit, read_invoice_qr
from invoicebot.extract.url_strip import STRIP_TIMEOUT_S, read_url_strip

#: Exit code for a bad invocation or an unreadable rules file; the handler retries these (R-9.4).
EXIT_FAILURE = 1
#: Pixel cap, byte cap, OCR engine, language, PSM, timeout and rules path.
ARGUMENT_COUNT = 7
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
#: Seconds of OCR budget below which a retry read is not started (D198).
MIN_RETRY_S = 1.0
#: The upscaled plain retry scales the deskewed photo by this factor, capped so its long side stays within
#: ``UPSCALE_MAX_SIDE`` pixels (D198).
UPSCALE_FACTOR = 2.0
UPSCALE_MAX_SIDE = 4000
#: The page segmentation mode of the block-mode retry read of the upscaled photo; it reads the page as one
#: uniform block of text, which can catch what the configured mode splits wrongly (D198).
RETRY_PSM = 6
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
    """Read the input from stdin and print the helper result (R-2.1, R-2.15, D158)."""
    # Single-threaded cv2: a preexec_fn after fork must not meet a lock held by a cv2 worker
    # thread, or the child deadlocks (R-9.2).
    cv2.setNumThreads(0)
    _limit_resources()
    args = sys.argv[1:] if argv is None else argv
    options = _parse_arguments(args)
    if options is None:
        return EXIT_FAILURE
    # One byte past the cap is enough to tell an oversized input from one that fits.
    data = sys.stdin.buffer.read(options.max_bytes + 1)
    if len(data) > options.max_bytes:
        _emit(_unreadable(ocr=False))
        return 0
    _emit(_read_invoice(data, options))
    return 0


def _parse_arguments(args: list[str]) -> _Options | None:
    """Return ``args`` as options, or ``None`` when they are not a valid invocation."""
    trailing = _trailing_options(args)
    if trailing is None:
        return None
    kind, max_pdf_pages, pdf_min_text_chars = trailing
    try:
        max_px = int(args[0])
        max_bytes = int(args[1])
        ocr_cfg = Ocr(engine=args[2], lang=args[3], psm=int(args[4]), timeout_s=int(args[5]))
        rules = load_rules(Path(args[6]))
    except (OSError, ValueError, yaml.YAMLError):
        return None
    return _Options(
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


def _read_invoice(data: bytes, options: _Options) -> dict[str, Any]:
    """Read the input as a photo or a PDF and return the helper result (R-2.1, R-2.15)."""
    if options.kind == PDF_INPUT_KIND:
        return _read_pdf(data, options)
    return _read_photo(data, options.max_px, options.ocr, options.rules)


def _read_photo(data: bytes, max_px: int, ocr_cfg: Ocr, rules: ParsingRules) -> dict[str, Any]:
    """Read one photo from its bytes and return the helper result (R-2.1, R-2.13, D198)."""
    started = time.monotonic()
    try:
        gray = load_gray(data, max_px)
        hit = read_invoice_qr(gray)
        straight = deskew(gray)
        processed = binarize(straight)
    except (ImageUnreadable, cv2.error, MemoryError):
        return _unreadable(ocr=False)
    try:
        text = read_text(processed, ocr_cfg)
        ocr_reading = None if text is None else parse_ocr(text, rules).reading
    except MemoryError:
        # OCR ran out of memory: keep any QR reading, as when OCR does not run (BR-E01).
        text, ocr_reading = None, None
    qr_reading = None if hit is None else hit.reading
    text, ocr_reading, source = _retries(
        gray, ocr_cfg, rules, started, (text, ocr_reading, straight), qr_reading
    )
    if text is not None and ocr_reading is not None and ocr_reading.printed_url is None:
        ocr_reading = _with_url_strip(source, text, ocr_cfg, rules, ocr_reading)
    reading = merge(ocr_reading, qr_reading)
    return _payload(reading, hit, ocr_ran=text is not None)


def _missing_count(ocr: Reading | None, qr: Reading | None) -> int:
    """Return how many required fields the OCR and QR readings together lack (D198).

    A field the QR reading has is not missing; no reading at all lacks every field.
    """
    return len(merge(ocr, qr).missing_required())


#: One retry read: the image to read, its deskewed image and the PSM to read it with
#: (``None`` is the configured one).
_Candidate = tuple[np.ndarray, np.ndarray, int | None]


def _crop_candidates(gray: np.ndarray) -> Iterator[_Candidate]:
    """Yield the (image to read, deskewed image, psm) candidates of the receipt crop of ``gray`` (D198).

    Nothing is yielded when the crop finds no receipt (it keeps the shape of ``gray``) or fails.
    The binarized crop comes first, then the deskewed crop as it is.
    """
    try:
        crop = crop_receipt(gray)
        if crop.shape == gray.shape:
            return
        straight = deskew(crop)
    except (cv2.error, MemoryError):
        return
    try:
        binarized = binarize(straight)
    except (cv2.error, MemoryError):
        pass
    else:
        yield binarized, straight, None
    yield straight, straight, None


def _upscaled_candidates(straight: np.ndarray) -> Iterator[_Candidate]:
    """Yield ``straight`` upscaled twice, or nothing when it cannot grow (D198).

    The first candidate uses the configured PSM and the second ``RETRY_PSM``; the photo is resized once.

    The factor is ``UPSCALE_FACTOR``, capped so the long side stays within ``UPSCALE_MAX_SIDE``; a
    factor of 1 or less, or a failed resize, yields nothing.
    """
    height, width = straight.shape[:2]
    factor = min(UPSCALE_FACTOR, UPSCALE_MAX_SIDE / max(height, width))
    if factor <= 1:
        return
    size = (round(width * factor), round(height * factor))
    try:
        upscaled = cv2.resize(straight, size, interpolation=cv2.INTER_CUBIC)
    except (cv2.error, MemoryError):
        return
    # The upscaled image is its own deskewed image: the strip re-read needs the read's coordinates.
    yield upscaled, upscaled, None
    yield upscaled, upscaled, RETRY_PSM


def _candidates(gray: np.ndarray, straight: np.ndarray) -> Iterator[_Candidate]:
    """Yield the (image to read, deskewed image, psm) candidates to retry, in order, lazily (D198).

    The plain deskewed photo comes first, then the upscaled one, then the upscaled one in block
    mode, then the crop; the last three are only computed when they are asked for.
    """
    yield straight, straight, None
    yield from _upscaled_candidates(straight)
    yield from _crop_candidates(gray)


def _read_candidate(
    image: np.ndarray, ocr_cfg: Ocr, rules: ParsingRules, left: float, psm: int | None
) -> tuple[OcrText | None, Reading | None] | None:
    """Return the text and reading of ``image`` read in ``left`` seconds, or ``None`` on failure.

    ``psm`` is the page segmentation mode; ``None`` is the configured one (D198).
    """
    try:
        text = read_text(image, ocr_cfg, timeout_s=left, psm=psm)
        return text, None if text is None else parse_ocr(text, rules).reading
    except (cv2.error, MemoryError):
        return None


def _left(budget: float, started: float) -> float:
    """Return the seconds of ``budget`` left after ``started`` (D198)."""
    return budget - (time.monotonic() - started)


def _retries(
    gray: np.ndarray,
    ocr_cfg: Ocr,
    rules: ParsingRules,
    started: float,
    first: tuple[OcrText | None, Reading | None, np.ndarray],
    qr_reading: Reading | None,
) -> tuple[OcrText | None, Reading | None, np.ndarray]:
    """Return the best of ``first`` and the retry reads, with the deskewed image it came from (D198, D200).

    While the best reading misses a required field, the candidates run in order, each only with
    at least ``MIN_RETRY_S`` of the retry budget left after ``started`` (the helper's start) and
    with only that time. The budget is ``ocr_cfg.timeout_s`` less ``STRIP_TIMEOUT_S``, kept for
    the strip re-read that follows. The best has the fewest missing required fields once merged
    with ``qr_reading``, a tie keeping the earlier reading; the reading kept is the OCR one,
    unmerged, with the fields it lacks filled from the other reads (``fill_missing``), and the
    retries stop once those fills leave nothing missing. ``first`` carries the deskewed image it
    came from; the image returned is the best read's.
    """
    best = first
    reads = [] if first[1] is None else [first[1]]
    candidates = _candidates(gray, first[2])
    budget = ocr_cfg.timeout_s - STRIP_TIMEOUT_S
    # Time is checked before a candidate is built, so a skipped crop is never made, and again after,
    # so the time the build took is not handed to the read.
    while (
        _missing_count(_filled(best[1], reads, qr_reading), qr_reading) > 0
        and _left(budget, started) >= MIN_RETRY_S
    ):
        candidate = next(candidates, None)
        left = _left(budget, started)
        if candidate is None or left < MIN_RETRY_S:
            break
        read = _read_candidate(candidate[0], ocr_cfg, rules, left, candidate[2])
        if read is None:
            continue
        if read[1] is not None:
            reads.append(read[1])
        if _missing_count(read[1], qr_reading) < _missing_count(best[1], qr_reading):
            best = (*read, candidate[1])
    return best[0], _filled(best[1], reads, qr_reading), best[2]


def _filled(best: Reading | None, reads: list[Reading], qr: Reading | None) -> Reading | None:
    """Return ``best`` with the gaps ``qr`` leaves filled from the other ``reads`` (D200).

    With no ``best`` there is nothing to fill and ``None`` is returned.
    """
    if best is None:
        return None
    qr_names = frozenset() if qr is None else frozenset(qr.fields)
    return fill_missing(best, [read for read in reads if read is not best], qr_names)


def _with_url_strip(
    straight: np.ndarray,
    text: OcrText,
    ocr_cfg: Ocr,
    rules: ParsingRules,
    reading: Reading,
) -> Reading:
    """Return ``reading`` with the strip under the consult anchor re-read, or unchanged (D191)."""
    try:
        strip = read_url_strip(straight, text, rules, ocr_cfg)
    except (MemoryError, cv2.error):
        return reading
    return reading if strip is None else replace(reading, url_strip=strip)


def _read_pdf(data: bytes, options: _Options) -> dict[str, Any]:
    """Read page 1 of a PDF from its text layer or, failing that, its render (R-2.15, D122)."""
    try:
        document = open_pdf(data, options.max_pdf_pages)
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
    return Reading(fields=fields, document_url=reading.document_url, printed_url=reading.printed_url)


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
