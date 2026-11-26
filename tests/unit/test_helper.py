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
SMALL = (100, 300)


def _texts(count: int) -> list[OcrText]:
    """Return ``count`` distinct texts, so a fake parser can tell the reads apart."""
    return [OcrText(lines=((OcrWord(text=f"read{i}", confidence=0.9),),)) for i in range(count)]


def _reading_with(fields: dict[str, Field]) -> Reading:
    """Return a reading with ``fields`` and no URL."""
    return Reading(fields=fields, document_url=None, printed_url=None)


class _Spy:
    """Fake OCR that yields ``readings`` in order and records the reads and the crop calls."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, readings: list[Reading], *, crop_shape: tuple[int, int]
    ) -> None:
        self.calls: list[tuple[np.ndarray, float | None]] = []
        self.crops = 0
        texts = _texts(len(readings))
        by_text = dict(zip(texts, readings, strict=True))
        queue = iter(texts)

        def fake_read(image: np.ndarray, _cfg: Ocr, *, timeout_s: float | None = None) -> OcrText:
            self.calls.append((image, timeout_s))
            return next(queue)

        def fake_crop(_gray: np.ndarray) -> np.ndarray:
            self.crops += 1
            return np.full(crop_shape, WHITE, dtype=np.uint8)

        monkeypatch.setattr(
            helper, "load_gray", lambda _data, _max_px: np.full(CANVAS, WHITE, dtype=np.uint8)
        )
        monkeypatch.setattr(helper, "read_invoice_qr", lambda _gray: None)
        monkeypatch.setattr(helper, "deskew", lambda image: image)
        monkeypatch.setattr(helper, "binarize", lambda image: np.zeros_like(image))
        monkeypatch.setattr(helper, "crop_receipt", fake_crop)
        monkeypatch.setattr(helper, "read_text", fake_read)
        monkeypatch.setattr(helper, "parse_ocr", lambda text, _rules: OcrReading(reading=by_text[text]))


def _fields(*names: str) -> Reading:
    """Return a reading with only the required fields ``names``."""
    return _reading_with({n: Field(value=n, source="ocr", confidence=0.9) for n in names})


def test_photo_first_read_with_every_required_field_runs_no_other_read(
    monkeypatch: pytest.MonkeyPatch, rules: ParsingRules
) -> None:
    """D198: a first read that has every required field runs no retry and never crops."""
    spy = _Spy(monkeypatch, [_reading_with(FULL_FIELDS), _reading_with({})], crop_shape=SMALL)

    helper._read_photo(b"photo", MAX_PX, OCR_CONFIG, rules)

    assert len(spy.calls) == 1
    assert spy.calls[0][1] is None
    assert spy.crops == 0


def test_photo_plain_full_read_that_completes_is_kept_without_cropping(
    monkeypatch: pytest.MonkeyPatch, rules: ParsingRules
) -> None:
    """D198: a plain read of the whole photo that has every required field ends the retries."""
    spy = _Spy(monkeypatch, [_reading_with({}), _reading_with(FULL_FIELDS)], crop_shape=SMALL)

    payload = helper._read_photo(b"photo", MAX_PX, OCR_CONFIG, rules)

    assert set(payload["reading"]["fields"]) == set(helper.REQUIRED)
    assert len(spy.calls) == 2
    first_image, first_timeout = spy.calls[0]
    second_image, second_timeout = spy.calls[1]
    assert first_timeout is None
    assert second_timeout is not None
    assert second_timeout <= OCR_CONFIG.timeout_s
    assert not np.array_equal(first_image, second_image)
    assert spy.crops == 0


def test_photo_crop_read_is_the_fallback_when_the_full_reads_miss_fields(
    monkeypatch: pytest.MonkeyPatch, rules: ParsingRules
) -> None:
    """D198: with both full reads incomplete, the binarized crop read runs and is kept when complete."""
    spy = _Spy(
        monkeypatch, [_reading_with({}), _reading_with({}), _reading_with(FULL_FIELDS)], crop_shape=SMALL
    )

    payload = helper._read_photo(b"photo", MAX_PX, OCR_CONFIG, rules)

    assert set(payload["reading"]["fields"]) == set(helper.REQUIRED)
    assert [image.shape for image, _ in spy.calls] == [CANVAS, CANVAS, SMALL]
    assert spy.crops == 1


def test_photo_crop_with_the_same_shape_adds_no_read(
    monkeypatch: pytest.MonkeyPatch, rules: ParsingRules
) -> None:
    """D198: a crop that keeps the shape of the photo is no new candidate: only two reads."""
    spy = _Spy(
        monkeypatch, [_reading_with({}), _reading_with({}), _reading_with(FULL_FIELDS)], crop_shape=CANVAS
    )

    helper._read_photo(b"photo", MAX_PX, OCR_CONFIG, rules)

    assert len(spy.calls) == 2
    assert spy.crops == 1


def test_photo_first_read_that_used_the_budget_runs_no_retry(
    monkeypatch: pytest.MonkeyPatch, rules: ParsingRules
) -> None:
    """D198: with less than MIN_RETRY_S left after the first read there is no retry and no crop."""
    spy = _Spy(monkeypatch, [_reading_with({}), _reading_with(FULL_FIELDS)], crop_shape=SMALL)
    ticks: Callable[[], float] = iter((0.0, OCR_CONFIG.timeout_s - 0.5)).__next__
    monkeypatch.setattr(helper.time, "monotonic", ticks)

    payload = helper._read_photo(b"photo", MAX_PX, OCR_CONFIG, rules)

    assert len(spy.calls) == 1
    assert spy.crops == 0
    assert payload["reading"] is None


def test_photo_tied_reads_keep_the_earlier_and_re_read_the_strip_on_its_image(
    monkeypatch: pytest.MonkeyPatch, rules: ParsingRules
) -> None:
    """D198: ties keep the earlier reading, and the strip re-read gets the image that reading came from."""
    one, two, three, four = sorted(helper.REQUIRED)[:4]
    spy = _Spy(monkeypatch, [_fields(one), _fields(two), _fields(three), _fields(four)], crop_shape=SMALL)
    strip_images: list[np.ndarray] = []

    def fake_strip(image: np.ndarray, *_args: object) -> str:
        strip_images.append(image)
        return STRIP

    monkeypatch.setattr(helper, "read_url_strip", fake_strip)

    payload = helper._read_photo(b"photo", MAX_PX, OCR_CONFIG, rules)

    assert len(spy.calls) == 4
    assert set(payload["reading"]["fields"]) == {one}
    assert [image.shape for image in strip_images] == [CANVAS]


def test_photo_better_crop_reading_re_reads_the_strip_on_the_crop(
    monkeypatch: pytest.MonkeyPatch, rules: ParsingRules
) -> None:
    """D198: when the crop reading is kept, the strip re-read gets the deskewed crop."""
    spy = _Spy(
        monkeypatch, [_fields(), _fields(), _fields(sorted(helper.REQUIRED)[0]), _fields()], crop_shape=SMALL
    )
    strip_images: list[np.ndarray] = []

    def fake_strip(image: np.ndarray, *_args: object) -> str:
        strip_images.append(image)
        return STRIP

    monkeypatch.setattr(helper, "read_url_strip", fake_strip)

    helper._read_photo(b"photo", MAX_PX, OCR_CONFIG, rules)

    assert len(spy.calls) == 4
    assert [image.shape for image in strip_images] == [SMALL]
