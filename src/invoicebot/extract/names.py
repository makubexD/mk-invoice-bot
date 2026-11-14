"""The closed sets of image-variant and QR-decoder names the extract helper may report (R-9.16).

These are the only values the helper may report, so logs carry a closed set (R-9.16).
"""

from __future__ import annotations

from typing import Literal

#: The names yielded by :func:`invoicebot.extract.image.variants`.
QrVariant = Literal[
    "original",
    "upscaled_2x",
    "upscaled_3x",
    "sharpened",
    "rotated_90",
    "rotated_180",
    "rotated_270",
]

#: The decoders :func:`invoicebot.extract.qr.read_invoice_qr` tries.
QrDecoder = Literal["zxing", "opencv"]
