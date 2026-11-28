"""Tests for merging the OCR and QR readings (D81, R-2.6, D189, D210, D211)."""

from __future__ import annotations

from invoicebot.extract.fields import FULL_CONFIDENCE, Field, Flag, Reading
from invoicebot.extract.merge import best_of_reads, merge

OCR_CONFIDENCE = 0.7
DOCUMENT_URL = "https://example.test/invoice"
PRINTED_URL = "https://portal.example.pe/consulta"
URL_STRIP = "hitps.//sy.example pe/tiemda"


def _reading(
    fields: dict[str, Field],
    document_url: str | None = None,
    printed_url: str | None = None,
    url_strip: str | None = None,
) -> Reading:
    """Return a reading built from already-parsed fields."""
    return Reading(fields=fields, document_url=document_url, printed_url=printed_url, url_strip=url_strip)


def _ocr_field(value: str, confidence: float = OCR_CONFIDENCE, flag: Flag | None = None) -> Field:
    """Return an OCR-sourced field with the given confidence and flag."""
    return Field(value=value, source="ocr", confidence=confidence, flag=flag)


def _qr_field(value: str, flag: Flag | None = None) -> Field:
    """Return a QR-sourced field at full confidence, optionally flagged."""
    return Field(value=value, source="qr", confidence=FULL_CONFIDENCE, flag=flag)


def test_merge_equal_amount_takes_full_confidence() -> None:
    """D81: OCR `total` 12.50 at 0.7 and QR `total` 12.5 merge to 12.50 from OCR at 1.0 with no flag."""
    merged = merge(_reading({"total": _ocr_field("12.50")}), _reading({"total": _qr_field("12.5")}))

    assert merged.fields["total"] == Field(value="12.50", source="ocr", confidence=FULL_CONFIDENCE, flag=None)


def test_merge_different_amount_keeps_ocr_value_and_flags_conflict() -> None:
    """D81: OCR `total` 12.50 and QR `total` 21.50 keep the OCR value and confidence, flagged conflict."""
    merged = merge(_reading({"total": _ocr_field("12.50")}), _reading({"total": _qr_field("21.50")}))

    assert merged.fields["total"] == Field(
        value="12.50", source="ocr", confidence=OCR_CONFIDENCE, flag="conflict"
    )


def _agreeing_anchors() -> tuple[dict[str, Field], dict[str, Field]]:
    """Return OCR and QR fields that agree on `ruc_emisor` and `total`."""
    ocr = {"ruc_emisor": _ocr_field("20000000001"), "total": _ocr_field("12.50")}
    qr = {"ruc_emisor": _qr_field("20000000001"), "total": _qr_field("12.5")}
    return ocr, qr


def test_merge_corroborating_qr_wins_a_serie_and_numero_conflict() -> None:
    """D210: RUC and total agree, so the QR's `serie` and `numero` replace the OCR's, unflagged."""
    ocr, qr = _agreeing_anchors()
    ocr |= {"serie": _ocr_field("2026"), "numero": _ocr_field("222")}
    qr |= {"serie": _qr_field("F001"), "numero": _qr_field("222")}

    merged = merge(_reading(ocr), _reading(qr))

    assert merged.fields["serie"] == _qr_field("F001")
    assert merged.fields["numero"] == Field(value="222", source="ocr", confidence=FULL_CONFIDENCE, flag=None)


def test_merge_without_an_agreeing_anchor_flags_the_ocr_value_as_conflict() -> None:
    """D211: the only shared anchor disagrees, so D81 holds: the OCR value stays, flagged conflict."""
    ocr = {"ruc_emisor": _ocr_field("20000000001"), "serie": _ocr_field("2026")}
    qr = {"ruc_emisor": _qr_field("20000000002"), "serie": _qr_field("F001")}

    merged = merge(_reading(ocr), _reading(qr))

    assert merged.fields["serie"] == Field(
        value="2026", source="ocr", confidence=OCR_CONFIDENCE, flag="conflict"
    )
    assert merged.fields["ruc_emisor"].flag == "conflict"


