"""Parity test between the business-rule catalog and ``specs/06-business-rules.md``.

R-11.1: every stop, warning or reroute references a rule ID from spec 06 and
uses that rule's message from the catalog. This test parses the BR-* rows of
the spec at test time and fails, listing the offenders, when the catalog, the
messages or the states drift from it.

Message-column normalization used on both sides of the comparison:
- the text lives inside backticks; the backticks are Markdown table markup and
  are not part of the message;
- an italic annotation such as ``*(one line in the summary)*`` is a note about
  where the text is rendered and is dropped;
- button labels in ``[...]`` are UI controls, not message text, and are dropped;
- ``User:``, ``Admin:``, ``Owner:`` and ``Sender:`` split one cell into several
  messages. The sender's message is the rule's primary message (key = rule ID);
  every other message gets a ``_<role>`` suffix (``_admin``, ``_owner``,
  ``_sender``);
- code spans joined by `` + `` are one message broken across the table line;
  the parts are joined with a newline, the line break the spec is showing;
- the one insertion the spec describes in prose, ``one `•` line per fetcher``
  (BR-F07), becomes the ``{fetcher_lines}`` placeholder. BR-F07 is the only row
  whose text is not copied verbatim from a code span.

State normalization: the Result column's backticked invoice-state names (the
states of spec 03 §4) are read from the spec. The last one wins, so an arrow
transition yields its target (BR-D02); a name preceded by "stay" is not a
transition (BR-D03); and "Same as E01" / "continues as BR-R03" delegate to the
referenced rule (BR-E02, BR-R06). No state name means ``None``.
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from invoicebot.rules.catalog import RULES
from invoicebot.rules.messages_es import GAP_DIGEST_LINE, KIND_DESC, KIND_SUBJECT, MESSAGES

SPEC_PATH = Path(__file__).resolve().parents[2] / "specs" / "06-business-rules.md"

INVOICE_STATES = frozenset(
    {
        "RECEIVED",
        "EXTRACTING",
        "ROUTING",
        "VALIDATING",
        "FETCHING_DOCS",
        "DELIVERING",
        "DONE",
        "BATCHED",
        "WAITING_CAPTCHA",
        "EXTRACT_FAILED",
        "CONFIRMING",
        "CANCELLED",
        "CONFIRM_INVALID",
        "PENDING_USER",
        "MANUAL_REQUIRED",
        "PENDING_AUTH",
        "FAILED",
    }
)

_ROW_RE = re.compile(r"^\|\s*(BR-[A-Z]\d{2})\s*\|")
_STATE_REF_RE = re.compile(r"\b(?:Same as|continues as)\s+(BR-)?([A-Z]\d{2})\b")
_CODE_SPAN_RE = re.compile(r"`([^`]*)`")
_ANNOTATION_RE = re.compile(r"\*\([^)]*\)\*")
# A button is a [..] outside a code span; a text may name a button inside its span (BR-F02, D190).
_BUTTON_RE = re.compile(r"(`[^`]*`)|\[[^\]]*\]")
_ROLE_RE = re.compile(r"\b(User|Admin|Owner|Sender):")

_ROLE_PRIORITY = {None: 0, "user": 0, "sender": 0, "owner": 1, "admin": 2}
_ROLE_SUFFIXES = {"admin": "_admin", "owner": "_owner", "sender": "_sender", "user": "_user"}

_GAPS_SECTION_START = "## 3. Capability gaps"
_GAPS_SECTION_END = "### Gap lifecycle"
_GAP_KIND_ROW_RE = re.compile(r"^\|\s*`([A-Z_]+)`\s*\|")
_GAP_ALERT_RE = re.compile(r"`(GAP_[A-Za-z_]+)`\s+`([^`]*)`")
_GAP_DIGEST_LINE_RE = re.compile(r"`(• \{kind_desc\} — \{subject\})`")


@dataclass(frozen=True)
class SpecRule:
    """A BR-* row of spec 06, reduced to what the catalog mirrors."""

    id: str
    state: str | None
    messages: dict[str, str]


def _direct_state(result_cell: str) -> str | None:
    states = [
        token
        for token in _CODE_SPAN_RE.findall(result_cell)
        if token in INVOICE_STATES and not re.search(rf"\bstays?\s+`{re.escape(token)}`", result_cell)
    ]
    return states[-1] if states else None


def _state_for(
    rule_id: str, rows: dict[str, tuple[str, str]], seen: frozenset[str] = frozenset()
) -> str | None:
    result_cell, _ = rows[rule_id]
    state = _direct_state(result_cell)
    if state is not None:
        return state
    reference = _STATE_REF_RE.search(result_cell)
    if reference is not None:
        target = (reference.group(1) or "BR-") + reference.group(2)
        if target in rows and target not in seen:
            return _state_for(target, rows, seen | {rule_id})
    return None


def _messages_for(rule_id: str, message_cell: str) -> dict[str, str]:
    cell = _ANNOTATION_RE.sub("", message_cell)
    cell = _BUTTON_RE.sub(lambda match: match.group(1) or "", cell)

    parts = _ROLE_RE.split(cell)
    segments: list[tuple[str | None, str]] = [(None, parts[0])]
    for index in range(1, len(parts), 2):
        segments.append((parts[index].lower(), parts[index + 1]))

    found: list[tuple[str | None, str]] = []
    for role, segment in segments:
        spans = _CODE_SPAN_RE.findall(segment)
        if not spans:
            continue
        if len(spans) == 1:
            text = spans[0]
        elif "line per fetcher" in segment:
            text = spans[0] + "\n{fetcher_lines}\n" + spans[-1]
        else:
            text = "\n".join(spans)
        found.append((role, text))

    if not found:
        return {}

    primary_index = min(range(len(found)), key=lambda index: _ROLE_PRIORITY[found[index][0]])
    _, primary_text = found.pop(primary_index)
    messages = {rule_id: primary_text}
    for role, text in found:
        # The primary is the sender's message; every other role is a suffix.
        messages[rule_id + _ROLE_SUFFIXES[role or "user"]] = text
    return messages


@lru_cache(maxsize=1)
def _parse_spec() -> dict[str, SpecRule]:
    text = SPEC_PATH.read_text(encoding="utf-8")
    rows: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        match = _ROW_RE.match(line)
        if match is None:
            continue
        cells = line.strip().strip("|").split("|")
        if len(cells) != 4:
            raise AssertionError(f"unparsed table row in {SPEC_PATH.name}: {line!r}")
        rows[match.group(1)] = (cells[2].strip(), cells[3].strip())
    return {
        rule_id: SpecRule(rule_id, _state_for(rule_id, rows), _messages_for(rule_id, message_cell))
        for rule_id, (_, message_cell) in rows.items()
    }


def _failure(title: str, offenders: list[str]) -> str:
    return f"{title} (R-11.1):\n" + "\n".join(f"  - {offender}" for offender in offenders)


def test_every_spec_rule_has_a_catalog_entry() -> None:
    """R-11.1: every BR-* row in spec 06 is present in RULES."""
    spec = _parse_spec()
    offenders = sorted(rule_id for rule_id in spec if rule_id not in RULES)
    assert not offenders, _failure("rules present in spec 06 but missing from RULES", offenders)


def test_every_catalog_rule_has_a_spec_row() -> None:
    """R-11.1: every RULES entry has a matching BR-* row in spec 06."""
    spec = _parse_spec()
    offenders = sorted(rule_id for rule_id in RULES if rule_id not in spec)
    assert not offenders, _failure("rules present in RULES but missing from spec 06", offenders)


def test_message_keys_follow_the_spec() -> None:
    """R-11.1: each rule points at its catalogued message, or at none when the row has none."""
    spec = _parse_spec()
    offenders = []
    for rule_id in sorted(spec):
        rule = RULES.get(rule_id)
        if rule is None:
            continue
        expected_key = rule_id if spec[rule_id].messages else None
        if rule.message_key != expected_key:
            offenders.append(
                f"{rule_id}: message_key is {rule.message_key!r}, spec row implies {expected_key!r}"
            )
        if rule.message_key is not None and rule.message_key not in MESSAGES:
            offenders.append(f"{rule_id}: message_key {rule.message_key!r} is missing from MESSAGES")
    assert not offenders, _failure("message_key parity", offenders)


def test_states_match_the_spec() -> None:
    """R-11.1: each rule's state matches the state its spec row describes."""
    spec = _parse_spec()
    offenders = []
    for rule_id in sorted(spec):
        rule = RULES.get(rule_id)
        if rule is None:
            continue
        if rule.state != spec[rule_id].state:
            offenders.append(f"{rule_id}: state is {rule.state!r}, spec row implies {spec[rule_id].state!r}")
    assert not offenders, _failure("state parity", offenders)


