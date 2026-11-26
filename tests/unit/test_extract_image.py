"""Tests for image loading and the QR decode variants (R-1.11)."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from invoicebot.extract import image
from invoicebot.extract.image import ImageUnreadable, load_gray, variants

MAX_PX = 1000
WIDE = (1500, 3000)  # height, width
SMALL = (80, 100)  # height, width
VARIANT_NAMES = [
    "original",
    "upscaled_2x",
    "upscaled_3x",
    "sharpened",
    "rotated_90",
    "rotated_180",
    "rotated_270",
]


def _png(height: int, width: int) -> bytes:
    """Return a deterministic PNG of ``height`` x ``width`` grayscale pixels."""
    rng = np.random.default_rng(seed=0)
    gray = rng.integers(0, 256, size=(height, width), dtype=np.uint8)
    ok, encoded = cv2.imencode(".png", gray)
    assert ok
    return encoded.tobytes()


def test_load_gray_large_image_downscales_long_side_to_max_px() -> None:
    """R-1.11: a 3000 x 1500 photo comes back with its long side at ``max_px``."""
    gray = load_gray(_png(*WIDE), MAX_PX)

    assert gray.shape == (MAX_PX // 2, MAX_PX)


def test_load_gray_small_image_is_not_enlarged() -> None:
    """R-1.11: an image already under the cap keeps its size."""
    gray = load_gray(_png(*SMALL), MAX_PX)

    assert gray.shape == SMALL


def test_load_gray_garbage_raises_image_unreadable() -> None:
    """R-1.11: undecodable bytes raise ``ImageUnreadable``."""
    with pytest.raises(ImageUnreadable, match="not a decodable image"):
        load_gray(b"not an image", MAX_PX)


def test_variants_yields_names_in_documented_order() -> None:
    """R-1.11: the variants arrive in order, the original first."""
    gray = np.zeros((100, 200), dtype=np.uint8)

    assert [name for name, _ in variants(gray)] == VARIANT_NAMES


def test_variants_upscale_and_sharpen_as_documented() -> None:
    """R-1.11: 2x the whole page, 3x its bottom 40 %, and a sharpened copy."""
    gray = np.arange(100 * 200, dtype=np.uint8).reshape(100, 200)
    produced = dict(variants(gray))

    assert produced["original"] is gray
    assert produced["upscaled_2x"].shape == (200, 400)
    assert produced["upscaled_3x"].shape == (120, 600)  # bottom 40 rows, then 3x
    assert produced["sharpened"].shape == gray.shape


def test_variants_rotate_the_original() -> None:
    """R-1.11: the rotations turn the original, not another variant."""
    gray = np.arange(100 * 200, dtype=np.uint8).reshape(100, 200)
    produced = dict(variants(gray))

    assert np.array_equal(produced["rotated_90"], cv2.rotate(gray, cv2.ROTATE_90_CLOCKWISE))
    assert np.array_equal(produced["rotated_180"], cv2.rotate(gray, cv2.ROTATE_180))
    assert np.array_equal(produced["rotated_270"], cv2.rotate(gray, cv2.ROTATE_90_COUNTERCLOCKWISE))


def test_variants_is_lazy(monkeypatch: pytest.MonkeyPatch) -> None:
    """R-1.11: consuming the original computes nothing else."""
    calls: list[float] = []
    resize = image._resized

    def tracking_resize(gray: np.ndarray, factor: float) -> np.ndarray:
        calls.append(factor)
        return resize(gray, factor)

    monkeypatch.setattr(image, "_resized", tracking_resize)
    stream = variants(np.zeros((100, 200), dtype=np.uint8))

    name, _ = next(stream)

    assert name == "original"
    assert calls == []

    name, _ = next(stream)

    assert name == "upscaled_2x"
    assert calls == [2.0]
    stream.close()


RECEIPT_SIZE = (600, 1200)  # width, height
RECEIPT_ORIGIN = (200, 300)  # x, y
PHOTO_SIZE = (1080, 1920)  # width, height
STRIPE_DARK = 50
STRIPE_PERIOD = 40
MAX_CROP_GROWTH = 1.15


def _text_on_white(width: int, height: int) -> np.ndarray:
    """Return a white ``width`` x ``height`` page with a few lines of black text."""
    page = np.full((height, width), 255, dtype=np.uint8)
    for row in range(10, height - 20, 60):
        cv2.putText(page, "FACTURA 0001", (10, row), cv2.FONT_HERSHEY_SIMPLEX, 1.0, 0, 2)
    return page


def _striped_photo() -> np.ndarray:
    """Return a photo-sized background of dark diagonal stripes."""
    width, height = PHOTO_SIZE
    cols, rows = np.meshgrid(np.arange(width), np.arange(height))
    stripes = (cols + rows) % STRIPE_PERIOD < STRIPE_PERIOD // 2
    return np.where(stripes, STRIPE_DARK, STRIPE_DARK // 2).astype(np.uint8)


def test_crop_receipt_on_striped_background_keeps_receipt_with_small_margin() -> None:
    """D198: a receipt on a dark striped table is cropped with at most 15 % extra per side."""
    photo = _striped_photo()
    width, height = RECEIPT_SIZE
    x, y = RECEIPT_ORIGIN
    photo[y : y + height, x : x + width] = _text_on_white(width, height)

    crop = image.crop_receipt(photo)

    crop_height, crop_width = crop.shape
    assert width <= crop_width <= width * MAX_CROP_GROWTH
    assert height <= crop_height <= height * MAX_CROP_GROWTH
    assert (crop == 255).sum() >= (photo[y : y + height, x : x + width] == 255).sum()


def test_crop_receipt_receipt_filling_frame_is_unchanged() -> None:
    """D198: a page that fills the frame is returned as it came."""
    page = _text_on_white(600, 800)

    assert image.crop_receipt(page).shape == page.shape


def test_crop_receipt_small_blob_is_unchanged() -> None:
    """D198: a white square under 20 % of the image is not taken for the receipt."""
    photo = _striped_photo()
    photo[500:650, 400:550] = 255

    assert image.crop_receipt(photo).shape == photo.shape


def test_crop_receipt_uniform_gray_is_unchanged() -> None:
    """D198: an image with nothing to find is returned as it came."""
    gray = np.full((400, 300), 128, dtype=np.uint8)

    assert image.crop_receipt(gray).shape == gray.shape
