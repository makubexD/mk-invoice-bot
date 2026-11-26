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
from invoicebot.extract.fields import REQUIRED, Field, Reading
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


FULL_FIELDS = {name: Field(value="x", source="ocr", confidence=0.9) for name in REQUIRED}
SMALL = (100, 300)
UPSCALED = (400, 1200)


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
        self.psms: list[int | None] = []
        self.crops = 0
        texts = _texts(len(readings))
        by_text = dict(zip(texts, readings, strict=True))
        queue = iter(texts)

        def fake_read(
            image: np.ndarray, _cfg: Ocr, *, timeout_s: float | None = None, psm: int | None = None
        ) -> OcrText:
            self.calls.append((image, timeout_s))
            self.psms.append(psm)
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

    assert set(payload["reading"]["fields"]) == set(REQUIRED)
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
    """D198: with the plain, upscaled and block-mode reads incomplete, the binarized crop read is kept."""
    spy = _Spy(
        monkeypatch,
        [*[_reading_with({})] * 4, _reading_with(FULL_FIELDS)],
        crop_shape=SMALL,
    )

    payload = helper._read_photo(b"photo", MAX_PX, OCR_CONFIG, rules)

    assert set(payload["reading"]["fields"]) == set(REQUIRED)
    assert [image.shape for image, _ in spy.calls] == [CANVAS, CANVAS, UPSCALED, UPSCALED, SMALL]
    assert spy.crops == 1


def test_photo_crop_with_the_same_shape_adds_no_read(
    monkeypatch: pytest.MonkeyPatch, rules: ParsingRules
) -> None:
    """D198: a crop that keeps the photo's shape is no new candidate: no read follows the block-mode one."""
    spy = _Spy(
        monkeypatch,
        [*[_reading_with({})] * 4, _reading_with(FULL_FIELDS)],
        crop_shape=CANVAS,
    )

    helper._read_photo(b"photo", MAX_PX, OCR_CONFIG, rules)

    assert len(spy.calls) == 4
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
    one, *others = sorted(REQUIRED)[:6]
    spy = _Spy(
        monkeypatch,
        [_fields(one), *(_fields(name) for name in others)],
        crop_shape=SMALL,
    )
    strip_images: list[np.ndarray] = []

    def fake_strip(image: np.ndarray, *_args: object) -> str:
        strip_images.append(image)
        return STRIP

    monkeypatch.setattr(helper, "read_url_strip", fake_strip)

    payload = helper._read_photo(b"photo", MAX_PX, OCR_CONFIG, rules)

    assert len(spy.calls) == 6
    assert set(payload["reading"]["fields"]) == {one}
    assert [image.shape for image in strip_images] == [CANVAS]


def test_photo_better_crop_reading_re_reads_the_strip_on_the_crop(
    monkeypatch: pytest.MonkeyPatch, rules: ParsingRules
) -> None:
    """D198: when the crop reading is kept, the strip re-read gets the deskewed crop."""
    spy = _Spy(
        monkeypatch,
        [_fields(), _fields(), _fields(), _fields(), _fields(sorted(REQUIRED)[0]), _fields()],
        crop_shape=SMALL,
    )
    strip_images: list[np.ndarray] = []

    def fake_strip(image: np.ndarray, *_args: object) -> str:
        strip_images.append(image)
        return STRIP

    monkeypatch.setattr(helper, "read_url_strip", fake_strip)

    helper._read_photo(b"photo", MAX_PX, OCR_CONFIG, rules)

    assert len(spy.calls) == 6
    assert [image.shape for image in strip_images] == [SMALL]


