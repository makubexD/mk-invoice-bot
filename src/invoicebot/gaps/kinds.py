"""The five gap kinds of spec 06 §3, their fingerprint prefixes and raise thresholds (D115).

A gap is deduplicated by fingerprint, so the prefix plus its parts must name the
same missing capability every time: ten invoices from one unknown issuer share
``portal_missing:<ruc>``. The Spanish text of each kind lives in
``invoicebot.rules.messages_es``; this module holds only its key.
"""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class GapKind:
    """One row of spec 06 §3: how its fingerprint is built and when it is raised."""

    kind: str
    fingerprint_prefix: str
    threshold: int
    window_days: int | None
    desc_key: str


GAP_KINDS: Mapping[str, GapKind] = {
    "PORTAL_MISSING": GapKind(
        kind="PORTAL_MISSING",
        fingerprint_prefix="portal_missing",
        threshold=1,
        window_days=None,
        desc_key="PORTAL_MISSING",
    ),
    "PORTAL_BROKEN": GapKind(
        kind="PORTAL_BROKEN",
        fingerprint_prefix="portal_broken",
        threshold=1,
        window_days=None,
        desc_key="PORTAL_BROKEN",
    ),
    "TEMPLATE_INVALID": GapKind(
        kind="TEMPLATE_INVALID",
        fingerprint_prefix="template_invalid",
        threshold=1,
        window_days=None,
        desc_key="TEMPLATE_INVALID",
    ),
    "PARSER_MISS": GapKind(
        kind="PARSER_MISS",
        fingerprint_prefix="parser_miss",
        threshold=2,
        window_days=None,
        desc_key="PARSER_MISS",
    ),
    "UNROUTED_BUYER": GapKind(
        kind="UNROUTED_BUYER",
        fingerprint_prefix="unrouted",
        threshold=3,
        window_days=30,
        desc_key="UNROUTED_BUYER",
    ),
}


def fingerprint(kind: str, *parts: str) -> str:
    """Return the gap fingerprint: the kind's prefix and the parts joined with ``:``.

    Raises:
        ValueError: a part is empty or itself contains ``:``, which would make
            two different gaps share one fingerprint.
    """
    for part in parts:
        if not part:
            raise ValueError(f"empty fingerprint part for {kind}")
        if ":" in part:
            raise ValueError(f"fingerprint part for {kind} contains ':': {part!r}")
    return ":".join((GAP_KINDS[kind].fingerprint_prefix, *parts))
