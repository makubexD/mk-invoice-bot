"""Tests for reading a SUNAT QR out of a photo (R-2.1, R-2.8, R-9.16)."""

from __future__ import annotations

import io
import json
import sys
from types import SimpleNamespace
from typing import get_args

import cv2
import numpy as np
import pytest
from pydantic import TypeAdapter, ValidationError

from invoicebot.extract import __main__ as helper
from invoicebot.extract.fields import reading_from_json
from invoicebot.extract.image import variants
from invoicebot.extract.names import QrDecoder, QrVariant
from invoicebot.extract.qr import OPENCV_DECODER, ZXING_DECODER, read_invoice_qr
from invoicebot.extract.qr_payload import parse_qr_payload

# Synthetic RUC from tests/synthetic_rucs.txt. No real RUC may appear here.
SYNTHETIC_RUC = "20000000001"
PAYLOAD = f"{SYNTHETIC_RUC}|01|F001|15160|7.36|49.90|2026-11-13|6|{SYNTHETIC_RUC}"
EXPECTED = parse_qr_payload(PAYLOAD)
URL = "https://example.com/menu"
NOT_A_PAYLOAD = "Mesa 12"
WHITE = 255
CANVAS = (800, 800)  # height, width
LARGE_CANVAS = 2000
SMALL_SCALE = 3
QR_SCALE = 6
MAX_PX = 2000
MAX_BYTES = 10_000_000
SMALL_MAX_BYTES = 1024
HELPER_PSM = 6
HELPER_TIMEOUT_S = 5
RULES_PATH = "config/parsing_rules.yaml"


def _canvas(height: int, width: int) -> np.ndarray:
    """Return a white canvas of ``height`` x ``width`` grayscale pixels."""
    return np.full((height, width), WHITE, dtype=np.uint8)


def _code(text: str, scale: int = QR_SCALE) -> np.ndarray:
    """Return a QR image for ``text``, enlarged so both decoders can see it."""
    qr = cv2.QRCodeEncoder.create().encode(text)
    return cv2.resize(qr, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)


def _paste(canvas: np.ndarray, code: np.ndarray, top: int, left: int) -> np.ndarray:
    """Draw ``code`` on ``canvas`` at ``top``/``left`` and return the canvas."""
    height, width = code.shape
    canvas[top : top + height, left : left + width] = code
    return canvas


def _argv(*, max_bytes: int = MAX_BYTES) -> list[str]:
    """Build the helper argv one photo input comes in as (D122, D158)."""
    return [
        str(MAX_PX),
        str(max_bytes),
        "tesseract",
        "spa",
        str(HELPER_PSM),
        str(HELPER_TIMEOUT_S),
        RULES_PATH,
    ]


def _feed_stdin(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    """Make the helper read ``data`` from its stdin (D158)."""
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(data)))


def test_read_invoice_qr_clean_image_returns_payload() -> None:
    """R-2.1: a clean QR on a white canvas decodes to its SUNAT reading."""
    canvas = _paste(_canvas(*CANVAS), _code(PAYLOAD), 100, 100)

    hit = read_invoice_qr(canvas)

    assert hit is not None
    assert hit.reading == EXPECTED
    assert hit.variant == "original"
    assert hit.decoder == "zxing"


@pytest.mark.parametrize(
    "rotate_code",
    [cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180],
    ids=["rotated_90", "rotated_180"],
)
def test_read_invoice_qr_rotated_code_is_read(rotate_code: int) -> None:
    """R-2.1: the same QR turned 90 or 180 degrees is still read."""
    code = cv2.rotate(_code(PAYLOAD), rotate_code)
    canvas = _paste(_canvas(*CANVAS), code, 100, 100)

    hit = read_invoice_qr(canvas)

    assert hit is not None
    assert hit.reading == EXPECTED


def test_read_invoice_qr_small_code_in_bottom_corner_of_large_photo() -> None:
    """R-2.1: a small QR at the bottom of a 2000 px photo is found by a variant."""
    canvas = _paste(
        _canvas(LARGE_CANVAS, LARGE_CANVAS),
        _code(PAYLOAD, SMALL_SCALE),
        LARGE_CANVAS - 150,
        LARGE_CANVAS - 150,
    )

    hit = read_invoice_qr(canvas)

    assert hit is not None
    assert hit.reading == EXPECTED


def test_read_invoice_qr_blurred_code_is_read() -> None:
    """R-2.1: a lightly blurred QR still decodes."""
    canvas = _paste(_canvas(*CANVAS), _code(PAYLOAD), 100, 100)
    blurred = cv2.GaussianBlur(canvas, (5, 5), 1.5)

    hit = read_invoice_qr(blurred)

    assert hit is not None
    assert hit.reading == EXPECTED


def test_read_invoice_qr_two_codes_prefers_the_payload() -> None:
    """R-2.1, R-2.8: when a URL and a SUNAT payload share the photo, the payload wins."""
    canvas = _paste(_canvas(1200, 1200), _code(URL), 100, 100)
    _paste(canvas, _code(PAYLOAD), 600, 600)

    hit = read_invoice_qr(canvas)

    assert hit is not None
    assert hit.reading == EXPECTED
    assert hit.reading.document_url is None