def test_merge_inferred_qr_anchor_is_no_evidence_so_the_qr_wins() -> None:
    """D211: the only shared anchor is inferred on the QR, so nothing contradicts it and its `serie` wins."""
    ocr = {"ruc_emisor": _ocr_field("20000000001"), "serie": _ocr_field("2026")}
    qr = {"ruc_emisor": _qr_field("20000000002", "inferred"), "serie": _qr_field("F001")}

    merged = merge(_reading(ocr), _reading(qr))

    assert merged.fields["serie"] == _qr_field("F001")


def test_merge_ocr_without_anchors_takes_the_qr_serie_and_numero() -> None:
    """D211: the OCR read no anchor, so the QR is not contradicted and its `serie` and `numero` win."""
    ocr = {"serie": _ocr_field("2026"), "numero": _ocr_field("222"), "base": _ocr_field("135.94")}
    qr = {
        "ruc_emisor": _qr_field("20000000001"),
        "serie": _qr_field("F001"),
        "numero": _qr_field("12367"),
    }

    merged = merge(_reading(ocr), _reading(qr))

    assert (merged.fields["serie"], merged.fields["numero"]) == (_qr_field("F001"), _qr_field("12367"))
    assert merged.fields["base"] == _ocr_field("135.94")


def test_merge_one_agreeing_anchor_outweighs_a_disagreeing_one() -> None:
    """D210, D211: RUC agrees and total disagrees, so the QR wins the total and the `serie`."""
    ocr = {"ruc_emisor": _ocr_field("20000000001"), "total": _ocr_field("16.41"), "serie": _ocr_field("2026")}
    qr = {"ruc_emisor": _qr_field("20000000001"), "total": _qr_field("160.41"), "serie": _qr_field("F001")}

    merged = merge(_reading(ocr), _reading(qr))

    assert (merged.fields["total"], merged.fields["serie"]) == (_qr_field("160.41"), _qr_field("F001"))


def test_merge_with_a_url_only_qr_keeps_the_ocr_reading() -> None:
    """D210: a QR with no fields (URL only) leaves the OCR fields unchanged."""
    ocr = {"serie": _ocr_field("F001")}

    merged = merge(_reading(ocr), _reading({}, document_url=DOCUMENT_URL))

    assert merged.fields == ocr


def test_merge_keeps_qr_only_field() -> None:
    """D81: `num_doc_adq` only in the QR comes through as that QR field."""
    qr_field = _qr_field("20000000001")
    merged = merge(_reading({}), _reading({"num_doc_adq": qr_field}))

    assert merged.fields["num_doc_adq"] == qr_field


def test_merge_keeps_ocr_only_field() -> None:
    """D81: `base` only in the OCR comes through as that OCR field."""
    ocr_field = _ocr_field("42.29")
    merged = merge(_reading({"base": ocr_field}), _reading({}))

    assert merged.fields["base"] == ocr_field


def test_merge_without_qr_returns_the_ocr_reading() -> None:
    """D81: a missing QR reading leaves the OCR reading unchanged."""
    ocr = _reading({"total": _ocr_field("12.50")}, document_url=DOCUMENT_URL)

    assert merge(ocr, None) is ocr


def test_merge_without_ocr_returns_the_qr_reading() -> None:
    """D81: a missing OCR reading leaves the QR reading unchanged."""
    qr = _reading({"total": _qr_field("12.50")})

    assert merge(None, qr) is qr


def test_merge_without_readings_returns_an_empty_reading() -> None:
    """D81: two missing readings merge to an empty reading with no document URL."""
    assert merge(None, None) == Reading(fields={}, document_url=None)


def test_merge_takes_document_url_from_the_qr() -> None:
    """D81: a QR that carries only `document_url` adds it beside the OCR fields."""
    ocr_field = _ocr_field("12.50")
    merged = merge(_reading({"total": ocr_field}), _reading({}, document_url=DOCUMENT_URL))

    assert merged.document_url == DOCUMENT_URL
    assert merged.fields == {"total": ocr_field}