def test_messages_match_the_spec() -> None:
    """R-11.1: every message text equals the spec row's text after normalization."""
    spec = _parse_spec()
    offenders = []
    for rule_id in sorted(spec):
        for key, expected_text in spec[rule_id].messages.items():
            if key not in MESSAGES:
                offenders.append(f"{key}: message missing from MESSAGES")
            elif MESSAGES[key] != expected_text:
                offenders.append(
                    f"{key}: text differs from spec 06\n"
                    f"      catalog: {MESSAGES[key]!r}\n"
                    f"      spec:    {expected_text!r}"
                )
    assert not offenders, _failure("message text parity", offenders)


@lru_cache(maxsize=1)
def _parse_gap_section() -> tuple[dict[str, tuple[str, str]], dict[str, str], str]:
    """Return spec 06 §3's ``kind_desc``/subject pairs, owner alert texts and digest line."""
    text = SPEC_PATH.read_text(encoding="utf-8")
    section = text.split(_GAPS_SECTION_START, 1)[1].split(_GAPS_SECTION_END, 1)[0]

    pairs: dict[str, tuple[str, str]] = {}
    for line in section.splitlines():
        match = _GAP_KIND_ROW_RE.match(line)
        if match is None:
            continue
        cells = line.strip().strip("|").split("|")
        if len(cells) != 8:
            raise AssertionError(f"unparsed gap-table row in {SPEC_PATH.name}: {line!r}")
        spans = _CODE_SPAN_RE.findall(cells[6])
        if len(spans) != 2:
            raise AssertionError(f"gap row without a kind_desc/subject pair: {line!r}")
        pairs[match.group(1)] = (spans[0], spans[1])

    alerts = dict(_GAP_ALERT_RE.findall(section))
    digest_line = _GAP_DIGEST_LINE_RE.search(section)
    if digest_line is None:
        raise AssertionError(f"no digest line in the owner alert texts of {SPEC_PATH.name}")
    return pairs, alerts, digest_line.group(1)


