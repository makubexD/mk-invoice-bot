"""Tests for the strip re-read under the printed URL anchor (D191)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

import invoicebot.extract.url_strip as url_strip_module
from invoicebot.config.models import Ocr
from invoicebot.extract.fields import MAX_URL_STRIP_CHARS
from invoicebot.extract.ocr import OcrBox, OcrText, OcrWord
from invoicebot.extract.parsing_rules import ParsingRules, load_rules
from invoicebot.extract.url_strip import (
    Anchor,
    clean_strip,
    find_anchor,
    read_url_strip,
    strip_boxes,
)

ROOT = Path(__file__).resolve().parents[2]
RULES_PATH = ROOT / "config" / "parsing_rules.yaml"

WIDTH = 600
HEIGHT = 200
WHITE = 255
TIMEOUT_S = 30
OCR_CONFIG = Ocr(engine="tesseract", lang="spa", psm=4, timeout_s=TIMEOUT_S)

TSV_HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
FAKE_ONE_WORD_SCRIPT = (
    "import sys\n"
    "sys.stdin.buffer.read()\n"
    f"print({TSV_HEADER!r})\n"
    f"print({('5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t50\thitps.//portal.example')!r})\n"
)
FAKE_BY_HEIGHT_SCRIPT = (
    "import sys\n"
    "import cv2\n"
    "import numpy as np\n"
    "data = sys.stdin.buffer.read()\n"
    "image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)\n"
    "height = int(image.shape[0])\n"
    f"print({TSV_HEADER!r})\n"
    "print(f'5\\t1\\t1\\t1\\t1\\t1\\t0\\t0\\t10\\t10\\t{height}\\thitps.//crop{height}')\n"
)


@pytest.fixture(scope="module")
def rules() -> ParsingRules:
    """Return the checked-in parsing rules."""
    return load_rules(RULES_PATH)


def _anchor_text() -> OcrText:
    """Return an OCR result whose consult line is boxed, as the full-page pass reports it."""
    return OcrText(
        lines=(
            (
                OcrWord(text="RUC", confidence=0.9, box=OcrBox(10, 10, 40, 20)),
                OcrWord(text="20000000001", confidence=0.9, box=OcrBox(60, 10, 120, 20)),
            ),
            (
                OcrWord(text="CONSULTADO", confidence=0.9, box=OcrBox(10, 100, 80, 20)),
                OcrWord(text="EN;", confidence=0.9, box=OcrBox(95, 104, 20, 22)),
            ),
        )
    )


def test_find_anchor_matched_line_returns_median_bottom_and_height(rules: ParsingRules) -> None:
    """D191: the first matching line gives the median bottom and word height of its words."""
    assert find_anchor(_anchor_text(), rules.anchor_pattern) == Anchor(bottom=120, word_height=20)


def test_find_anchor_no_matching_line_returns_none(rules: ParsingRules) -> None:
    """D191: a page without a consult line has no anchor."""
    text = OcrText(lines=((OcrWord(text="TOTAL", confidence=0.9, box=OcrBox(10, 10, 60, 20)),),))

    assert find_anchor(text, rules.anchor_pattern) is None


def test_find_anchor_matching_word_without_box_returns_none(rules: ParsingRules) -> None:
    """D191: a matching line with a word that has no box cannot place a band, so there is none."""
    text = OcrText(
        lines=(
            (
                OcrWord(text="CONSULTADO", confidence=0.9, box=OcrBox(10, 100, 80, 20)),
                OcrWord(text="EN;", confidence=0.9),
            ),
        )
    )

    assert find_anchor(text, rules.anchor_pattern) is None


def test_strip_boxes_returns_six_bands_in_offset_then_height_order() -> None:
    """D191: offsets and heights make six full-width bands, the first at the anchor's bottom."""
    boxes = strip_boxes(Anchor(bottom=120, word_height=20), WIDTH, 1280)

    assert len(boxes) == 6
    assert boxes[0] == OcrBox(left=0, top=120, width=WIDTH, height=32)
    assert boxes[-1] == OcrBox(left=0, top=126, width=WIDTH, height=36)


def test_strip_boxes_near_image_bottom_keeps_clipped_bands_only() -> None:
    """D191: bands clipped to the image keep at least one pixel and end at its bottom edge."""
    boxes = strip_boxes(Anchor(bottom=1275, word_height=20), WIDTH, 1280)

    assert boxes
    assert all(box.height >= 1 and box.top + box.height == 1280 for box in boxes)


def test_clean_strip_keeps_url_characters_and_trims() -> None:
    """D191: junk runs become one space, the edges are trimmed and URL characters stay."""
    assert clean_strip("hitps.//sy.example pe/tiemda ") == "hitps.//sy.example pe/tiemda"
    assert clean_strip("https://a.pe/x»…") == "https://a.pe/x"


def test_clean_strip_all_junk_returns_none() -> None:
    """D191: a read with no URL character yields no strip."""
    assert clean_strip("«»") is None


def test_clean_strip_long_input_is_cut_to_the_cap() -> None:
    """D191: a strip longer than the cap keeps only its first characters."""
    assert clean_strip("a" * 300) == "a" * MAX_URL_STRIP_CHARS


def test_read_url_strip_boxed_anchor_returns_the_read(rules: ParsingRules) -> None:
    """D191: a boxed consult line makes the strip re-read return its cleaned word text."""
    image = np.full((HEIGHT, WIDTH), WHITE, dtype=np.uint8)

    result = read_url_strip(
        image,
        _anchor_text(),
        rules,
        OCR_CONFIG,
        command=(sys.executable, "-c", FAKE_ONE_WORD_SCRIPT),
    )

    assert result == "hitps.//portal.example"


def test_read_url_strip_no_anchor_returns_none_without_running_ocr(
    rules: ParsingRules, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D191: without an anchor the strip is not read and the OCR command is never run."""

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("read_text must not run without an anchor")

    monkeypatch.setattr(url_strip_module, "read_text", fail)
    text = OcrText(lines=((OcrWord(text="TOTAL", confidence=0.9, box=OcrBox(10, 10, 60, 20)),),))

    assert read_url_strip(np.full((HEIGHT, WIDTH), WHITE, dtype=np.uint8), text, rules, OCR_CONFIG) is None


def test_read_url_strip_tallest_crop_with_highest_confidence_wins(rules: ParsingRules) -> None:
    """D191: the band read with the highest mean confidence supplies the strip."""
    image = np.full((HEIGHT, WIDTH), WHITE, dtype=np.uint8)

    result = read_url_strip(
        image,
        _anchor_text(),
        rules,
        OCR_CONFIG,
        command=(sys.executable, "-c", FAKE_BY_HEIGHT_SCRIPT),
    )

    assert result == "hitps.//crop108"
