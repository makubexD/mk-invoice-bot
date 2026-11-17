"""The committed synthetic golden set meets the R-2.9 accuracy targets (R-2.9, D54, D127)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from invoicebot.config.loader import load_config
from invoicebot.config.models import Config
from invoicebot.extract.accuracy import Score, load_expected, score, summarize
from invoicebot.extract.fields import reading_from_json

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "golden"
EXAMPLE = ROOT / "config" / "example.yaml"
RULES = ROOT / "config" / "parsing_rules.yaml"
#: The committed set holds at least this many pairs (spec 03 "Golden set", D54).
MIN_PAIRS = 10


def _helper_argv(image: Path, cfg: Config) -> list[str]:
    """Return the helper argv the extract step builds for a photo (pipeline/extract.py)."""
    extraction, media = cfg.extraction, cfg.limits.media
    return [
        sys.executable,
        "-I",
        "-m",
        "invoicebot.extract",
        str(image),
        str(extraction.max_image_px),
        str(media.max_bytes.image),
        extraction.ocr.engine,
        extraction.ocr.lang,
        str(extraction.ocr.psm),
        str(extraction.ocr.timeout_s),
        str(RULES),
        "photo",
        str(media.max_pdf_pages),
        str(extraction.pdf_min_text_chars),
    ]


def _score_pair(expected_path: Path, cfg: Config) -> Score:
    """Run the helper over the pair's image and score its reading against the expected file."""
    image = expected_path.with_suffix(".jpg")
    completed = subprocess.run(  # noqa: S603 - fixed argv: this interpreter and a repo fixture path
        _helper_argv(image, cfg),
        capture_output=True,
        check=True,
        text=True,
        timeout=cfg.extraction.helper_timeout_s,
    )
    payload = json.loads(completed.stdout)
    reading = None if payload["reading"] is None else reading_from_json(payload["reading"])
    return score(reading, load_expected(expected_path))


def test_golden_set_passes_the_accuracy_targets() -> None:
    """R-2.9: the committed synthetic set reaches 95 % on QR invoices and 80 % of OCR-only fields."""
    cfg = load_config(EXAMPLE)
    expected_paths = sorted(GOLDEN.glob("*.json"))
    assert len(expected_paths) >= MIN_PAIRS

    summary = summarize(_score_pair(path, cfg) for path in expected_paths)

    assert summary.passes(), summary
