"""Tests for the JSON form of a reading (R-2.6, R-2.8)."""

from __future__ import annotations

from typing import Any

import pytest

from invoicebot.extract.fields import Field, Flag, Reading, reading_from_json, reading_to_json

#: From tests/synthetic_rucs.txt; no real RUC may appear here.
SYNTHETIC_RUC = "20000000001"


def _reading() -> Reading:
    """Return a reading with two fields and a document URL."""
    return Reading(
        fields={
            "ruc_emisor": Field(value=SYNTHETIC_RUC, source="qr", confidence=1.0),
            "total": Field(value="49.90", source="ocr", confidence=0.5),
        },
        document_url="https://example.test/invoice",
    )


def _payload() -> dict[str, Any]:
    """Return a valid payload with one field."""
    return {
        "fields": {"total": {"value": "49.90", "source": "qr", "confidence": 1.0}},
        "document_url": None,
    }


def _payload_with_field(**overrides: object) -> dict[str, Any]:
    """Return a valid payload whose one field has ``overrides`` applied."""
    field: dict[str, object] = {"value": "49.90", "source": "qr", "confidence": 1.0}
    field.update(overrides)
    return {"fields": {"total": field}, "document_url": None}


def test_reading_round_trips_through_json() -> None:
    """R-2.6: a reading survives reading_to_json and reading_from_json unchanged."""
    reading = _reading()

    assert reading_from_json(reading_to_json(reading)) == reading


def test_reading_without_document_url_round_trips() -> None:
    """R-2.8: a reading with no document URL round-trips with it still None."""
    reading = Reading(fields={"total": Field(value="49.90", source="ocr", confidence=1.0)}, document_url=None)

    assert reading_from_json(reading_to_json(reading)) == reading


@pytest.mark.parametrize("flag", ["inferred", "conflict"])
def test_reading_with_each_flag_round_trips(flag: Flag) -> None:
    """R-2.6: a reading with a flagged field survives the JSON round trip unchanged."""
    reading = Reading(
        fields={"total": Field(value="49.90", source="ocr", confidence=0.9, flag=flag)},
        document_url=None,
    )

    assert reading_from_json(reading_to_json(reading)) == reading


def test_field_without_flag_loads_with_none() -> None:
    """R-2.6: a reading stored before flags existed loads with ``flag=None``."""
    reading = reading_from_json(_payload())

    assert reading.fields["total"].flag is None


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        pytest.param(["not", "a", "dict"], "reading must be an object", id="non-dict"),
        pytest.param({**_payload(), "extra": 1}, "reading must be an object", id="extra-top-level-key"),
        pytest.param(_payload_with_field(source="evil"), "source must be one of", id="unknown-source"),
        pytest.param(
            _payload_with_field(confidence=1.5),
            "confidence must be a finite number",
            id="confidence-above-one",
        ),
        pytest.param(
            _payload_with_field(confidence=float("nan")),
            "confidence must be a finite number",
            id="confidence-nan",
        ),
        pytest.param(
            _payload_with_field(confidence=2),
            "confidence must be a finite number",
            id="confidence-int-above-one",
        ),
        pytest.param(_payload_with_field(value=20000000001), "value must be a string", id="non-str-value"),
        pytest.param(_payload_with_field(flag="evil"), "flag must be one of", id="unknown-flag"),
    ],
)
def test_reading_from_json_rejects_an_invalid_payload(payload: object, match: str) -> None:
    """R-2.6: a payload outside the reading shape raises ValueError."""
    with pytest.raises(ValueError, match=match):
        reading_from_json(payload)


@pytest.mark.parametrize(
    ("payload", "match", "bad_value"),
    [
        pytest.param(
            _payload_with_field(value=20000000001),
            "value must be a string",
            str(20000000001),
            id="non-str-value",
        ),
        pytest.param(
            _payload_with_field(source="evil"), "source must be one of", "evil", id="unknown-source"
        ),
        pytest.param(
            {**_payload(), "extra": "LEAK"}, "reading must be an object", "LEAK", id="extra-key-value"
        ),
        pytest.param(_payload_with_field(flag="evil"), "flag must be one of", "evil", id="unknown-flag"),
    ],
)
def test_reading_from_json_error_never_quotes_the_bad_value(
    payload: object, match: str, bad_value: str
) -> None:
    """R-2.6: an invalid payload's error names only the field or key, never the value."""
    with pytest.raises(ValueError, match=match) as caught:
        reading_from_json(payload)

    assert bad_value not in str(caught.value)