def test_photo_upscaled_read_that_completes_is_kept_and_the_strip_gets_the_straight_image(
    monkeypatch: pytest.MonkeyPatch, rules: ParsingRules
) -> None:
    """D198: a complete upscaled read is kept, twice the size, without cropping; the strip gets straight."""
    spy = _Spy(
        monkeypatch, [_reading_with({}), _reading_with({}), _reading_with(FULL_FIELDS)], crop_shape=SMALL
    )
    strip_images: list[np.ndarray] = []

    def fake_strip(image: np.ndarray, *_args: object) -> str:
        strip_images.append(image)
        return STRIP

    monkeypatch.setattr(helper, "read_url_strip", fake_strip)

    payload = helper._read_photo(b"photo", MAX_PX, OCR_CONFIG, rules)

    assert set(payload["reading"]["fields"]) == set(REQUIRED)
    assert len(spy.calls) == 3
    assert spy.calls[2][0].shape == (2 * CANVAS[0], 2 * CANVAS[1])
    assert spy.crops == 0
    assert [image.shape for image in strip_images] == [CANVAS]


def test_photo_block_mode_read_of_the_upscaled_photo_is_kept_without_cropping(
    monkeypatch: pytest.MonkeyPatch, rules: ParsingRules
) -> None:
    """D198: with the first three reads incomplete, the block-mode read of the upscaled photo is kept."""
    spy = _Spy(monkeypatch, [*[_reading_with({})] * 3, _reading_with(FULL_FIELDS)], crop_shape=SMALL)

    payload = helper._read_photo(b"photo", MAX_PX, OCR_CONFIG, rules)

    assert set(payload["reading"]["fields"]) == set(REQUIRED)
    assert len(spy.calls) == 4
    assert spy.psms == [None, None, None, helper.RETRY_PSM]
    assert spy.calls[3][0] is spy.calls[2][0]
    assert spy.crops == 0


@pytest.mark.parametrize(
    ("long_side", "expected_long_side"),
    [(3000, 4000), (4000, None), (5000, None)],
)
def test_upscaled_candidate_is_capped_at_the_maximum_side(
    long_side: int, expected_long_side: int | None
) -> None:
    """D198: a 3000 px photo is upscaled by 4000/3000; one of 4000 px or more gets no upscaled read."""
    straight = np.full((100, long_side), WHITE, dtype=np.uint8)

    candidates = list(helper._upscaled_candidates(straight))

    if expected_long_side is None:
        assert candidates == []
    else:
        (upscaled, deskewed, psm), (block, block_deskewed, block_psm) = candidates
        assert upscaled.shape == (round(100 * 4000 / 3000), expected_long_side)
        assert deskewed is straight
        assert psm is None
        assert block is upscaled
        assert block_deskewed is straight
        assert block_psm == helper.RETRY_PSM


def _fake_qr(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """Make the QR reading of the photo carry the required fields ``names``."""
    reading = Reading(
        fields={n: Field(value=n, source="qr", confidence=1.0) for n in names},
        document_url=None,
        printed_url=None,
    )
    hit = helper.QrHit(reading=reading, variant="original", decoder="zxing")
    monkeypatch.setattr(helper, "read_invoice_qr", lambda _gray: hit)


def test_photo_first_read_missing_only_what_the_qr_has_runs_no_retry(
    monkeypatch: pytest.MonkeyPatch, rules: ParsingRules
) -> None:
    """D198: a field the QR reading has is not missing, so the first read needs no retry."""
    others = sorted(set(REQUIRED) - {"ruc_emisor"})
    spy = _Spy(monkeypatch, [_fields(*others), _reading_with(FULL_FIELDS)], crop_shape=SMALL)
    _fake_qr(monkeypatch, "ruc_emisor")

    helper._read_photo(b"photo", MAX_PX, OCR_CONFIG, rules)

    assert len(spy.calls) == 1


def test_photo_retry_is_judged_with_the_qr_reading_and_the_ocr_one_is_kept(
    monkeypatch: pytest.MonkeyPatch, rules: ParsingRules
) -> None:
    """D198: with the QR having ruc_emisor, a first read missing it and total loses to a full read."""
    others = sorted(set(REQUIRED) - {"ruc_emisor", "total"})
    spy = _Spy(monkeypatch, [_fields(*others), _reading_with(FULL_FIELDS)], crop_shape=SMALL)
    _fake_qr(monkeypatch, "ruc_emisor")

    payload = helper._read_photo(b"photo", MAX_PX, OCR_CONFIG, rules)

    assert len(spy.calls) == 2
    assert payload["reading"]["fields"]["total"]["value"] == FULL_FIELDS["total"].value
