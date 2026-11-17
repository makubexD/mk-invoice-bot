"""Tests for golden-set scoring (R-2.9, D127)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from invoicebot.extract.accuracy import (
    Expected,
    Score,
    Summary,
    load_expected,
    score,
    summarize,
)
from invoicebot.extract.fields import FULL_CONFIDENCE, REQUIRED, Field, Reading

# Synthetic values, as tests/synthetic_rucs.txt and config/example.yaml carry them.
SYNTHETIC_RUC = "20000000001"
FIELDS = {
    "ruc_emisor": SYNTHETIC_RUC,
    "tipo": "01",
    "serie": "F001",
    "numero": "15160",
    "fecha_emision": "2026-11-13",
    "total": "49.90",
    "igv": "7.36",
}


def _reading(fields: dict[str, str]) -> Reading:
    """Return an OCR-sourced reading holding ``fields``."""
    return Reading(
        fields={
            name: Field(value=value, source="ocr", confidence=FULL_CONFIDENCE)
            for name, value in fields.items()
        },
        document_url=None,
    )


def _expected(**changes: str) -> Expected:
    """Return a QR-carrying expected file built from ``FIELDS`` with ``changes`` applied."""
    return Expected(has_qr=True, fields={**FIELDS, **changes})


def _write(tmp_path: Path, document: object) -> Path:
    """Write ``document`` as an expected JSON file and return its path."""
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_score_exact_reading_is_all_right() -> None:
    """R-2.9: a reading that matches every expected field is fully right."""
    result = score(_reading(FIELDS), _expected())

    assert result == Score(qr_all_right=True, required_right=len(REQUIRED), required_total=len(REQUIRED))


@pytest.mark.parametrize(("actual", "expected_value"), [("10.0", "10.00"), ("10.00", "10.0")])
def test_score_amounts_compare_as_decimals(actual: str, expected_value: str) -> None:
    """R-2.9: an amount written `10.0` equals the expected `10.00`, so the field is right."""
    result = score(_reading({**FIELDS, "total": actual}), _expected(total=expected_value))

    assert result.required_right == len(REQUIRED)
    assert result.qr_all_right is True


def test_score_missing_total_counts_wrong() -> None:
    """R-2.9: an expected `total` the reading lacks is wrong and breaks the QR verdict."""
    fields = {name: value for name, value in FIELDS.items() if name != "total"}

    result = score(_reading(fields), _expected())

    assert result.required_right == len(REQUIRED) - 1
    assert result.qr_all_right is False


@pytest.mark.parametrize(("qr_right", "expected_pass"), [(94, False), (95, True)])
def test_summarize_qr_rate_boundary(qr_right: int, expected_pass: bool) -> None:
    """R-2.9: a set at 94 % QR fails and one at 95 % QR passes."""
    scores = [
        Score(
            qr_all_right=index < qr_right,
            required_right=len(REQUIRED),
            required_total=len(REQUIRED),
        )
        for index in range(100)
    ]
    scores.append(Score(qr_all_right=None, required_right=len(REQUIRED), required_total=len(REQUIRED)))

    assert summarize(scores).passes() is expected_pass


def test_score_unreadable_reading_counts_every_field_wrong() -> None:
    """R-2.9: a reading of `None` (unreadable) counts every field wrong."""
    result = score(None, _expected())

    assert result.required_right == 0
    assert result.required_total == len(REQUIRED)
    assert result.qr_all_right is False


def test_score_without_qr_has_no_qr_verdict() -> None:
    """R-2.9: an expected file with `has_qr` false leaves the invoice out of the QR rate."""
    result = score(_reading(FIELDS), Expected(has_qr=False, fields=FIELDS))

    assert result.qr_all_right is None


def test_summarize_reports_rates_over_the_set() -> None:
    """R-2.9: the QR rate covers QR invoices and the required rate covers OCR-only invoices."""
    scores = [
        Score(qr_all_right=True, required_right=len(REQUIRED), required_total=len(REQUIRED)),
        Score(qr_all_right=None, required_right=3, required_total=len(REQUIRED)),
    ]

    summary = summarize(scores)

    assert summary == Summary(qr_rate=1.0, required_rate=3 / len(REQUIRED))
    assert summary.passes() is False


def test_summarize_empty_set_fails() -> None:
    """R-2.9: a set with no scores cannot show the targets and fails."""
    summary = summarize([])

    assert summary == Summary(qr_rate=0.0, required_rate=0.0)
    assert summary.passes() is False


def test_load_expected_reads_d127_file(tmp_path: Path) -> None:
    """D127: an expected file loads its `has_qr` and `fields`."""
    path = _write(tmp_path, {"has_qr": True, "fields": FIELDS})

    assert load_expected(path) == Expected(has_qr=True, fields=FIELDS)


@pytest.mark.parametrize(
    ("document", "match"),
    [
        pytest.param(
            {"has_qr": True, "fields": FIELDS, "extra": 1},
            "Extra inputs are not permitted",
            id="top-level-key",
        ),
        pytest.param(
            {"has_qr": True, "fields": {**FIELDS, "seller": "x"}},
            "unknown expected field",
            id="field-name",
        ),
        pytest.param(
            {"has_qr": True, "fields": {"total": 49.9}},
            "valid string",
            id="non-string-value",
        ),
    ],
)
def test_load_expected_refuses_unknown_keys(tmp_path: Path, document: object, match: str) -> None:
    """D127: an unknown key, field name or non-string value is refused."""
    with pytest.raises(ValueError, match=match):
        load_expected(_write(tmp_path, document))
