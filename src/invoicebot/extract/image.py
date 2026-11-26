"""Decode an invoice photo, crop it to the receipt and yield the QR reader variants (R-1.11, D78, D198).

``cv2`` is imported at module top because lint forbids imports inside functions. The
``heavy-in-helper-only`` import contract keeps this module out of the bot process: only the
extract helper imports it, so OpenCV's memory returns to the system when the helper exits.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import cv2
import numpy as np

BOTTOM_FRACTION = 0.4
ROTATIONS: tuple[tuple[str, int], ...] = (
    ("rotated_90", cv2.ROTATE_90_CLOCKWISE),
    ("rotated_180", cv2.ROTATE_180),
    ("rotated_270", cv2.ROTATE_90_COUNTERCLOCKWISE),
)
SHARPEN_KERNEL = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]], dtype=np.float32)
UNREADABLE_MESSAGE = "bytes are not a decodable image"
BLUR_KERNEL = (5, 5)
CLOSE_KERNEL = (25, 25)
CROP_MARGIN = 0.02
MIN_RECEIPT_SHARE = 0.20
MAX_RECEIPT_SHARE = 0.95


class ImageUnreadable(ValueError):
    """The bytes are not an image OpenCV can decode."""


def load_gray(data: bytes, max_px: int) -> np.ndarray:
    """Decode ``data`` to grayscale, downscaling so the long side is at most ``max_px``.

    Raises:
        ImageUnreadable: when ``data`` is empty or not a decodable image.
    """
    try:
        decoded = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    except cv2.error as err:
        raise ImageUnreadable(UNREADABLE_MESSAGE) from err
    if decoded is None:
        raise ImageUnreadable(UNREADABLE_MESSAGE)

    gray = cast(np.ndarray, decoded)
    height, width = gray.shape[:2]
    long_side = max(height, width)
    if long_side <= max_px:
        return gray
    return _resized(gray, max_px / long_side)


def crop_receipt(gray: np.ndarray) -> np.ndarray:
    """Crop ``gray`` to the bright receipt on a busy background (D198).

    Returns ``gray`` unchanged when no bright blob covers between ``MIN_RECEIPT_SHARE`` and
    ``MAX_RECEIPT_SHARE`` of the image: it is then either not found or already filling the frame.
    """
    blurred = cast(np.ndarray, cv2.GaussianBlur(gray, BLUR_KERNEL, 0))
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, CLOSE_KERNEL)
    closed = cast(np.ndarray, cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel))
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return gray

    x, y, width, height = cv2.boundingRect(max(contours, key=cv2.contourArea))
    image_height, image_width = gray.shape[:2]
    share = width * height / (image_width * image_height)
    if not MIN_RECEIPT_SHARE <= share <= MAX_RECEIPT_SHARE:
        return gray

    margin_x, margin_y = round(width * CROP_MARGIN), round(height * CROP_MARGIN)
    return gray[
        max(0, y - margin_y) : min(image_height, y + height + margin_y),
        max(0, x - margin_x) : min(image_width, x + width + margin_x),
    ]


def variants(gray: np.ndarray) -> Iterator[tuple[str, np.ndarray]]:
    """Yield ``(name, image)`` decode candidates lazily, the original first.

    ``upscaled_3x`` enlarges the bottom 40 % of the page, where an invoice's QR usually sits.
    A caller that decodes the first variant never pays for the rest.
    """
    yield "original", gray
    yield "upscaled_2x", _resized(gray, 2.0)
    yield "upscaled_3x", _resized(_bottom(gray), 3.0)
    yield "sharpened", cast(np.ndarray, cv2.filter2D(gray, -1, SHARPEN_KERNEL))
    for name, rotate_code in ROTATIONS:
        yield name, cast(np.ndarray, cv2.rotate(gray, rotate_code))


def _resized(image: np.ndarray, factor: float) -> np.ndarray:
    """Scale ``image`` by ``factor``, keeping the aspect ratio."""
    height, width = image.shape[:2]
    size = (max(1, round(width * factor)), max(1, round(height * factor)))
    interpolation = cv2.INTER_CUBIC if factor > 1 else cv2.INTER_AREA
    return cast(np.ndarray, cv2.resize(image, size, interpolation=interpolation))


def _bottom(image: np.ndarray) -> np.ndarray:
    """Return the bottom ``BOTTOM_FRACTION`` of ``image``."""
    start = int(image.shape[0] * (1 - BOTTOM_FRACTION))
    return image[start:, :]
