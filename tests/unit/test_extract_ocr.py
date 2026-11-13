"""Tests for OCR preprocessing and the Tesseract call (S3.5, R-2.3, R-2.6, R-9.2)."""

from __future__ import annotations

import subprocess

import cv2
import numpy as np
import pytest

from invoicebot.config.models import Ocr
from invoicebot.extract.ocr import OcrWord, parse_tsv, preprocess, read_text

# Synthetic RUC from tests/synthetic_rucs.txt. No real RUC may appear here.
SYNTHETIC_RUC = "20000000001"
RUC_TEXT = f"RUC {SYNTHETIC_RUC}"
TOTAL_TEXT = "TOTAL S/ 12.50"
WHITE = 255
BLACK = 0
WIDTH = 1200
HEIGHT = 400
TEXT_LEFT = 60
FIRST_LINE_TOP = 140
SECOND_LINE_TOP = 300
FONT = cv2.FONT_HERSHEY_SIMPLEX
FONT_SCALE = 2.0
FONT_THICKNESS = 3
TIMEOUT_S = 30
TIMEOUT_FOR_TEST_S = 1
OCR_CONFIG = Ocr(engine="tesseract", lang="spa", psm=4, timeout_s=TIMEOUT_S)

TSV = "\n".join(
    (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
        "1\t1\t0\t0\t0\t0\t0\t0\t1200\t400\t-1\t",
        "4\t1\t1\t1\t2\t0\t60\t260\t400\t40\t-1\t",
        "5\t1\t1\t1\t2\t1\t60\t260\t60\t40\t120\tTOTAL",
        "5\t1\t1\t1\t2\t2\t140\t260\t200\t40\t-1\tS/ 12.50",
        "5\t1\t1\t1\t1\t1\t60\t100\t60\t40\t96.5\tRUC",
        "5\t1\t1\t1\t1\t2\t140\t100\t200\t40\t88\t20000000001",
        "5\t1\t1\t1\t3\t1\t60\t340\t40\t40\t99\t",
    )
)
EXPECTED_LINES = (
    (OcrWord(text="TOTAL", confidence=1.0), OcrWord(text="S/ 12.50", confidence=0.0)),
    (OcrWord(text="RUC", confidence=0.965), OcrWord(text="20000000001", confidence=0.88)),
)


def _canvas(width: int = WIDTH, height: int = HEIGHT) -> np.ndarray:
    """Return a white canvas of ``width`` x ``height`` grayscale pixels."""
    return np.full((height, width), WHITE, dtype=np.uint8)


def test_read_text_put_text_image_returns_both_lines() -> None:
    """R-2.3: Tesseract reads the RUC and the total drawn on a clean photo."""
    image = _canvas()
    cv2.putText(image, RUC_TEXT, (TEXT_LEFT, FIRST_LINE_TOP), FONT, FONT_SCALE, BLACK, FONT_THICKNESS)
    cv2.putText(image, TOTAL_TEXT, (TEXT_LEFT, SECOND_LINE_TOP), FONT, FONT_SCALE, BLACK, FONT_THICKNESS)

    result = read_text(preprocess(image), OCR_CONFIG)

    assert result is not None
    lines = [" ".join(word.text for word in line) for line in result.lines]
    assert any(RUC_TEXT in line for line in lines)
    assert any(TOTAL_TEXT in line for line in lines)


def test_read_text_blank_image_yields_no_words() -> None:
    """R-2.3: a blank photo yields no OCR words, as ``None`` or an empty result."""
    result = read_text(preprocess(_canvas()), OCR_CONFIG)

    assert result is None or not any(word for line in result.lines for word in line)


def test_read_text_timeout_returns_none(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R-9.2: a Tesseract run longer than timeout_s returns ``None`` and logs a warning."""

    def _timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="tesseract", timeout=TIMEOUT_FOR_TEST_S)

    monkeypatch.setattr("invoicebot.extract.ocr.subprocess.run", _timeout)

    assert read_text(_canvas(100, 100), OCR_CONFIG) is None
    assert "tesseract timed out" in caplog.text


def test_read_text_missing_tesseract_binary_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """D81: a missing Tesseract binary makes ``read_text`` return ``None`` instead of raising."""

    def _missing(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("tesseract")

    monkeypatch.setattr("invoicebot.extract.ocr.subprocess.run", _missing)

    assert read_text(_canvas(100, 100), OCR_CONFIG) is None


def test_read_text_non_zero_exit_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """D81: a Tesseract run that exits non-zero makes ``read_text`` return ``None``."""

    def _failed(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=["tesseract"], returncode=1, stdout=b"")

    monkeypatch.setattr("invoicebot.extract.ocr.subprocess.run", _failed)

    assert read_text(_canvas(100, 100), OCR_CONFIG) is None


def test_parse_tsv_groups_words_and_scales_confidence() -> None:
    """R-2.6: the TSV parser keeps word rows, groups them by line and scales confidence to 0..1."""
    assert parse_tsv(TSV).lines == EXPECTED_LINES


def test_parse_tsv_non_numeric_row_is_skipped() -> None:
    """D81: a TSV word row whose confidence or position is not a number is skipped."""
    tsv = "\n".join(
        (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
            "5\t1\t1\t1\t1\t1\t60\t100\t60\t40\t96.5\tKEPT",
            "5\t1\t1\t1\t1\t2\t60\t100\t200\t40\tnot-a-number\tDROPPED_CONF",
            "5\t1\tx\t1\t1\t3\t60\t100\t200\t40\t88\tDROPPED_POSITION",
        )
    )

    assert parse_tsv(tsv).lines == ((OcrWord(text="KEPT", confidence=0.965),),)
