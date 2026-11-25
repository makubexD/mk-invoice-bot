"""Re-read the line under a ``CONSULT… EN`` anchor, for the portal guess (D191).

The full-page OCR reads the printed portal address too small to use. This module finds the anchor
line that sits just above it, crops a band under that line from the deskewed gray image and reads
the band again as one line. The best read, reduced to URL characters, is the reading's
``url_strip``. ``cv2`` is imported at module top because lint forbids imports inside functions, and
the ``heavy-in-helper-only`` contract keeps this module in the helper process (D78). The settings
come from the spike in ``specs/spikes/portal-guess.md``.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

import cv2
import numpy as np

from invoicebot.config.models import Ocr
from invoicebot.extract.fields import MAX_URL_STRIP_CHARS
from invoicebot.extract.ocr import TESSERACT_COMMAND, OcrBox, OcrText, OcrWord, read_text
from invoicebot.extract.parsing_rules import ParsingRules

#: Upscale of the cropped band before the second read (spike).
STRIP_SCALE = 3
#: Band tops, in anchor word heights below the anchor's bottom edge (spike).
STRIP_OFFSETS = (0.0, 0.15, 0.3)
#: Band heights, in anchor word heights (spike).
STRIP_HEIGHTS = (1.6, 1.8)
#: Tesseract language and page segmentation for a one-line read (spike).
STRIP_LANG = "eng"
STRIP_PSM = 7
#: Runs of characters that are not URL characters; each becomes one space (D191).
URL_STRIP_JUNK = re.compile(r"[^A-Za-z0-9.:/_~%-]+")

_MIN_WORD_HEIGHT = 1
_MIN_BOX_HEIGHT = 1


@dataclass(frozen=True)
class Anchor:
    """The median bottom and word height of the words on the consult line (D191)."""

    bottom: int
    word_height: int


def find_anchor(text: OcrText, pattern: re.Pattern[str]) -> Anchor | None:
    """Return the first line matching ``pattern`` as an anchor, or ``None`` (D191).

    A matching line whose words lack a box, or whose median word height is under one pixel,
    gives no usable anchor.
    """
    for line in text.lines:
        if not line or pattern.search(" ".join(word.text for word in line)) is None:
            continue
        boxes: list[OcrBox] = []
        for word in line:
            if word.box is None:
                return None
            boxes.append(word.box)
        word_height = statistics.median_low(box.height for box in boxes)
        if word_height < _MIN_WORD_HEIGHT:
            return None
        return Anchor(bottom=statistics.median_low(box.bottom for box in boxes), word_height=word_height)
    return None


def strip_boxes(anchor: Anchor, image_width: int, image_height: int) -> list[OcrBox]:
    """Return the full-width bands under ``anchor``, clipped to the image (D191).

    One box per offset and height, in that order; a box with less than a pixel of height left
    after clipping is left out.
    """
    boxes: list[OcrBox] = []
    for offset in STRIP_OFFSETS:
        top = anchor.bottom + round(offset * anchor.word_height)
        for factor in STRIP_HEIGHTS:
            height = round(factor * anchor.word_height)
            clipped_height = min(top + height, image_height) - top
            if clipped_height < _MIN_BOX_HEIGHT:
                continue
            boxes.append(OcrBox(left=0, top=top, width=image_width, height=clipped_height))
    return boxes


def clean_strip(raw: str) -> str | None:
    """Reduce a raw read to URL characters separated by single spaces, or ``None`` (D191)."""
    cleaned = URL_STRIP_JUNK.sub(" ", raw).strip()[:MAX_URL_STRIP_CHARS]
    return cleaned or None


def read_url_strip(
    straight: np.ndarray,
    text: OcrText,
    rules: ParsingRules,
    ocr_cfg: Ocr,
    command: Sequence[str] = (TESSERACT_COMMAND,),
) -> str | None:
    """Re-read the bands under the consult anchor and return the most confident read (D191).

    ``straight`` is the deskewed gray image the word boxes refer to. Each band is cropped,
    upscaled and read as one line; the read with the highest mean word confidence wins (ties:
    the first). ``None`` when there is no anchor, no band or no read.
    """
    anchor = find_anchor(text, rules.anchor_pattern)
    if anchor is None:
        return None
    image_height, image_width = straight.shape[:2]
    boxes = strip_boxes(anchor, image_width, image_height)
    if not boxes:
        return None
    read_cfg = ocr_cfg.model_copy(update={"lang": STRIP_LANG, "psm": STRIP_PSM})
    best_words: tuple[OcrWord, ...] | None = None
    best_confidence = -1.0
    for box in boxes:
        crop = straight[box.top : box.bottom, box.left : box.left + box.width]
        if crop.size == 0:
            continue
        upscaled = cv2.resize(crop, None, fx=STRIP_SCALE, fy=STRIP_SCALE, interpolation=cv2.INTER_CUBIC)
        read = read_text(upscaled, read_cfg, command)
        if read is None:
            continue
        words = tuple(word for line in read.lines for word in line)
        if not words:
            continue
        confidence = sum(word.confidence for word in words) / len(words)
        if confidence > best_confidence:
            best_confidence = confidence
            best_words = words
    if best_words is None:
        return None
    return clean_strip(" ".join(word.text for word in best_words))
