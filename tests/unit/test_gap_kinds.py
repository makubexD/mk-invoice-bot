"""Gap kinds: fingerprints, thresholds and the spec 06 §3 parity (R-11.2, D115).

The kind list is read from the capability-gaps table of
``specs/06-business-rules.md`` at test time, the way ``test_rules_parity.py``
reads the rule rows.
"""

import re
from functools import lru_cache
from pathlib import Path

import pytest

from invoicebot.gaps.kinds import GAP_KINDS, fingerprint
from invoicebot.rules.messages_es import KIND_DESC

SPEC_PATH = Path(__file__).resolve().parents[2] / "specs" / "06-business-rules.md"
_SECTION_START = "## 3. Capability gaps"
_SECTION_END = "### Gap lifecycle"
_KIND_ROW_RE = re.compile(r"^\|\s*`([A-Z_]+)`\s*\|")


@lru_cache(maxsize=1)
def _spec_kinds() -> tuple[str, ...]:
    text = SPEC_PATH.read_text(encoding="utf-8")
    section = text.split(_SECTION_START, 1)[1].split(_SECTION_END, 1)[0]
    kinds = []
    for line in section.splitlines():
        match = _KIND_ROW_RE.match(line)
        if match is None:
            continue
        cells = line.strip().strip("|").split("|")
        if len(cells) != 8:
            raise AssertionError(f"unparsed gap-table row in {SPEC_PATH.name}: {line!r}")
        kinds.append(match.group(1))
    return tuple(kinds)


def test_fingerprint_joins_the_prefix_and_the_parts() -> None:
    """BR-F02: an unknown store's gap is keyed by its kind prefix and RUC."""
    assert fingerprint("PORTAL_MISSING", "20100000001") == "portal_missing:20100000001"


def test_fingerprint_refuses_a_part_with_a_colon() -> None:
    """R-11.2: a part with ':' would merge two fingerprints, so it raises."""
    with pytest.raises(ValueError, match="contains ':'"):
        fingerprint("TEMPLATE_INVALID", "flow", "a:b")


def test_fingerprint_refuses_an_empty_part() -> None:
    """R-11.2: an empty part would make an ambiguous fingerprint, so it raises."""
    with pytest.raises(ValueError, match="empty fingerprint part"):
        fingerprint("PORTAL_MISSING", "")


def test_unrouted_buyer_raises_at_three_within_30_days() -> None:
    """BR-R06: a buyer without a flow is a gap only on its third hit in 30 days."""
    kind = GAP_KINDS["UNROUTED_BUYER"]
    assert kind.threshold == 3
    assert kind.window_days == 30


def test_parser_miss_raises_at_two() -> None:
    """BR-E06: the second unreadable invoice from an issuer raises the gap."""
    assert GAP_KINDS["PARSER_MISS"].threshold == 2


def test_every_spec_kind_is_a_gap_kind() -> None:
    """R-11.2: every kind in the spec 06 §3 table has a GAP_KINDS entry."""
    spec_kinds = _spec_kinds()
    missing = sorted(kind for kind in spec_kinds if kind not in GAP_KINDS)
    assert not missing, f"kinds in spec 06 §3 but missing from GAP_KINDS: {missing}"


def test_every_desc_key_is_a_kind_desc() -> None:
    """R-11.1: every kind's desc_key resolves to a KIND_DESC text."""
    missing = sorted(
        f"{kind}: {gap.desc_key!r}" for kind, gap in GAP_KINDS.items() if gap.desc_key not in KIND_DESC
    )
    assert not missing, f"desc_keys missing from KIND_DESC: {missing}"
