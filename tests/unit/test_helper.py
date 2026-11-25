"""Unit tests for the helper subprocess runner and the photo strip re-read (R-9.2, D191)."""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

from invoicebot.config.models import Ocr
from invoicebot.extract import __main__ as helper
from invoicebot.extract.fields import Field, Reading
from invoicebot.extract.ocr import OcrText, OcrWord
from invoicebot.extract.ocr_parse import OcrReading
from invoicebot.extract.parsing_rules import ParsingRules, load_rules
from invoicebot.helper import run_helper

#: Several times a pipe buffer, so the write blocks once the helper has gone.
LARGE_INPUT = b"x" * (4 * 1024 * 1024)
#: The exit code the early-exiting helper returns.
EARLY_EXIT_CODE = 3

ROOT = Path(__file__).resolve().parents[2]
RULES_PATH = ROOT / "config" / "parsing_rules.yaml"

#: Synthetic RUC from tests/synthetic_rucs.txt. No real RUC may appear here.
SYNTHETIC_RUC = "20000000001"
PRINTED_URL = "https://example.com/menu"
STRIP = "hitps.//portal.example"
CANVAS = (200, 600)
WHITE = 255
MAX_PX = 2000
OCR_CONFIG = Ocr(engine="tesseract", lang="spa", psm=6, timeout_s=5)
OCR_TEXT = OcrText(lines=((OcrWord(text="RUC", confidence=0.9),),))


@pytest.fixture(scope="module")
def rules() -> ParsingRules:
    """Return the checked-in parsing rules."""
    return load_rules(RULES_PATH)


def _ocr_reading(printed_url: str | None = None) -> Reading:
    """Return the OCR reading of a photo with one field, optionally with a printed URL."""
    return Reading(
        fields={"ruc_emisor": Field(value=SYNTHETIC_RUC, source="ocr", confidence=0.9)},
        document_url=None,
        printed_url=printed_url,
    )


def _fake_ocr(monkeypatch: pytest.MonkeyPatch, reading: Reading) -> None:
    """Make the photo pass return ``reading`` without running Tesseract or the field rules."""
    monkeypatch.setattr(helper, "load_gray", lambda _data, _max_px: np.full(CANVAS, WHITE, dtype=np.uint8))
    monkeypatch.setattr(helper, "read_invoice_qr", lambda _gray: None)
    monkeypatch.setattr(helper, "read_text", lambda _image, _cfg: OCR_TEXT)
    monkeypatch.setattr(helper, "parse_ocr", lambda _text, _rules: OcrReading(reading=reading))


async def test_helper_exiting_before_reading_its_input_raises_its_exit_code() -> None:
    """R-9.2: a helper that exits unread surfaces its exit code, not a broken pipe."""
    argv = [sys.executable, "-c", f"import sys; sys.exit({EARLY_EXIT_CODE})"]

    with pytest.raises(RuntimeError, match=f"exited with code {EARLY_EXIT_CODE}"):
        await run_helper(argv, LARGE_INPUT, {}, timeout_s=10, max_output_bytes=1024)


def test_photo_without_printed_url_keeps_the_re_read_strip(
    monkeypatch: pytest.MonkeyPatch, rules: ParsingRules
) -> None:
    """D191: a photo whose OCR has no printed URL carries the re-read strip in its payload."""
    _fake_ocr(monkeypatch, _ocr_reading())
    monkeypatch.setattr(helper, "read_url_strip", lambda _straight, _text, _rules, _cfg: STRIP)

    payload = helper._read_photo(b"photo", MAX_PX, OCR_CONFIG, rules)

    assert payload["reading"]["url_strip"] == STRIP


def test_photo_with_printed_url_does_not_re_read_the_strip(
    monkeypatch: pytest.MonkeyPatch, rules: ParsingRules
) -> None:
    """D191: a photo whose OCR already has a printed URL does not re-read the strip."""
    _fake_ocr(monkeypatch, _ocr_reading(printed_url=PRINTED_URL))

    def unexpected(*_args: object) -> str:
        raise AssertionError("read_url_strip must not run when the OCR has a printed URL")

    monkeypatch.setattr(helper, "read_url_strip", unexpected)

    payload = helper._read_photo(b"photo", MAX_PX, OCR_CONFIG, rules)

    assert payload["reading"]["printed_url"] == PRINTED_URL
    assert payload["reading"]["url_strip"] is None


def test_photo_re_read_cv2_error_keeps_the_reading_without_a_strip(
    monkeypatch: pytest.MonkeyPatch, rules: ParsingRules
) -> None:
    """D191: a re-read that raises a cv2 error keeps the reading, without a strip."""
    _fake_ocr(monkeypatch, _ocr_reading())

    def explode(*_args: object) -> str:
        raise cv2.error("boom")

    monkeypatch.setattr(helper, "read_url_strip", explode)

    payload = helper._read_photo(b"photo", MAX_PX, OCR_CONFIG, rules)

    assert payload["reading"]["url_strip"] is None
    assert payload["reading"]["fields"]["ruc_emisor"]["value"] == SYNTHETIC_RUC