def test_merge_keeps_the_ocr_printed_url() -> None:
    """D189: a merged reading keeps the OCR reading's printed URL."""
    merged = merge(
        _reading({"total": _ocr_field("12.50")}, printed_url=PRINTED_URL),
        _reading({"total": _qr_field("12.50")}),
    )

    assert merged.printed_url == PRINTED_URL


def test_merge_without_ocr_has_no_printed_url() -> None:
    """D189: a QR-only merge carries no printed URL."""
    assert merge(None, _reading({"total": _qr_field("12.50")})).printed_url is None


def test_merge_without_qr_keeps_the_ocr_printed_url() -> None:
    """D189: a merge with no QR reading keeps the OCR printed URL."""
    ocr = _reading({"total": _ocr_field("12.50")}, printed_url=PRINTED_URL)

    assert merge(ocr, None).printed_url == PRINTED_URL


def test_merge_keeps_the_ocr_url_strip() -> None:
    """D191: a merged reading keeps the OCR reading's URL strip."""
    merged = merge(
        _reading({"total": _ocr_field("12.50")}, url_strip=URL_STRIP),
        _reading({"total": _qr_field("12.50")}),
    )

    assert merged.url_strip == URL_STRIP


def test_merge_keeps_tipo_inferred_when_both_readers_inferred_it() -> None:
    """R-2.6: an OCR and a QR `tipo` both inferred and equal stay inferred at the OCR confidence."""
    ocr_field = _ocr_field("01", flag="inferred")
    merged = merge(_reading({"tipo": ocr_field}), _reading({"tipo": _qr_field("01", flag="inferred")}))

    assert merged.fields["tipo"] == ocr_field


def test_merge_confirms_an_inferred_qr_tipo() -> None:
    """D81: an inferred QR `tipo` equal to the OCR `tipo` reaches 1.0 with no flag."""
    merged = merge(
        _reading({"tipo": _ocr_field("01")}),
        _reading({"tipo": _qr_field("01", flag="inferred")}),
    )

    assert merged.fields["tipo"] == Field(value="01", source="ocr", confidence=FULL_CONFIDENCE, flag=None)


BUYER_RUC = "20000000010"
OTHER_RUC = "20000000001"


def test_best_of_reads_takes_absent_required_field() -> None:
    """D200: `kept` lacks `fecha_emision`, `others[0]` has it at 0.7: filled, rest unchanged."""
    kept = _reading({"total": _ocr_field("12.50", 0.9)})
    other = _reading({"fecha_emision": _ocr_field("2026-11-01", 0.7)})

    result = best_of_reads(kept, [other])

    assert result.fields["fecha_emision"] == _ocr_field("2026-11-01", 0.7)
    assert result.fields["total"] == kept.fields["total"]
    assert list(result.fields) == ["total", "fecha_emision"]


def test_best_of_reads_keeps_present_field_and_returns_kept() -> None:
    """D200: `kept` has `total` 49.90 and `others[0]` has 12.50: `kept` is returned itself."""
    kept = _reading({"total": _ocr_field("49.90")})

    result = best_of_reads(kept, [_reading({"total": _ocr_field("12.50")})])

    assert result is kept
    assert result.fields["total"].value == "49.90"


def test_best_of_reads_takes_first_other_that_has_it() -> None:
    """D200: two others give different `fecha_emision` values: the first one's is taken."""
    first = _reading({"fecha_emision": _ocr_field("2026-11-01")})
    second = _reading({"fecha_emision": _ocr_field("2026-11-02")})

    result = best_of_reads(_reading({}), [first, second])

    assert result.fields["fecha_emision"].value == "2026-11-01"


