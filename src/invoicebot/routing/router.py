"""Routing engine: picks the flow for an invoice from its buyer and the configured rules (R-3.1).

Flows and rules come only from config (D53): this module knows the rule shapes, never a
flow name or a RUC value. ``compile_rules`` runs once at startup; ``route`` is pure and is
called for every invoice with the reading the extract step saved.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Literal

from invoicebot.config.models import Flow, Routing
from invoicebot.extract.fields import Reading


@dataclass(frozen=True)
class CompiledRule:
    """One configured routing rule with its ``when`` values interned as frozensets."""

    id: str
    flow: str
    when: Mapping[str, frozenset[str]]


@dataclass(frozen=True)
class RouteResult:
    """The flow chosen for an invoice and the rule id that decided it (R-3.1, R-11.1)."""

    flow: str | None
    rule: str


def compile_rules(routing: Routing) -> tuple[CompiledRule, ...]:
    """Compile the configured rules once at startup, keeping their order (R-3.1)."""
    return tuple(
        CompiledRule(
            id=rule.id,
            flow=rule.flow,
            when={key: frozenset(values) for key, values in rule.when.items()},
        )
        for rule in routing.rules
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
        return RouteResult(flow=None, rule="BR-R03" if unmatched == "ask" else "BR-R07")
    if matched.flow not in profile_flows:
        return RouteResult(flow=None, rule="BR-R05")
    tipo = reading.fields.get("tipo")
    if tipo is None or tipo.value not in flows[matched.flow].accepted_tipos:
        return RouteResult(flow=None, rule="BR-R08")
    return RouteResult(flow=matched.flow, rule=matched.id)


def _matches(rule: CompiledRule, reading: Reading) -> bool:
    """Return whether every ``when`` key of ``rule`` holds for ``reading``."""
    for key, values in rule.when.items():
        field = reading.fields.get(key)
        if field is None or field.value not in values:
            return False
    return True
