"""Tests for telling the issuer from the buyer by our own RUCs (R-2.4, D199)."""

from __future__ import annotations

from invoicebot.extract.fields import Field, Reading
from invoicebot.extract.parties import with_own_buyer

#: From tests/synthetic_rucs.txt; no real RUC may appear here.
OWN = "20000000001"
WORK = "20000000002"
STORE = "20000000003"
OWN_RUCS = frozenset({OWN, WORK})


def _reading(issuer: str, buyer: str | None) -> Reading:
    """Return a reading with the given issuer and buyer, each with its own confidence."""
    fields = {
        "ruc_emisor": Field(value=issuer, source="ocr", confidence=0.6),
        "tipo_doc_adq": Field(value="6", source="ocr", confidence=0.7),
        "total": Field(value="49.90", source="ocr", confidence=0.8),
    }
    if buyer is not None:
        fields["num_doc_adq"] = Field(value=buyer, source="ocr", confidence=0.4)
    return Reading(fields=fields, document_url="https://example.test/invoice")


def test_swaps_when_issuer_is_ours_and_buyer_is_not() -> None:
    """D199: our own RUC read as issuer becomes the buyer; each field keeps its confidence."""
    swapped = with_own_buyer(_reading(WORK, STORE), OWN_RUCS)

    assert swapped.value("ruc_emisor") == STORE
    assert swapped.value("num_doc_adq") == WORK
    assert swapped.fields["ruc_emisor"].confidence == 0.4
    assert swapped.fields["num_doc_adq"].confidence == 0.6


def test_issuer_not_ours_is_the_same_object() -> None:
    """D199: a reading whose issuer is not ours is returned as it is."""
    reading = _reading(STORE, WORK)

    assert with_own_buyer(reading, OWN_RUCS) is reading


def test_both_ours_is_unchanged() -> None:
    """D199: with both RUCs ours there is nothing to tell apart, so the reading is unchanged."""
    reading = _reading(OWN, WORK)

    assert with_own_buyer(reading, OWN_RUCS) == reading


def test_no_buyer_is_unchanged() -> None:
    """D199: an own issuer with no buyer read has nothing to swap with."""
    reading = _reading(WORK, None)

    assert with_own_buyer(reading, OWN_RUCS) == reading


def test_other_fields_survive_a_swap() -> None:
    """D199: a swap moves only the two RUC fields; the rest and the document URL stay."""
    reading = _reading(WORK, STORE)
    swapped = with_own_buyer(reading, OWN_RUCS)

    assert swapped.fields["tipo_doc_adq"] == reading.fields["tipo_doc_adq"]
    assert swapped.fields["total"] == reading.fields["total"]
    assert swapped.document_url == reading.document_url
