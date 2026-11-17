"""A stand-in for the Tesseract binary in tests (R-2.3, R-9.2).

Run it as ``python tests/fake_tesseract.py`` through ``read_text``'s ``command`` argument. It
ignores its arguments and drains stdin, then does what ``FAKE_TESSERACT_MODE`` says: ``fail``
exits 1 with no output, ``sleep`` waits past a short timeout, and ``tsv`` prints a fixed TSV.
"""

from __future__ import annotations

import os
import sys
import time

MODE_ENV = "FAKE_TESSERACT_MODE"
FAIL = "fail"
SLEEP = "sleep"
TSV = "tsv"
SLEEP_SECONDS = 30

TSV_OUTPUT = "\n".join(
    (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
        "1\t1\t0\t0\t0\t0\t0\t0\t1200\t400\t-1\t",
        "4\t1\t1\t1\t1\t0\t60\t100\t400\t40\t-1\t",
        "5\t1\t1\t1\t1\t1\t60\t100\t60\t40\t96.5\tRUC",
        "5\t1\t1\t1\t1\t2\t140\t100\t200\t40\t88\t20000000001",
    )
)


def main() -> int:
    """Drain stdin, then act out the mode named in ``FAKE_TESSERACT_MODE``."""
    sys.stdin.buffer.read()
    mode = os.environ[MODE_ENV]
    if mode == FAIL:
        return 1
    if mode == SLEEP:
        time.sleep(SLEEP_SECONDS)
        return 0
    if mode == TSV:
        sys.stdout.write(TSV_OUTPUT)
        return 0
    sys.stderr.write(f"unknown {MODE_ENV}: {mode!r}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
