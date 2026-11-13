"""Extract helper process: read one invoice photo and print one JSON line (D78).

The extract handler starts this module as ``python -m invoicebot.extract <photo> <max_px>``.
It loads the photo and reads its SUNAT QR, then writes exactly one JSON object to stdout:
``{"reading": {...} | null, "variant": ..., "decoder": ..., "error": null | "unreadable"}``.
OpenCV and zxing live here and are released when the process exits (D78). Nothing else is
printed: stderr is discarded by the handler because it could carry decoded text (R-9.16).
A path that cannot be read exits non-zero, so the caller raises and the queue retries.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from invoicebot.extract.fields import Reading
from invoicebot.extract.image import ImageUnreadable, load_gray
from invoicebot.extract.qr import read_invoice_qr

#: Exit code for a missing file or a bad invocation; the handler retries these (R-9.4).
EXIT_FAILURE = 1
#: The helper's value for "there is no reading to return" (BR-E01).
UNREADABLE = "unreadable"


def main(argv: list[str] | None = None) -> int:
    """Read the photo named by ``argv`` and print the helper result (R-2.1, R-2.13)."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        return EXIT_FAILURE
    try:
        max_px = int(args[1])
        data = Path(args[0]).read_bytes()
    except (OSError, ValueError):
        return EXIT_FAILURE

    try:
        gray = load_gray(data, max_px)
    except ImageUnreadable:
        _emit(_unreadable())
        return 0

    hit = read_invoice_qr(gray)
    if hit is None:
        _emit(_unreadable())
        return 0
    _emit(
        {
            "reading": _reading_json(hit.reading),
            "variant": hit.variant,
            "decoder": hit.decoder,
            "error": None,
        }
    )
    return 0


def _unreadable() -> dict[str, Any]:
    """Return the result for a photo where no SUNAT reading was found."""
    return {"reading": None, "variant": None, "decoder": None, "error": UNREADABLE}


def _reading_json(reading: Reading) -> dict[str, Any]:
    """Render a reading as the field/value/source/confidence object the handler validates."""
    return {
        "fields": {
            name: {"value": field.value, "source": field.source, "confidence": field.confidence}
            for name, field in reading.fields.items()
        },
        "document_url": reading.document_url,
    }


def _emit(payload: dict[str, Any]) -> None:
    """Write the result as the helper's one stdout line."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