def test_best_of_reads_serie_and_numero_come_from_one_reading() -> None:
    """D200: `kept` lacks both; `others[0]` has only `serie`, `others[1]` has both: both from it."""
    only_serie = _reading({"serie": _ocr_field("F001")})
    both = _reading({"serie": _ocr_field("F002"), "numero": _ocr_field("15")})

    result = best_of_reads(_reading({}), [only_serie, both])

    assert result.fields["serie"].value == "F002"
    assert result.fields["numero"].value == "15"


def test_best_of_reads_takes_a_full_pair_when_kept_has_only_serie() -> None:
    """D212: `kept` has `serie` but no `numero`, `others[0]` has both: the pair is taken together."""
    kept = _reading({"serie": _ocr_field("F001")})
    other = _reading({"serie": _ocr_field("F002"), "numero": _ocr_field("15")})

    result = best_of_reads(kept, [other])

    assert result.fields["serie"].value == "F002"
    assert result.fields["numero"].value == "15"


def test_best_of_reads_skips_buyer_ruc_as_issuer() -> None:
    """D200: a `ruc_emisor` candidate equal to the buyer `num_doc_adq` is skipped for the next read."""
    kept = _reading({"num_doc_adq": _ocr_field(BUYER_RUC)})
    buyer_read = _reading({"ruc_emisor": _ocr_field(BUYER_RUC)})
    issuer_read = _reading({"ruc_emisor": _ocr_field(OTHER_RUC)})

    result = best_of_reads(kept, [buyer_read, issuer_read])

    assert result.fields["ruc_emisor"].value == OTHER_RUC


def test_best_of_reads_ignores_optional_fields() -> None:
    """D200: `kept` lacks `igv` (not required) and `others[0]` has it: `igv` stays missing."""
    kept = _reading({"total": _ocr_field("12.50")})

    result = best_of_reads(kept, [_reading({"igv": _ocr_field("1.90")})])

    assert result is kept
    assert "igv" not in result.fields


def test_best_of_reads_with_no_others_returns_kept() -> None:
    """D200: with no other reads, `kept` is returned itself."""
    kept = _reading({}, document_url=DOCUMENT_URL)

    assert best_of_reads(kept, []) is kept


def test_best_of_reads_leaves_a_field_the_qr_has() -> None:
    """D200: `kept` lacks `total`, the QR has it and `others[0]` read 4990: `total` is not filled."""
    kept = _reading({"fecha_emision": _ocr_field("2026-11-01")})

    result = best_of_reads(kept, [_reading({"total": _ocr_field("4990")})], frozenset({"total"}))

    assert result is kept


def test_best_of_reads_leaves_serie_and_numero_when_the_qr_has_one() -> None:
    """D200: `kept` lacks both, the QR has `serie` and `others[0]` has both: neither is filled."""
    other = _reading({"serie": _ocr_field("F002"), "numero": _ocr_field("15")})

    result = best_of_reads(_reading({}), [other], frozenset({"serie"}))

    assert "serie" not in result.fields
    assert "numero" not in result.fields


def test_best_of_reads_brings_the_buyer_with_a_filled_issuer() -> None:
    """D200, D199: `kept` has no RUCs and `others[0]` has issuer and buyer: both come from it."""
    other = _reading({"ruc_emisor": _ocr_field(OTHER_RUC), "num_doc_adq": _ocr_field(BUYER_RUC)})

    result = best_of_reads(_reading({}), [other])

    assert result.fields["ruc_emisor"].value == OTHER_RUC
    assert result.fields["num_doc_adq"].value == BUYER_RUC


def test_best_of_reads_keeps_the_buyer_kept_has() -> None:
    """D200: `kept` has a buyer, `others[0]` has issuer and another buyer: only the issuer is filled."""
    kept = _reading({"num_doc_adq": _ocr_field(BUYER_RUC)})
    other = _reading({"ruc_emisor": _ocr_field(OTHER_RUC), "num_doc_adq": _ocr_field("20000000002")})

    result = best_of_reads(kept, [other])

    assert result.fields["num_doc_adq"].value == BUYER_RUC
    assert result.fields["ruc_emisor"].value == OTHER_RUC


