"""Read invoice text from a photo with Tesseract (S3.5, D81).

The grayscale image is deskewed, equalised with CLAHE and binarised, then Tesseract reads it
through ``stdin`` and returns its words and confidences from the TSV output. OCR text is never
logged or stored raw (R-9.16). ``cv2`` is imported at module top because lint forbids imports
inside functions, and the ``heavy-in-helper-only`` contract keeps this module in the helper
process (D78).
"""

from __future__ import annotations

import logging
import math
import resource
import subprocess  # nosec B404  # runs only the fixed Tesseract argument list below
from dataclasses import dataclass
from typing import cast

import cv2
import numpy as np

from invoicebot.config.models import Ocr

logger = logging.getLogger(__name__)

TESSERACT_COMMAND = "tesseract"
TESSERACT_STDIN = "stdin"
TESSERACT_STDOUT = "stdout"
TESSERACT_OEM = "1"
TSV_CONFIG = "tsv"
PNG_FORMAT = ".png"
TEXT_ENCODING = "utf-8"

# Preprocessing chosen by the S0.3 spike (specs/spikes/ocr.md).
WHITE_PIXEL = 255
CLAHE_CLIP_LIMIT = 2.0
CLAHE_TILE_SIZE = (8, 8)
ADAPTIVE_BLOCK_SIZE = 31
ADAPTIVE_C = 15
DESKEW_KERNEL = (1, 15)
HOUGH_RHO = 1
HOUGH_THETA = np.pi / 720
HOUGH_THRESHOLD = 200
HOUGH_MIN_LINE_DIVISOR = 6
HOUGH_MAX_LINE_GAP = 10
MAX_SKEW_DEGREES = 20.0
MIN_DESKEW_DEGREES = 0.3

# TSV columns: level, page, block, paragraph, line, word, ..., conf, text.
TSV_WORD_LEVEL = "5"
TSV_BLOCK = 2
TSV_PARAGRAPH = 3
TSV_LINE = 4
TSV_CONFIDENCE = 10
TSV_TEXT = 11
TSV_MIN_COLUMNS = 12
CONFIDENCE_SCALE = 100.0
MIN_CONFIDENCE = 0.0
MAX_CONFIDENCE = 1.0


@dataclass(frozen=True)
class OcrWord:
    """One word Tesseract read, with its confidence in the range 0..1."""

    text: str
    confidence: float


@dataclass(frozen=True)
class OcrText:
    """The OCR result: lines of words in the order Tesseract first reported them."""

    lines: tuple[tuple[OcrWord, ...], ...]


def preprocess(gray: np.ndarray) -> np.ndarray:
    """Deskew, equalise and binarise ``gray`` before OCR (S0.3)."""
    straightened = _deskew(gray)
    equalised = cast(
        np.ndarray,
        cv2.createCLAHE(clipLimit=CLAHE_CLIP_LIMIT, tileGridSize=CLAHE_TILE_SIZE).apply(straightened),
    )
    return cast(
        np.ndarray,
        cv2.adaptiveThreshold(
            equalised,
            WHITE_PIXEL,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            ADAPTIVE_BLOCK_SIZE,
            ADAPTIVE_C,
        ),
    )


def _no_file_writes() -> None:
    """Refuse every file write in the Tesseract child process (R-9.2)."""
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))


