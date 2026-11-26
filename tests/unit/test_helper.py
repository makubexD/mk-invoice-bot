"""Unit tests for the helper subprocess runner and the photo strip re-read (R-9.2, D191)."""

from __future__ import annotations

import sys
from collections.abc import Callable
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
    monkeypatch.setattr(helper, "read_text", lambda _image, _cfg, **_kwargs: OCR_TEXT)
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
    assert "url_strip" not in payload["reading"]


@pytest.mark.parametrize("error", [cv2.error("boom"), MemoryError()])
def test_photo_re_read_error_keeps_the_reading_without_a_strip(
    monkeypatch: pytest.MonkeyPatch, rules: ParsingRules, error: Exception
) -> None:
    """D191: a re-read that fails or runs out of memory keeps the reading, without a strip."""
    _fake_ocr(monkeypatch, _ocr_reading())

    def explode(*_args: object) -> str:
        raise error

    monkeypatch.setattr(helper, "read_url_strip", explode)

    payload = helper._read_photo(b"photo", MAX_PX, OCR_CONFIG, rules)

    assert "url_strip" not in payload["reading"]
    assert payload["reading"]["fields"]["ruc_emisor"]["value"] == SYNTHETIC_RUC


FULL_FIELDS = {name: Field(value="x", source="ocr", confidence=0.9) for name in helper.REQUIRED}
FIRST_TEXT = OcrText(lines=((OcrWord(text="first", confidence=0.9),),))
SECOND_TEXT = OcrText(lines=((OcrWord(text="second", confidence=0.9),),))


def _reading_with(fields: dict[str, Field]) -> Reading:
    """Return a reading with ``fields`` and no URL."""
    return Reading(fields=fields, document_url=None, printed_url=PRINTED_URL)


def _two_reads(
    monkeypatch: pytest.MonkeyPatch, first: Reading, second: Reading
) -> list[tuple[np.ndarray, float | None]]:
    """Fake OCR so the first read yields ``first`` and the second ``second``; return the calls."""
    calls: list[tuple[np.ndarray, float | None]] = []
    texts = iter((FIRST_TEXT, SECOND_TEXT))
    readings = {FIRST_TEXT: first, SECOND_TEXT: second}

    def fake_read(image: np.ndarray, _cfg: Ocr, *, timeout_s: float | None = None) -> OcrText:
        calls.append((image, timeout_s))
        return next(texts)

    monkeypatch.setattr(helper, "load_gray", lambda _data, _max_px: np.full(CANVAS, WHITE, dtype=np.uint8))
    monkeypatch.setattr(helper, "read_invoice_qr", lambda _gray: None)
    monkeypatch.setattr(helper, "read_text", fake_read)
    monkeypatch.setattr(helper, "parse_ocr", lambda text, _rules: OcrReading(reading=readings[text]))
    return calls


def test_photo_first_read_without_required_fields_returns_the_unbinarized_read(
    monkeypatch: pytest.MonkeyPatch, rules: ParsingRules
) -> None:
    """D198: a binarized read with no required field loses to an unbinarized read with all."""
    calls = _two_reads(monkeypatch, _reading_with({}), _reading_with(FULL_FIELDS))
    monkeypatch.setattr(helper, "binarize", np.zeros_like)

    payload = helper._read_photo(b"photo", MAX_PX, OCR_CONFIG, rules)

    assert set(payload["reading"]["fields"]) == set(helper.REQUIRED)
    assert len(calls) == 2
    first_image, first_timeout = calls[0]
    second_image, second_timeout = calls[1]
    assert first_timeout is None
    assert second_timeout is not None
    assert second_timeout <= OCR_CONFIG.timeout_s
    assert not np.array_equal(first_image, second_image)


def test_photo_first_read_with_every_required_field_skips_the_second_read(
    monkeypatch: pytest.MonkeyPatch, rules: ParsingRules
) -> None:
    """D198: a first read that has every required field never runs the second read."""
    calls = _two_reads(monkeypatch, _reading_with(FULL_FIELDS), _reading_with({}))

    helper._read_photo(b"photo", MAX_PX, OCR_CONFIG, rules)

    assert len(calls) == 1


def test_photo_first_read_that_used_the_budget_skips_the_second_read(
    monkeypatch: pytest.MonkeyPatch, rules: ParsingRules
) -> None:
    """D198: with less than MIN_RETRY_S left after the first read there is no second read."""
    calls = _two_reads(monkeypatch, _reading_with({}), _reading_with(FULL_FIELDS))
    ticks: Callable[[], float] = iter((0.0, OCR_CONFIG.timeout_s - 0.5)).__next__
    monkeypatch.setattr(helper.time, "monotonic", ticks)

    payload = helper._read_photo(b"photo", MAX_PX, OCR_CONFIG, rules)

    assert len(calls) == 1
    assert payload["reading"] is None


def test_photo_tied_reads_keep_the_binarized_reading(
    monkeypatch: pytest.MonkeyPatch, rules: ParsingRules
) -> None:
    """D198: when both reads have the same number of required fields the first one stays."""
    first = _reading_with({"ruc_emisor": Field(value="first", source="ocr", confidence=0.9)})
    second = _reading_with({"total": Field(value="second", source="ocr", confidence=0.9)})
    calls = _two_reads(monkeypatch, first, second)

    payload = helper._read_photo(b"photo", MAX_PX, OCR_CONFIG, rules)

    assert len(calls) == 2
    assert set(payload["reading"]["fields"]) == {"ruc_emisor"}
