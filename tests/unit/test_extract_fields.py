"""Tests for the JSON form of a reading (R-2.6, R-2.8, D189)."""

from __future__ import annotations

from typing import Any

import pytest

from invoicebot.extract.fields import Field, Flag, Reading, reading_from_json, reading_to_json

#: From tests/synthetic_rucs.txt; no real RUC may appear here.
SYNTHETIC_RUC = "20000000001"
URL_STRIP = "hitps.//sy.example pe/tiemda"


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


def test_reading_with_empty_serie_reports_it_missing() -> None:
    """R-2.13, R-12.8: an empty field counts as absent, so a required field is missing."""
    reading = Reading(
        fields={"serie": Field(value="", source="ocr", confidence=1.0)},
        document_url=None,
    )

    assert "serie" in reading.missing_required()
    assert reading.value("serie") is None


def test_reading_with_serie_value_does_not_report_it_missing() -> None:
    """R-2.13, R-12.8: a required field with text is present, not missing."""
    reading = Reading(
        fields={"serie": Field(value="F001", source="ocr", confidence=1.0)},
        document_url=None,
    )

    assert "serie" not in reading.missing_required()
    assert reading.value("serie") == "F001"


def test_reading_round_trips_through_json() -> None:
    """R-2.6: a reading survives reading_to_json and reading_from_json unchanged."""
    reading = _reading()

    assert reading_from_json(reading_to_json(reading)) == reading


def test_reading_without_document_url_round_trips() -> None:
    """R-2.8: a reading with no document URL round-trips with it still None."""
    reading = Reading(fields={"total": Field(value="49.90", source="ocr", confidence=1.0)}, document_url=None)

    assert reading_from_json(reading_to_json(reading)) == reading


def test_reading_with_printed_url_round_trips() -> None:
    """D189: a reading with a printed URL survives the JSON round trip unchanged."""
    reading = Reading(
        fields={"total": Field(value="49.90", source="ocr", confidence=1.0)},
        document_url=None,
        printed_url="https://portal.example.pe/consulta",
    )

    payload = reading_to_json(reading)

    assert payload["printed_url"] == "https://portal.example.pe/consulta"
    assert reading_from_json(payload) == reading


def test_reading_without_printed_url_reads_as_none() -> None:
    """D189: a reading stored before ``printed_url`` existed loads with it None."""
    assert reading_from_json(_payload()).printed_url is None


def test_reading_with_url_strip_round_trips() -> None:
    """D191: a reading with a URL strip survives the JSON round trip unchanged."""
    reading = Reading(
        fields={"total": Field(value="49.90", source="ocr", confidence=1.0)},
        document_url=None,
        url_strip=URL_STRIP,
    )

    payload = reading_to_json(reading)

    assert payload["url_strip"] == URL_STRIP
    assert reading_from_json(payload) == reading


def test_reading_without_url_strip_reads_as_none() -> None:
    """D191: a reading stored before ``url_strip`` existed loads with it None."""
    assert reading_from_json(_payload()).url_strip is None


@pytest.mark.parametrize(
    "url_strip",
    [
        "a\nb",
        "",
        "a" * 121,
    ],
)
def test_reading_from_json_rejects_a_url_strip_of_the_wrong_shape(url_strip: object) -> None:
    """D191: a ``url_strip`` that is not 1 to 120 URL characters or null raises ValueError."""
    with pytest.raises(ValueError, match="url_strip"):
        reading_from_json({**_payload(), "url_strip": url_strip})


@pytest.mark.parametrize(
    "printed_url",
    [
        5,
        "http://portal.example.pe/consulta",
        "https://portal.example.pe/con\u202esulta",
        "https://Portal.Example.pe/consulta",
        "https://portal.example.pe/" + "a" * 200,
        "javascript:alert(1)",
    ],
)
def test_reading_from_json_rejects_a_printed_url_of_the_wrong_shape(printed_url: object) -> None:
    """D189: a ``printed_url`` that is not an https, ASCII, capped printed URL or null raises ValueError."""
    with pytest.raises(ValueError, match="printed_url"):
        reading_from_json({**_payload(), "printed_url": printed_url})


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
    ("value", "cleaned"),
    [
        pytest.param("ACME\x00 SAC", "ACME  SAC", id="nul"),
        pytest.param("F001\n", "F001", id="trailing-newline"),
        pytest.param("Razón\tSocial", "Razón Social", id="tab"),
        pytest.param("\x1b[31mX", "[31mX", id="escape"),
        pytest.param("Pollería Ñandú", "Pollería Ñandú", id="no-control-character"),
        pytest.param("Razón\u202eSocial", "Razón Social", id="bidi-override"),
    ],
)
def test_field_replaces_control_characters_with_spaces(value: str, cleaned: str) -> None:
    """R-12.8: a field value carries a space where a control character was, and no edges."""
    assert Field(value=value, source="ocr", confidence=1.0).value == cleaned


def test_reading_from_json_cleans_a_control_character() -> None:
    """R-12.8: a loaded field value carries a space where a control character was."""
    payload = {
        "fields": {"razon_social": {"value": "ACME\x07 SAC", "source": "ocr", "confidence": 1.0}},
        "document_url": None,
    }

    reading = reading_from_json(payload)

    assert reading.fields["razon_social"].value == "ACME  SAC"


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