def test_read_invoice_qr_only_a_non_sunat_code_returns_none() -> None:
    """R-2.1: a QR that is not a SUNAT payload and not a URL is skipped."""
    canvas = _paste(_canvas(*CANVAS), _code(NOT_A_PAYLOAD), 100, 100)

    assert read_invoice_qr(canvas) is None


def test_read_invoice_qr_url_only_is_kept() -> None:
    """R-2.8: a URL is kept when no variant finds a data payload."""
    canvas = _paste(_canvas(*CANVAS), _code(URL), 100, 100)

    hit = read_invoice_qr(canvas)

    assert hit is not None
    assert hit.reading.document_url == URL
    assert hit.reading.fields == {}


def test_read_invoice_qr_blank_image_returns_none() -> None:
    """R-2.1: a photo with no code yields no hit."""
    assert read_invoice_qr(_canvas(*CANVAS)) is None


def test_helper_opencv_error_reports_unreadable_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """BR-E01, R-2.1: an OpenCV error while reading a photo is an unreadable result, exit 0."""

    def explode(_data: bytes, _max_px: int) -> np.ndarray:
        raise cv2.error("boom")

    monkeypatch.setattr(helper, "_limit_resources", lambda: None)
    monkeypatch.setattr(helper, "load_gray", explode)
    _feed_stdin(monkeypatch, b"not an image")

    exit_code = helper.main(_argv())

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "unreadable"
    assert payload["reading"] is None


def test_helper_load_gray_memory_error_reports_unreadable_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R-2.1, BR-E01: a photo decode that runs out of memory answers unreadable, exit 0."""

    def explode(_data: bytes, _max_px: int) -> np.ndarray:
        raise MemoryError

    monkeypatch.setattr(helper, "_limit_resources", lambda: None)
    monkeypatch.setattr(helper, "load_gray", explode)
    _feed_stdin(monkeypatch, b"not an image")

    exit_code = helper.main(_argv())

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == helper._unreadable(ocr=False)


def test_helper_read_text_memory_error_reports_unreadable_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R-2.1, BR-E01: an OCR allocation failure answers unreadable, exit 0."""

    def explode(_image: np.ndarray, _ocr: object) -> None:
        raise MemoryError

    monkeypatch.setattr(helper, "_limit_resources", lambda: None)
    monkeypatch.setattr(helper, "load_gray", lambda _data, _max_px: _canvas(*CANVAS))
    monkeypatch.setattr(helper, "read_text", explode)
    _feed_stdin(monkeypatch, b"not an image")

    exit_code = helper.main(_argv())

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == helper._unreadable(ocr=False)


def test_helper_read_text_memory_error_keeps_the_qr_reading(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R-2.1, BR-E01: an OCR allocation failure on a QR photo still answers the QR reading."""

    def explode(_image: np.ndarray, _ocr: object) -> None:
        raise MemoryError

    canvas = _paste(_canvas(*CANVAS), _code(PAYLOAD), 100, 100)
    monkeypatch.setattr(helper, "_limit_resources", lambda: None)
    monkeypatch.setattr(helper, "load_gray", lambda _data, _max_px: canvas)
    monkeypatch.setattr(helper, "read_text", explode)
    _feed_stdin(monkeypatch, b"not an image")

    assert helper.main(_argv()) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] is None
    assert payload["ocr"] is False
    assert reading_from_json(payload["reading"]) == EXPECTED


def test_helper_reads_a_qr_photo_from_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D158: a synthetic QR photo fed on stdin prints the same payload as a path input did."""
    canvas = _paste(_canvas(*CANVAS), _code(PAYLOAD), 100, 100)
    encoded, buffer = cv2.imencode(".png", canvas)
    assert encoded
    _feed_stdin(monkeypatch, buffer.tobytes())
    monkeypatch.setattr(helper, "_limit_resources", lambda: None)

    assert helper.main(_argv()) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] is None
    assert reading_from_json(payload["reading"]) == EXPECTED


def test_helper_stdin_over_the_byte_cap_is_unreadable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D158: an input of max_bytes + 1 bytes prints the unreadable payload and exits 0."""
    _feed_stdin(monkeypatch, b"x" * (SMALL_MAX_BYTES + 1))
    monkeypatch.setattr(helper, "_limit_resources", lambda: None)

    assert helper.main(_argv(max_bytes=SMALL_MAX_BYTES)) == 0

    assert json.loads(capsys.readouterr().out) == helper._unreadable(ocr=False)


def test_variant_and_decoder_names_are_the_closed_sets() -> None:
    """R-9.16: variants() yields exactly the QrVariant names and the decoders are the closed set."""
    gray = np.zeros((40, 40), dtype=np.uint8)

    yielded = {name for name, _image in variants(gray)}

    assert yielded == set(get_args(QrVariant))
    assert {ZXING_DECODER, OPENCV_DECODER} == set(get_args(QrDecoder))


@pytest.mark.parametrize("type_", [QrVariant | None, QrDecoder | None])
def test_unknown_variant_or_decoder_name_is_rejected(type_: object) -> None:
    """R-9.16: a variant or decoder name outside the closed set fails validation."""
    with pytest.raises(ValidationError):
        TypeAdapter(type_).validate_python("other")
