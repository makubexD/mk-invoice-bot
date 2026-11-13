"""Routing engine: picks the flow for an invoice from its buyer and the configured rules (R-3.1).

Flows and rules come only from config (D53): this module knows the rule shapes, never a
flow name or a RUC value. ``compile_rules`` runs once at startup; ``route`` is pure and is
called for every invoice with the reading the extract step saved.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from string import Formatter
from typing import Literal

from invoicebot.config.models import Flow, Routing, RoutingRule
from invoicebot.extract.fields import Reading
from invoicebot.rules import MESSAGES

#: No rule matched and the invoice should ask which flow to use (BR-R03).
RULE_ASK = "BR-R03"
#: The matched flow is outside the sender's profile (BR-R05).
RULE_NOT_ALLOWED = "BR-R05"
#: No rule matched and the invoice is rejected (BR-R07).
RULE_UNMATCHED = "BR-R07"
#: The matched flow does not accept the invoice's ``tipo`` (BR-R08).
RULE_WRONG_TIPO = "BR-R08"
#: Rules that cancel the invoice instead of routing it.
REFUSALS = frozenset({RULE_NOT_ALLOWED, RULE_UNMATCHED, RULE_WRONG_TIPO})
#: The rule ids the router itself returns; a configured rule may not reuse one.
ROUTER_RULE_IDS = frozenset({RULE_ASK, RULE_NOT_ALLOWED, RULE_UNMATCHED, RULE_WRONG_TIPO})


@dataclass(frozen=True)
class CompiledRule:
    """One configured routing rule with its ``when`` values interned as frozensets."""

    id: str
    flow: str
    when: Mapping[str, frozenset[str]]


@dataclass(frozen=True)
class RouteResult:
    """The flow a rule matched and the rule id that decided it (R-3.1, R-11.1).

    ``flow`` is ``None`` only when no rule matched. Under BR-R05 and BR-R08 it is the
    flow that was refused, because their messages name it; the caller acts on ``rule``.
    """

    flow: str | None
    rule: str


def compile_rules(routing: Routing) -> tuple[CompiledRule, ...]:
    """Compile the configured rules once at startup, keeping their order (R-3.1).

    Raises:
        ValueError: a rule's id has no message in the catalog, reuses an id the router
            itself returns, or its message needs placeholders other than ``{flow}``.
            The message names the rule's index and id, never a flow or RUC value.
    """
    for index, rule in enumerate(routing.rules):
        _check_rule(index, rule)
    return tuple(
        CompiledRule(
            id=rule.id,
            flow=rule.flow,
            when={key: frozenset(values) for key, values in rule.when.items()},
        )
        for rule in routing.rules
    )


def _check_rule(index: int, rule: RoutingRule) -> None:
    """Raise ``ValueError`` when ``route`` or the summary cannot use ``rule`` (R-3.1)."""
    if rule.id not in MESSAGES:
        raise ValueError(f"routing rule {index} ({rule.id}) has no message in the catalog")
    if rule.id in ROUTER_RULE_IDS:
        raise ValueError(f"routing rule {index} ({rule.id}) reuses an id the router returns")
    try:
        parsed = list(Formatter().parse(MESSAGES[rule.id]))
    except ValueError as exc:
        raise ValueError(f"routing rule {index} ({rule.id}) has a malformed message") from exc
    names = {field.split(".", 1)[0].split("[", 1)[0] for _, field, _, _ in parsed if field is not None}
    if names != {"flow"}:
        raise ValueError(
            f"routing rule {index} ({rule.id}) message uses placeholders {sorted(names)}; "
            "only 'flow' is filled"
        )


def route(
    reading: Reading,
    profile_flows: Collection[str],
    rules: tuple[CompiledRule, ...],
    flows: Mapping[str, Flow],
    unmatched: Literal["ask", "reject"],
) -> RouteResult:
    """Return the flow for ``reading`` and the rule id that decided it (R-3.1).

    The first rule whose every ``when`` key matches wins; a key the reading does not
    carry never matches. Without a match the result is BR-R03 (``unmatched == "ask"``)
    or BR-R07. A flow outside the sender's profile is BR-R05, and a flow that does not
    accept the invoice's ``tipo`` is BR-R08.
    """
    matched = next((rule for rule in rules if _matches(rule, reading)), None)
    if matched is None:
        return RouteResult(flow=None, rule=RULE_ASK if unmatched == "ask" else RULE_UNMATCHED)
    if matched.flow not in profile_flows:
        return RouteResult(flow=matched.flow, rule=RULE_NOT_ALLOWED)
    tipo = reading.fields.get("tipo")
    if tipo is None or tipo.value not in flows[matched.flow].accepted_tipos:
        return RouteResult(flow=matched.flow, rule=RULE_WRONG_TIPO)
    return RouteResult(flow=matched.flow, rule=matched.id)


def _matches(rule: CompiledRule, reading: Reading) -> bool:
    """Return whether every ``when`` key of ``rule`` holds for ``reading``."""
    for key, values in rule.when.items():
        field = reading.fields.get(key)
        if field is None or field.value not in values:
            return False
    return True
