"""Tests for confirming a photo reading with a matching invoice XML (R-5.6, D202)."""

from __future__ import annotations

from invoicebot.extract.confirm import confirmed_by_xml, confirmed_names
from invoicebot.extract.fields import FULL_CONFIDENCE, Field, Flag, Reading

#: From tests/synthetic_rucs.txt; no real RUC may appear here.
RUC = "20000000003"


def _ocr(value: str, flag: Flag | None = None) -> Field:
    """Return a photo field with low confidence."""
    return Field(value=value, source="ocr", confidence=0.43, flag=flag)


def _xml(value: str) -> Field:
    """Return an XML field."""
    return Field(value=value, source="xml", confidence=FULL_CONFIDENCE)


def _reading(fields: dict[str, Field], url: str | None = None) -> Reading:
    """Return a reading over ``fields``."""
    return Reading(fields=fields, document_url=url)


def test_agreeing_serie_becomes_the_xml_field() -> None:
    """D202: a serie the XML agrees with is the XML's field, full confidence, no flag."""
    result = confirmed_by_xml(_reading({"serie": _ocr("F001")}), _reading({"serie": _xml("F001")}))

    assert result.fields["serie"] == Field(value="F001", source="xml", confidence=FULL_CONFIDENCE)


def test_numero_agrees_as_an_integer() -> None:
    """D202: numero 00123 and 123 agree; the result carries the XML's field."""
    result = confirmed_by_xml(_reading({"numero": _ocr("00123")}), _reading({"numero": _xml("123")}))

    assert result.fields["numero"] == _xml("123")


def test_amount_agrees_by_value() -> None:
    """D202: total 49.9 and 49.90 agree as amounts."""
    result = confirmed_by_xml(_reading({"total": _ocr("49.9")}), _reading({"total": _xml("49.90")}))

    assert result.fields["total"] == _xml("49.90")


def test_disagreeing_field_keeps_the_reading() -> None:
    """D202: a fecha_emision that differs stays as the photo read it."""
    result = confirmed_by_xml(
        _reading({"fecha_emision": _ocr("2026-10-27")}), _reading({"fecha_emision": _xml("2026-10-28")})
    )

    assert result.fields["fecha_emision"] == _ocr("2026-10-27")
    assert result.fields["fecha_emision"].source == "ocr"


def test_missing_field_is_added_from_the_xml() -> None:
    """D202: an igv the photo lacks is added from the XML."""
    result = confirmed_by_xml(_reading({"serie": _ocr("F001")}), _reading({"igv": _xml("7.61")}))

    assert result.fields["igv"] == _xml("7.61")


def test_reading_only_field_and_url_stay() -> None:
    """D202: a field the XML lacks keeps its value, and document_url is the reading's."""
    reading = _reading({"num_doc_adq": _ocr(RUC), "serie": _ocr("F001")}, url="https://example.test/invoice")
    result = confirmed_by_xml(reading, _reading({"serie": _xml("F001")}, url="https://example.test/xml"))

    assert result.fields["num_doc_adq"] == _ocr(RUC)
    assert list(result.fields) == ["num_doc_adq", "serie"]
    assert result.document_url == "https://example.test/invoice"


def test_agreeing_conflict_flag_is_cleared() -> None:
    """D202: a conflict-flagged field that the XML agrees with is replaced, flag None."""
    result = confirmed_by_xml(
        _reading({"serie": _ocr("F001", flag="conflict")}), _reading({"serie": _xml("F001")})
    )

    assert result.fields["serie"].flag is None
    assert result.fields["serie"].source == "xml"


def test_confirmed_names_lists_only_fields_that_changed_to_xml() -> None:
    """D202: serie changed to xml; ruc_emisor was already xml, so only serie is listed."""
    before = _reading({"ruc_emisor": _xml(RUC), "serie": _ocr("F001")})
    xml = _reading({"ruc_emisor": _xml(RUC), "serie": _xml("F001")})

    assert confirmed_names(before, confirmed_by_xml(before, xml)) == ("serie",)