def test_best_of_reads_takes_the_pair_with_the_better_numero() -> None:
    """D212: `kept` numero at 0.18 and `others[0]` serie+numero at 0.61: the other's pair is taken."""
    kept = _reading({"serie": _ocr_field("F001", 0.9), "numero": _ocr_field("99", 0.18)})
    other = _reading({"serie": _ocr_field("F002", 0.8), "numero": _ocr_field("15", 0.61)})

    result = best_of_reads(kept, [other])

    assert result.fields["serie"].value == "F002"
    assert result.fields["numero"].value == "15"


def test_best_of_reads_takes_a_better_field_over_a_present_one() -> None:
    """D212: `kept` fecha_emision at 0.0 and `others[0]` at 0.6: the other's is taken."""
    kept = _reading({"fecha_emision": _ocr_field("2026-01-01", 0.0)})
    other = _reading({"fecha_emision": _ocr_field("2026-11-01", 0.6)})

    result = best_of_reads(kept, [other])

    assert result.fields["fecha_emision"].value == "2026-11-01"


def test_best_of_reads_tie_keeps_kept() -> None:
    """D212: equal confidence keeps `kept`'s field and returns `kept` itself."""
    kept = _reading({"total": _ocr_field("49.90", 0.7)})

    result = best_of_reads(kept, [_reading({"total": _ocr_field("12.50", 0.7)})])

    assert result is kept


def test_best_of_reads_tie_between_others_takes_the_earlier() -> None:
    """D212: two others tie above `kept`: the earlier one's field is taken."""
    kept = _reading({"total": _ocr_field("1.00", 0.1)})
    first = _reading({"total": _ocr_field("2.00", 0.8)})
    second = _reading({"total": _ocr_field("3.00", 0.8)})

    assert best_of_reads(kept, [first, second]).fields["total"].value == "2.00"


def test_best_of_reads_does_not_split_a_pair_for_a_lone_numero() -> None:
    """D212: `others[0]` has only `numero`, at higher confidence: `kept`'s pair stays whole."""
    kept = _reading({"serie": _ocr_field("F001", 0.5), "numero": _ocr_field("15", 0.5)})
    other = _reading({"numero": _ocr_field("99", 0.95)})

    result = best_of_reads(kept, [other])

    assert result is kept


def test_best_of_reads_pair_is_scored_by_its_lower_confidence() -> None:
    """D212: `others[0]` pair scores 0.4 (its lower part), below `kept`'s 0.5: `kept` stays."""
    kept = _reading({"serie": _ocr_field("F001", 0.5), "numero": _ocr_field("15", 0.5)})
    other = _reading({"serie": _ocr_field("F002", 0.99), "numero": _ocr_field("16", 0.4)})

    assert best_of_reads(kept, [other]) is kept


def test_best_of_reads_never_takes_a_qr_field_at_higher_confidence() -> None:
    """D212: `total` is in `qr_names`, so a higher-confidence `others[0]` total is not taken."""
    kept = _reading({"total": _ocr_field("49.90", 0.1)})

    result = best_of_reads(kept, [_reading({"total": _ocr_field("12.50", 0.9)})], frozenset({"total"}))

    assert result is kept


def test_best_of_reads_skips_a_better_issuer_equal_to_the_buyer() -> None:
    """D212, D199: a higher-confidence `ruc_emisor` equal to `kept`'s buyer is not taken."""
    kept = _reading({"ruc_emisor": _ocr_field(OTHER_RUC, 0.2), "num_doc_adq": _ocr_field(BUYER_RUC)})
    other = _reading({"ruc_emisor": _ocr_field(BUYER_RUC, 0.9)})

    assert best_of_reads(kept, [other]) is kept