def test_gap_kind_desc_and_subject_match_the_spec() -> None:
    """R-11.1: each gap kind's kind_desc and subject format equal spec 06 §3."""
    pairs, _, _ = _parse_gap_section()
    offenders = []
    for kind in sorted(pairs):
        desc, subject = pairs[kind]
        if KIND_DESC.get(kind) != desc:
            offenders.append(
                f"{kind}: kind_desc differs from spec 06\n"
                f"      catalog: {KIND_DESC.get(kind)!r}\n"
                f"      spec:    {desc!r}"
            )
        if KIND_SUBJECT.get(kind) != subject:
            offenders.append(
                f"{kind}: subject differs from spec 06\n"
                f"      catalog: {KIND_SUBJECT.get(kind)!r}\n"
                f"      spec:    {subject!r}"
            )
    assert not offenders, _failure("gap kind_desc/subject parity", offenders)


def test_gap_alert_texts_match_the_spec() -> None:
    """R-11.1: the two owner alert texts equal spec 06 §3."""
    _, alerts, spec_digest_line = _parse_gap_section()
    expected = {
        "GAP_admin": alerts.get("GAP_admin"),
        "GAP_DIGEST_admin": (
            None if alerts.get("GAP_DIGEST_admin") is None else alerts["GAP_DIGEST_admin"] + "\n{gap_lines}"
        ),
    }
    offenders = []
    for key, expected_text in expected.items():
        if expected_text is None:
            offenders.append(f"{key}: alert text missing from spec 06 §3")
        elif MESSAGES.get(key) != expected_text:
            offenders.append(
                f"{key}: text differs from spec 06\n"
                f"      catalog: {MESSAGES.get(key)!r}\n"
                f"      spec:    {expected_text!r}"
            )
    if spec_digest_line != GAP_DIGEST_LINE:
        offenders.append(
            "GAP_DIGEST_LINE: line differs from spec 06\n"
            f"      catalog: {GAP_DIGEST_LINE!r}\n"
            f"      spec:    {spec_digest_line!r}"
        )
    assert not offenders, _failure("gap alert text parity", offenders)
