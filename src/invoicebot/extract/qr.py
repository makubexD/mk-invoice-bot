"""Decode a SUNAT invoice QR from a photo (S3.3, R-2.1, D78).

Each image variant from :func:`invoicebot.extract.image.variants` is tried by zxing (QR and
PDF417) first, then by OpenCV's built-in detector, which adds no dependency. A code that decodes
but is not a SUNAT payload is skipped; a URL is kept only when no variant yields a data payload
(R-2.8). ``cv2`` and ``zxingcpp`` are imported at module top because lint forbids imports inside
functions; the ``heavy-in-helper-only`` contract keeps this module in the helper process.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import cv2
import numpy as np
import zxingcpp

from invoicebot.extract.fields import Reading
from invoicebot.extract.image import variants
from invoicebot.extract.qr_payload import parse_qr_payload

ZXING_DECODER = "zxing"
OPENCV_DECODER = "opencv"
ZXING_FORMATS = zxingcpp.BarcodeFormats((zxingcpp.BarcodeFormat.QRCode, zxingcpp.BarcodeFormat.PDF417))


@dataclass(frozen=True)
class QrHit:
    """The first invoice QR that decoded, with the variant and decoder that found it."""

    reading: Reading
    variant: str
    decoder: str


def read_invoice_qr(gray: np.ndarray) -> QrHit | None:
    """Return the first SUNAT reading found across the variants, or ``None``.

    A URL hit is returned only when no variant carried a data payload (R-2.8).
    """
    detector = cv2.QRCodeDetector()
    url_hit: QrHit | None = None
    for variant_name, image in variants(gray):
        for decoder_name, texts in _decoded_texts(detector, image):
            for text in texts:
                reading = parse_qr_payload(text)
                if reading is None:
                    continue
                hit = QrHit(reading=reading, variant=variant_name, decoder=decoder_name)
                if reading.document_url is None:
                    return hit
                if url_hit is None:
                    url_hit = hit
    return url_hit


def _decoded_texts(detector: cv2.QRCodeDetector, image: np.ndarray) -> Iterator[tuple[str, tuple[str, ...]]]:
    """Yield each decoder's texts lazily, so a zxing hit skips OpenCV for that variant."""
    yield ZXING_DECODER, _decode_zxing(image)
    yield OPENCV_DECODER, _decode_opencv(detector, image)


def _decode_zxing(image: np.ndarray) -> tuple[str, ...]:
    """Return the QR and PDF417 texts zxing reads from ``image``."""
    return tuple(result.text for result in zxingcpp.read_barcodes(image, formats=ZXING_FORMATS))


def _decode_opencv(detector: cv2.QRCodeDetector, image: np.ndarray) -> tuple[str, ...]:
    """Return the texts OpenCV's detector reads from ``image``; a decode error skips it."""
    try:
        found, texts, *_ = detector.detectAndDecodeMulti(image)
    except cv2.error:
        return ()
    if not found:
        return ()
    return tuple(text for text in texts if text)
