"""Tests for the near-RUC buyer match (D213)."""

from __future__ import annotations

from invoicebot.extract.buyer_match import match_buyer
from invoicebot.extract.fields import Field, Reading

RUC = "20123456789"
NAME = "RK EXPORT PERU S.A.C."
ROUTING = {RUC: NAME}


def _reading(read_ruc: str | None, read_name: str | None, **extra: Field) -> Reading:
    fields = dict(extra)
    if read_ruc is not None:
        fields["num_doc_adq_unchecked"] = Field(read_ruc, "ocr", 0.9)
    if read_name is not None:
        fields["nombre_adq"] = Field(read_name, "ocr", 0.8)
    return Reading(fields=fields, document_url=None)


def _off(ruc: str, digits: int) -> str:
    """Return ``ruc`` with its first ``digits`` digits changed."""
    return "".join(str((int(c) + 1) % 10) for c in ruc[:digits]) + ruc[digits:]


def test_near_ruc_and_garbled_name_match() -> None:
    """D213: a RUC 2 digits off with a garbled name matches the routing RUC."""
    result = match_buyer(_reading(_off(RUC, 2), "RC EXPORT PLRU-S:ADOS"), ROUTING)
    assert result.value("num_doc_adq") == RUC
    assert result.value("tipo_doc_adq") == "6"
    assert result.fields["num_doc_adq"].flag == "inferred"
    assert result.fields["num_doc_adq"].source == "ocr"


def test_three_digits_off_matches_four_does_not() -> None:
    """D213: 3 digits off matches, 4 digits off does not."""
    assert match_buyer(_reading(_off(RUC, 3), NAME), ROUTING).value("num_doc_adq") == RUC
    assert match_buyer(_reading(_off(RUC, 4), NAME), ROUTING).value("num_doc_adq") is None


def test_different_name_does_not_match() -> None:
    """D213: a near RUC with an unrelated name does not match."""
    reading = _reading(_off(RUC, 1), "CORPORACION BARLOVENTO S.A.C.")
    assert match_buyer(reading, ROUTING) is reading


def test_far_ruc_with_exact_name_does_not_match() -> None:
    """D213: the exact name with a RUC 5 digits off does not match."""
    reading = _reading(_off(RUC, 5), NAME)
    assert match_buyer(reading, ROUTING) is reading


def test_two_matching_routing_rucs_give_no_match() -> None:
    """D213: two routing RUCs that both match leave the reading unchanged."""
    reading = _reading(_off(RUC, 1), NAME)
    routing = {RUC: NAME, _off(RUC, 2): NAME}
    assert match_buyer(reading, routing) is reading


def test_existing_buyer_is_unchanged() -> None:
    """D213: a reading with num_doc_adq is returned unchanged."""
    reading = _reading(_off(RUC, 1), NAME, num_doc_adq=Field("20999999999", "ocr", 0.9))
    assert match_buyer(reading, ROUTING) is reading


def test_missing_hint_is_unchanged() -> None:
    """D213: a reading missing either hint is returned unchanged."""
    no_name = _reading(_off(RUC, 1), None)
    no_ruc = _reading(None, NAME)
    assert match_buyer(no_name, ROUTING) is no_name
    assert match_buyer(no_ruc, ROUTING) is no_ruc


def test_inferred_buyer_takes_lower_confidence_and_drops_hints() -> None:
    """D213: the inferred buyer has the lower hint's confidence and the hints are gone."""
    result = match_buyer(_reading(_off(RUC, 1), NAME), ROUTING)
    assert result.fields["num_doc_adq"].confidence == 0.8
    assert result.fields["tipo_doc_adq"].confidence == 0.8
    assert "num_doc_adq_unchecked" not in result.fields
    assert "nombre_adq" not in result.fields


def test_padron_name_without_letters_never_matches() -> None:
    """D213: a padrón name that squashes to nothing cannot vouch for a near RUC."""
    assert match_buyer(_reading(_off(RUC, 1), "RK EXPORT"), {RUC: "-- ."}).value("num_doc_adq") is None


def test_accented_padron_name_matches_its_unaccented_read() -> None:
    """D213: accents fold before comparing, so COMPAÑÍA matches an OCR read of COMPANIA."""
    routing = {RUC: "COMPAÑÍA ÑANDÚ S.A.C."}
    assert match_buyer(_reading(_off(RUC, 1), "COMPANIA NANDU SAC"), routing).value("num_doc_adq") == RUC


def test_issuers_own_ruc_never_matches_as_the_buyer() -> None:
    """D213: a routing RUC equal to the reading's issuer is never inferred as its buyer."""
    reading = _reading(_off(RUC, 1), "RK EXPORT PERU SAC", ruc_emisor=Field(RUC, "ocr", 0.95))
    assert match_buyer(reading, ROUTING).value("num_doc_adq") is None


def test_inferred_buyer_keeps_the_hints_source() -> None:
    """D213: a buyer matched from a PDF text layer's hints keeps source pdf."""
    reading = Reading(
        fields={
            "num_doc_adq_unchecked": Field(_off(RUC, 1), "pdf", 1.0),
            "nombre_adq": Field("RK EXPORT PERU SAC", "pdf", 1.0),
        },
        document_url=None,
    )
    assert match_buyer(reading, ROUTING).fields["num_doc_adq"].source == "pdf"