def read_text(image: np.ndarray, ocr_cfg: Ocr) -> OcrText | None:
    """Run Tesseract over ``image`` and return its words, or ``None`` when OCR did not run.

    A missing binary, a timeout or a non-zero exit is treated as "no OCR". Stderr is discarded
    and the text is never logged (R-9.16).

    Tesseract writes only to stdout (TSV), so any file write is a bug or an attack and is refused
    by an empty file-size limit (R-9.2).
    """
    encoded, buffer = cv2.imencode(PNG_FORMAT, image)
    if not encoded:
        return None
    command = [
        TESSERACT_COMMAND,
        TESSERACT_STDIN,
        TESSERACT_STDOUT,
        "-l",
        ocr_cfg.lang,
        "--psm",
        str(ocr_cfg.psm),
        "--oem",
        TESSERACT_OEM,
        TSV_CONFIG,
    ]
    try:
        completed = subprocess.run(  # noqa: S603  # nosec B603  # fixed argument list, no shell
            command,
            input=cast(np.ndarray, buffer).tobytes(),
            capture_output=True,
            timeout=ocr_cfg.timeout_s,
            check=False,
            preexec_fn=_no_file_writes,  # the helper process is single-threaded, so this is safe
        )
    except subprocess.TimeoutExpired:
        logger.warning("tesseract timed out")
        return None
    except OSError:
        logger.warning("tesseract could not be run")
        return None
    if completed.returncode != 0:
        logger.warning("tesseract exited non-zero: %s", completed.returncode)
        return None
    return parse_tsv(completed.stdout.decode(TEXT_ENCODING, errors="replace"))


def parse_tsv(tsv: str) -> OcrText:
    """Build the OCR result from Tesseract's TSV output, keeping word rows with text."""
    rows = tsv.splitlines()[1:]
    lines: dict[tuple[int, int, int], list[OcrWord]] = {}
    for row in rows:
        fields = row.split("\t")
        if len(fields) < TSV_MIN_COLUMNS or fields[0] != TSV_WORD_LEVEL:
            continue
        text = fields[TSV_TEXT].strip()
        if not text:
            continue
        try:
            key = (int(fields[TSV_BLOCK]), int(fields[TSV_PARAGRAPH]), int(fields[TSV_LINE]))
            confidence = _confidence(fields[TSV_CONFIDENCE])
        except ValueError:
            continue
        lines.setdefault(key, []).append(OcrWord(text=text, confidence=confidence))
    return OcrText(lines=tuple(tuple(words) for words in lines.values()))


def _confidence(conf: str) -> float:
    """Return Tesseract's 0..100 confidence as 0..1, treating a negative as 0."""
    value = float(conf) / CONFIDENCE_SCALE
    return min(max(value, MIN_CONFIDENCE), MAX_CONFIDENCE)


def _deskew(gray: np.ndarray) -> np.ndarray:
    """Rotate the image so the median Hough line lies horizontal, or leave it unchanged."""
    binary = cast(
        np.ndarray,
        cv2.adaptiveThreshold(
            gray,
            WHITE_PIXEL,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            ADAPTIVE_BLOCK_SIZE,
            ADAPTIVE_C,
        ),
    )
    kernel = np.ones(DESKEW_KERNEL, dtype=np.uint8)
    dilated = cast(np.ndarray, cv2.dilate(cv2.bitwise_not(binary), kernel))
    height, width = gray.shape[:2]
    detected = cv2.HoughLinesP(
        dilated,
        HOUGH_RHO,
        HOUGH_THETA,
        HOUGH_THRESHOLD,
        minLineLength=width // HOUGH_MIN_LINE_DIVISOR,
        maxLineGap=HOUGH_MAX_LINE_GAP,
    )
    if detected is None:
        return gray
    lines = cast(np.ndarray, detected)
    angles: list[float] = []
    for x1, y1, x2, y2 in lines.reshape(-1, 4):
        line_angle = math.degrees(math.atan2(float(y2 - y1), float(x2 - x1)))
        if abs(line_angle) < MAX_SKEW_DEGREES:
            angles.append(line_angle)
    if not angles:
        return gray
    angle = float(np.median(angles))
    if abs(angle) < MIN_DESKEW_DEGREES:
        return gray
    matrix = cv2.getRotationMatrix2D((width / 2.0, height / 2.0), angle, 1.0)
    return cast(
        np.ndarray,
        cv2.warpAffine(
            gray,
            matrix,
            (width, height),
            flags=cv2.INTER_CUBIC,
            borderValue=WHITE_PIXEL,
        ),
    )