def test_best_of_reads_takes_buyer_hints_read_only_by_another_pass() -> None:
    """D213: `kept` has no buyer and no hints, `others[0]` has both: both are taken."""
    kept = _reading({"total": _ocr_field("12.50")})
    other = _reading({"num_doc_adq_unchecked": _ocr_field("20000000019"), "nombre_adq": _ocr_field("RK SAC")})

    result = best_of_reads(kept, [other])

    assert result.fields["num_doc_adq_unchecked"] == other.fields["num_doc_adq_unchecked"]
    assert result.fields["nombre_adq"] == other.fields["nombre_adq"]


def test_best_of_reads_takes_the_higher_confidence_buyer_name() -> None:
    """D213: of `kept` at 0.4 and `others[0]` at 0.8, the `nombre_adq` of `others[0]` wins."""
    kept = _reading({"nombre_adq": _ocr_field("RK SAK", 0.4)})
    other = _reading({"nombre_adq": _ocr_field("RK SAC", 0.8)})

    assert best_of_reads(kept, [other]).fields["nombre_adq"] == other.fields["nombre_adq"]


def test_best_of_reads_buyer_hint_tie_keeps_kept() -> None:
    """D213: equal confidence for `nombre_adq` keeps `kept`'s, and `kept` itself is returned."""
    kept = _reading({"nombre_adq": _ocr_field("RK SAK", 0.6)})
    other = _reading({"nombre_adq": _ocr_field("RK SAC", 0.6)})

    assert best_of_reads(kept, [other]) is kept


def test_best_of_reads_drops_buyer_hints_when_the_result_has_a_buyer() -> None:
    """D213: `kept` has `num_doc_adq` and a hint, `others[0]` has a hint: no hint is returned."""
    kept = _reading({"num_doc_adq": _ocr_field(BUYER_RUC), "nombre_adq": _ocr_field("RK SAK")})
    other = _reading({"num_doc_adq_unchecked": _ocr_field("20000000019")})

    result = best_of_reads(kept, [other])

    assert "nombre_adq" not in result.fields
    assert "num_doc_adq_unchecked" not in result.fields
    assert result.fields["num_doc_adq"] == kept.fields["num_doc_adq"]


def test_best_of_reads_printed_ruc_outranks_a_repaired_one() -> None:
    """D212: a repaired (inferred) issuer RUC never displaces a printed one, whatever its confidence."""
    kept = _reading({"ruc_emisor": _ocr_field("20000000001", confidence=0.5)})
    other = _reading({"ruc_emisor": _ocr_field("20000000028", confidence=0.95, flag="inferred")})
    assert best_of_reads(kept, [other]).value("ruc_emisor") == "20000000001"


def test_best_of_reads_printed_ruc_from_another_pass_replaces_a_repaired_one() -> None:
    """D212: a printed issuer RUC from another pass replaces kept's repaired one."""
    kept = _reading({"ruc_emisor": _ocr_field("20000000028", confidence=0.95, flag="inferred")})
    other = _reading({"ruc_emisor": _ocr_field("20000000001", confidence=0.5)})
    assert best_of_reads(kept, [other]).value("ruc_emisor") == "20000000001"


def test_best_of_reads_keeps_a_printed_issuer_over_a_higher_confidence_one() -> None:
    """D212: a printed issuer in kept stays; another pass's RUC may be the buyer read as the issuer."""
    kept = _reading({"ruc_emisor": _ocr_field("20000000001", confidence=0.7)})
    other = _reading({"ruc_emisor": _ocr_field("20000000028", confidence=0.95)})
    assert best_of_reads(kept, [other]).value("ruc_emisor") == "20000000001"


def test_best_of_reads_issuer_from_another_pass_brings_its_buyer_document_type() -> None:
    """D212: a buyer brought with another pass's issuer comes with its tipo_doc_adq."""
    kept = _reading({"tipo": _ocr_field("01")})
    other = _reading(
        {
            "ruc_emisor": _ocr_field("20000000001"),
            "num_doc_adq": _ocr_field("20000000028"),
            "tipo_doc_adq": _ocr_field("6", flag="inferred"),
        }
    )
    assert best_of_reads(kept, [other]).value("tipo_doc_adq") == "6"
