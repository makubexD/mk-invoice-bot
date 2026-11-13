"""Extract helper process: read one invoice photo and print one JSON line (D78).

The extract handler starts this module as ``python -m invoicebot.extract <photo> <max_px>
<max_bytes> <engine> <lang> <psm> <timeout_s> <rules_path>``. It loads the photo once, reads its
SUNAT QR, reads its text with Tesseract and parses the fields with the configured rules, then
merges both readings (D81). It writes exactly one JSON object to stdout:
``{"reading": {...} | null, "variant": ..., "decoder": ..., "ocr": bool, "error": null |
"unreadable"}``, where ``ocr`` says whether Tesseract returned text. OpenCV, Tesseract and
zxing live here and are released when the process exits (D78). Nothing else is printed: stderr
is discarded by the handler because it could carry decoded text (R-9.16). A path or rules file
that cannot be read exits non-zero, so the caller raises and the queue retries. The process caps
its own address space, CPU time and file writes before it decodes anything, and an input above
the byte cap prints the same "unreadable" result as an undecodable image. An OpenCV error while
reading the image is reported the same way, as an unreadable photo (BR-E01).
"""

from __future__ import annotations

import json
import resource
import sys
from pathlib import Path
from typing import Any

import cv2
import yaml

from invoicebot.config.models import Ocr
from invoicebot.extract.fields import reading_to_json
from invoicebot.extract.image import ImageUnreadable, load_gray
from invoicebot.extract.merge import merge
from invoicebot.extract.ocr import preprocess, read_text
from invoicebot.extract.ocr_parse import parse_ocr
from invoicebot.extract.parsing_rules import load_rules
from invoicebot.extract.qr import read_invoice_qr

#: Exit code for a missing file or a bad invocation; the handler retries these (R-9.4).
EXIT_FAILURE = 1
#: Photo, pixel cap, byte cap, OCR engine, language, PSM, timeout and rules path.
ARGUMENT_COUNT = 8
#: The helper's value for "there is no reading to return" (BR-E01).
UNREADABLE = "unreadable"
#: Address space one decode may use (R-9.1). OpenCV maps about 1.5 GiB for its bundled
#: codecs before it decodes one pixel, and its thread pool reserves more per core, so a
#: 512 MiB cap makes every decode fail to allocate. This value leaves headroom over the
#: measured peak while still stopping a runaway allocation.
MAX_ADDRESS_SPACE_BYTES = 3 * 1024 * 1024 * 1024
#: CPU seconds the helper may burn before the kernel ends it (R-9.2).
MAX_CPU_SECONDS = 60
#: The helper writes to stdout only; a file write means something is wrong (R-9.2).
MAX_FILE_BYTES = 0


def _limit_resources() -> None:
    """Cap address space, CPU time and file writes before any byte is decoded (R-9.1, R-9.2)."""
    resource.setrlimit(resource.RLIMIT_AS, (MAX_ADDRESS_SPACE_BYTES, MAX_ADDRESS_SPACE_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (MAX_CPU_SECONDS, MAX_CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES, MAX_FILE_BYTES))


def main(argv: list[str] | None = None) -> int:
    """Read the photo named by ``argv`` and print the helper result (R-2.1, R-2.13)."""
    _limit_resources()
    args = sys.argv[1:] if argv is None else argv
    if len(args) != ARGUMENT_COUNT:
        return EXIT_FAILURE
    try:
        max_px = int(args[1])
        max_bytes = int(args[2])
        ocr_cfg = Ocr(engine=args[3], lang=args[4], psm=int(args[5]), timeout_s=int(args[6]))
        rules = load_rules(Path(args[7]))
        photo = Path(args[0])
        if photo.stat().st_size > max_bytes:
            _emit(_unreadable(ocr=False))
            return 0
        data = photo.read_bytes()
    except (OSError, ValueError, yaml.YAMLError):
        return EXIT_FAILURE

    try:
        gray = load_gray(data, max_px)
        hit = read_invoice_qr(gray)
        processed = preprocess(gray)
    except (ImageUnreadable, cv2.error):
        _emit(_unreadable(ocr=False))
        return 0

    text = read_text(processed, ocr_cfg)
    ocr_reading = None if text is None else parse_ocr(text, rules).reading
    reading = merge(ocr_reading, None if hit is None else hit.reading)
    if not reading.fields:
        _emit(_unreadable(ocr=text is not None))
        return 0
    _emit(
        {
            "reading": reading_to_json(reading),
            "variant": None if hit is None else hit.variant,
            "decoder": None if hit is None else hit.decoder,
            "ocr": text is not None,
            "error": None,
        }
    )
    return 0


def _unreadable(*, ocr: bool) -> dict[str, Any]:
    """Return the result for a photo where neither reader found a field."""
    return {"reading": None, "variant": None, "decoder": None, "ocr": ocr, "error": UNREADABLE}


def _emit(payload: dict[str, Any]) -> None:
    """Write the result as the helper's one stdout line."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
