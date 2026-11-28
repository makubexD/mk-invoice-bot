"""Match a misread buyer to a RUC a routing rule names, by a near RUC and its name (D213)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from difflib import SequenceMatcher

from invoicebot.extract.fields import BUYER_NAME, UNCHECKED_BUYER_RUC, Field, Reading
from invoicebot.extract.ocr_parse import RUC_DOCUMENT_TYPE, RUC_LENGTH

MAX_BUYER_RUC_DIGIT_DIFFS = 3
MIN_BUYER_NAME_RATIO = 0.7
_NOT_ALNUM = re.compile(r"[^A-Z0-9]")


def _squash(name: str) -> str:
    return _NOT_ALNUM.sub("", name.upper())


def _is_match(ruc: str, expected_name: str, read_ruc: str, read_name: str) -> bool:
    if len(ruc) != RUC_LENGTH or not ruc.isdigit() or len(read_ruc) != RUC_LENGTH:
        return False
    if sum(a != b for a, b in zip(ruc, read_ruc, strict=True)) > MAX_BUYER_RUC_DIGIT_DIFFS:
        return False
    expected = _squash(expected_name)
    ratio = SequenceMatcher(None, _squash(read_name)[: len(expected)], expected).ratio()
    return ratio >= MIN_BUYER_NAME_RATIO


def match_buyer(reading: Reading, names_by_ruc: Mapping[str, str]) -> Reading:
    """Return ``reading`` with the buyer inferred when exactly one routing RUC matches.

    A routing RUC matches when it is within ``MAX_BUYER_RUC_DIGIT_DIFFS`` digits of the
    unchecked buyer RUC and the read name is similar to its razón social. The result is
    ``inferred`` and drops both hints; with a buyer already read, a missing hint, or zero
    or several matches, ``reading`` is returned unchanged.
    """
    ruc_hint = reading.fields.get(UNCHECKED_BUYER_RUC)
    name_hint = reading.fields.get(BUYER_NAME)
    if reading.value("num_doc_adq") is not None or ruc_hint is None or name_hint is None:
        return reading
    matches = [
        ruc for ruc, name in names_by_ruc.items() if _is_match(ruc, name, ruc_hint.value, name_hint.value)
    ]
    if len(matches) != 1:
        return reading
    confidence = min(ruc_hint.confidence, name_hint.confidence)
    fields = {k: v for k, v in reading.fields.items() if k not in (UNCHECKED_BUYER_RUC, BUYER_NAME)}
    fields["num_doc_adq"] = Field(matches[0], "ocr", confidence, "inferred")
    fields["tipo_doc_adq"] = Field(RUC_DOCUMENT_TYPE, "ocr", confidence, "inferred")
    return replace(reading, fields=fields)
