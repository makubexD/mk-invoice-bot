"""Score a golden set against the R-2.9 accuracy target (D127).

An expected file holds ``{"has_qr": bool, "fields": {name: value}}``; an absent field means
the receipt does not print it. A merged reading is scored field by field with the same
normalisation the merge uses: amounts as decimals, dates as ISO dates, and text stripped and
upper-cased.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from invoicebot.extract.fields import REQUIRED, Reading
from invoicebot.extract.merge import same_value

#: R-2.9 targets: 95 % of QR-carrying invoices fully right, 80 % of required fields by OCR.
QR_MIN_RATE = 0.95
OCR_MIN_RATE = 0.80

#: The field names an expected file may hold (D127); any other name is refused.
EXPECTED_FIELDS = frozenset(
    {
        "ruc_emisor",
        "tipo",
        "serie",
        "numero",
        "fecha_emision",
        "total",
        "base",
        "igv",
        "service_charge",
        "icbper",
        "discounts",
        "num_doc_adq",
        "razon_social",
    }
)

#: The fields a SUNAT QR payload carries (``extract/qr_payload.py``); R-2.9's QR rate scores
#: the ones the expected file holds.
QR_FIELDS = (
    "ruc_emisor",
    "tipo",
    "serie",
    "numero",
    "fecha_emision",
    "igv",
    "total",
    "num_doc_adq",
)


class Expected(BaseModel):
    """The expected reading of one golden pair, as its ``expected.json`` holds it (D127)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    has_qr: bool
    fields: dict[str, str]

    @model_validator(mode="after")
    def _refuse_unknown_fields(self) -> Self:
        """Raise ValueError for a field name the readers do not produce (D127)."""
        unknown = sorted(set(self.fields) - EXPECTED_FIELDS)
        if unknown:
            raise ValueError(f"unknown expected field(s) {', '.join(unknown)}")
        return self


@dataclass(frozen=True)
class Score:
    """One invoice's R-2.9 score: its QR verdict and its required-field count.

    ``qr_all_right`` is ``None`` when the expected file says the receipt carries no QR, so
    the invoice stays out of the QR rate.
    """

    qr_all_right: bool | None
    required_right: int
    required_total: int


@dataclass(frozen=True)
class Summary:
    """A golden set's aggregate R-2.9 rates: QR invoices fully right, OCR-only required fields right."""

    qr_rate: float
    required_rate: float

    def passes(self, qr_min: float = QR_MIN_RATE, ocr_min: float = OCR_MIN_RATE) -> bool:
        """Return whether both rates reach the R-2.9 targets."""
        return self.qr_rate >= qr_min and self.required_rate >= ocr_min


def load_expected(path: Path) -> Expected:
    """Read one D127 expected file; raise ValueError when it is not one (D127)."""
    return Expected.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def score(reading: Reading | None, expected: Expected) -> Score:
    """Return ``reading`` scored against ``expected`` (R-2.9)."""
    required_right = sum(1 for name in REQUIRED if _field_is_right(reading, name, expected.fields.get(name)))
    qr_all_right = None
    if expected.has_qr:
        qr_all_right = all(
            _field_is_right(reading, name, expected.fields[name])
            for name in QR_FIELDS
            if name in expected.fields
        )
    return Score(qr_all_right=qr_all_right, required_right=required_right, required_total=len(REQUIRED))


def summarize(scores: Iterable[Score]) -> Summary:
    """Return the aggregate R-2.9 rates of ``scores``.

    The QR rate covers the invoices with a QR; the required-field rate covers the OCR-only
    ones, those without a QR. A set with no invoice of either kind reports ``0.0`` for that
    rate, so :meth:`Summary.passes` fails it.
    """
    counted = list(scores)
    qr_present = sum(1 for score in counted if score.qr_all_right is not None)
    qr_right = sum(1 for score in counted if score.qr_all_right)
    ocr_only = [score for score in counted if score.qr_all_right is None]
    required_right = sum(score.required_right for score in ocr_only)
    required_total = sum(score.required_total for score in ocr_only)
    return Summary(
        qr_rate=qr_right / qr_present if qr_present else 0.0,
        required_rate=required_right / required_total if required_total else 0.0,
    )


def _field_is_right(reading: Reading | None, name: str, expected_value: str | None) -> bool:
    """Return whether ``reading`` holds ``name`` equal to the expected value (R-2.9)."""
    if reading is None or expected_value is None:
        return False
    actual = reading.value(name)
    return actual is not None and same_value(name, actual, expected_value)
