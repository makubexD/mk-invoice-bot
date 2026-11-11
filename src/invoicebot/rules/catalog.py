"""Business-rule catalog (spec 06): each BR-* rule's state and message key.

R-11.1: every stop, warning or reroute references a rule ID from
``specs/06-business-rules.md`` and sends that rule's catalogued message. The
parity test in ``tests/unit/test_rules_parity.py`` keeps this table and
``messages_es.py`` in sync with the spec.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    """One business rule.

    ``state`` is the invoice state the rule leads to (the spec's Result
    column), or ``None`` when it does not change the state. ``message_key``
    indexes ``MESSAGES`` and is ``None`` only for rules whose spec row carries
    no chat message.
    """

    id: str
    state: str | None
    message_key: str | None


RULES: dict[str, Rule] = {
    "BR-R01": Rule("BR-R01", None, "BR-R01"),
    "BR-R02": Rule("BR-R02", None, "BR-R02"),
    "BR-R03": Rule("BR-R03", "CONFIRMING", "BR-R03"),
    "BR-R04": Rule("BR-R04", "CONFIRMING", "BR-R04"),
    "BR-R05": Rule("BR-R05", "CANCELLED", "BR-R05"),
    "BR-R06": Rule("BR-R06", "CONFIRMING", None),
    "BR-R07": Rule("BR-R07", "CANCELLED", "BR-R07"),
    "BR-E01": Rule("BR-E01", "EXTRACT_FAILED", "BR-E01"),
    "BR-E02": Rule("BR-E02", "EXTRACT_FAILED", "BR-E02"),
    "BR-E03": Rule("BR-E03", "CONFIRMING", "BR-E03"),
    "BR-E04": Rule("BR-E04", None, "BR-E04"),
    "BR-E05": Rule("BR-E05", "CANCELLED", "BR-E05"),
    "BR-E06": Rule("BR-E06", None, None),
    "BR-E07": Rule("BR-E07", "CANCELLED", "BR-E07"),
    "BR-E08": Rule("BR-E08", "EXTRACT_FAILED", "BR-E08"),
    "BR-V01": Rule("BR-V01", None, "BR-V01"),
    "BR-V02": Rule("BR-V02", "CONFIRM_INVALID", "BR-V02"),
    "BR-V03": Rule("BR-V03", None, "BR-V03"),
    "BR-V04": Rule("BR-V04", None, "BR-V04"),
    "BR-V05": Rule("BR-V05", None, "BR-V05"),
    "BR-V06": Rule("BR-V06", "CONFIRM_INVALID", "BR-V06"),
    "BR-F01": Rule("BR-F01", None, "BR-F01"),
    "BR-F02": Rule("BR-F02", None, "BR-F02"),
    "BR-F03": Rule("BR-F03", None, "BR-F03"),
    "BR-F04": Rule("BR-F04", None, "BR-F04"),
    "BR-F05": Rule("BR-F05", None, "BR-F05"),
    "BR-F06": Rule("BR-F06", None, "BR-F06"),
    "BR-F08": Rule("BR-F08", None, "BR-F08"),
    "BR-F09": Rule("BR-F09", None, "BR-F09"),
    "BR-F10": Rule("BR-F10", None, "BR-F10"),
    "BR-F07": Rule("BR-F07", "MANUAL_REQUIRED", "BR-F07"),
    "BR-D01": Rule("BR-D01", "PENDING_AUTH", "BR-D01"),
    "BR-D02": Rule("BR-D02", "MANUAL_REQUIRED", "BR-D02"),
    "BR-D03": Rule("BR-D03", None, "BR-D03"),
    "BR-D04": Rule("BR-D04", None, "BR-D04"),
    "BR-D05": Rule("BR-D05", None, "BR-D05"),
    "BR-S01": Rule("BR-S01", None, "BR-S01"),
    "BR-S02": Rule("BR-S02", None, "BR-S02"),
    "BR-S03": Rule("BR-S03", "FAILED", "BR-S03"),
    "BR-S04": Rule("BR-S04", None, "BR-S04"),
    "BR-S05": Rule("BR-S05", None, "BR-S05"),
    "BR-S06": Rule("BR-S06", None, "BR-S06"),
    "BR-S07": Rule("BR-S07", None, "BR-S07"),
    "BR-S08": Rule("BR-S08", "PENDING_USER", "BR-S08"),
    "BR-O01": Rule("BR-O01", None, "BR-O01"),
    "BR-O02": Rule("BR-O02", None, "BR-O02"),
    "BR-O03": Rule("BR-O03", None, "BR-O03"),
    "BR-O04": Rule("BR-O04", None, "BR-O04"),
}
