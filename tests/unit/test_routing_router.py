"""Tests for the routing engine (R-3.1, BR-R01..BR-R08)."""

from __future__ import annotations

from typing import Literal

import pytest

from invoicebot.config.models import Flow, Routing, RoutingRule
from invoicebot.extract.fields import Field, Reading
from invoicebot.routing.router import CompiledRule, RouteResult, compile_rules, route

# Both values come from tests/synthetic_rucs.txt; no real RUC appears in this file.
OWNER_RUC = "20000000001"
WORK_RUC = "51900000000"
DNI = "12345678"

FLOW_A = "flow_a"
FLOW_B = "flow_b"
FLOW_C = "flow_c"


def _flow(accepted_tipos: list[str]) -> Flow:
    return Flow(
        label="Etiqueta",
        accepted_tipos=accepted_tipos,
        fetchers=[],
        required_docs=[],
        deliver_without_docs=False,
        sinks=[],
    )


def _flows() -> dict[str, Flow]:
    """Return three flows, so a third one can be added from config alone (D53)."""
    return {
        FLOW_A: _flow(["01", "03"]),
        FLOW_B: _flow(["01"]),
        FLOW_C: _flow(["01", "03"]),
    }


def _routing() -> Routing:
    """Return a routing config: owner -> A, work -> B, and a fake issuer rule -> C."""
    return Routing(
        confirm_mode="auto",
        unmatched="ask",
        rules=[
            RoutingRule(id="BR-R01", when={"num_doc_adq": [OWNER_RUC]}, flow=FLOW_A),
            RoutingRule(id="BR-R02", when={"num_doc_adq": [WORK_RUC]}, flow=FLOW_B),
            RoutingRule(id="BR-R10", when={"ruc_emisor": [OWNER_RUC]}, flow=FLOW_C),
        ],
    )


def _reading(
    *,
    num_doc_adq: str | None = None,
    ruc_emisor: str = WORK_RUC,
    tipo: str = "01",
) -> Reading:
    """Build a reading; ``num_doc_adq`` is left out when the buyer is missing."""
    fields = {
        "tipo": Field(value=tipo, source="qr", confidence=1.0),
        "ruc_emisor": Field(value=ruc_emisor, source="qr", confidence=1.0),
    }
    if num_doc_adq is not None:
        fields["num_doc_adq"] = Field(value=num_doc_adq, source="qr", confidence=1.0)
    return Reading(fields=fields, document_url=None)


def _route(
    reading: Reading,
    *,
    profile_flows: list[str],
    routing: Routing | None = None,
    unmatched: Literal["ask", "reject"] = "ask",
) -> RouteResult:
    return route(
        reading,
        profile_flows,
        compile_rules(routing or _routing()),
        _flows(),
        unmatched,
    )


def test_compile_rules_interns_when_values_as_frozensets() -> None:
    """R-3.1: compiling keeps rule order and turns every when value into a frozenset."""
    rules = compile_rules(_routing())

    assert rules == (
        CompiledRule(id="BR-R01", flow=FLOW_A, when={"num_doc_adq": frozenset({OWNER_RUC})}),
        CompiledRule(id="BR-R02", flow=FLOW_B, when={"num_doc_adq": frozenset({WORK_RUC})}),
        CompiledRule(id="BR-R10", flow=FLOW_C, when={"ruc_emisor": frozenset({OWNER_RUC})}),
    )
    assert isinstance(rules[0].when["num_doc_adq"], frozenset)


@pytest.mark.parametrize(
    ("buyer", "expected_flow", "expected_rule"),
    [
        (OWNER_RUC, FLOW_A, "BR-R01"),
        (WORK_RUC, FLOW_B, "BR-R02"),
    ],
)
def test_route_buyer_rule_picks_its_flow(buyer: str, expected_flow: str, expected_rule: str) -> None:
    """BR-R01, BR-R02: the buyer RUC rule routes the invoice to the configured flow."""
    result = _route(_reading(num_doc_adq=buyer), profile_flows=[FLOW_A, FLOW_B])

    assert result == RouteResult(flow=expected_flow, rule=expected_rule)


def test_route_issuer_rule_picks_the_fake_third_flow() -> None:
    """R-3.1, D53: a rule on ruc_emisor routes to a third flow with no engine change."""
    reading = _reading(num_doc_adq=DNI, ruc_emisor=OWNER_RUC)

    result = _route(reading, profile_flows=[FLOW_A, FLOW_B, FLOW_C])

    assert result == RouteResult(flow=FLOW_C, rule="BR-R10")


def test_route_first_matching_rule_wins() -> None:
    """R-3.1: when two rules match, the earlier configured rule decides."""
    reading = _reading(num_doc_adq=OWNER_RUC, ruc_emisor=OWNER_RUC)

    result = _route(reading, profile_flows=[FLOW_A, FLOW_B, FLOW_C])

    assert result == RouteResult(flow=FLOW_A, rule="BR-R01")


def test_route_when_key_absent_from_reading_does_not_match() -> None:
    """R-3.1: a when key the reading does not carry cannot match, even when other keys do."""
    routing = Routing(
        confirm_mode="auto",
        unmatched="ask",
        rules=[
            RoutingRule(
                id="BR-R01",
                when={"num_doc_adq": [OWNER_RUC], "tipo_doc_adq": ["6"]},
                flow=FLOW_A,
            )
        ],
    )

    result = _route(_reading(num_doc_adq=OWNER_RUC), profile_flows=[FLOW_A], routing=routing)

    assert result == RouteResult(flow=None, rule="BR-R03")


@pytest.mark.parametrize(
    ("buyer", "unmatched", "expected_rule"),
    [
        (DNI, "ask", "BR-R03"),
        (DNI, "reject", "BR-R07"),
        (None, "ask", "BR-R03"),
        (None, "reject", "BR-R07"),
    ],
)
def test_route_unmatched_buyer_returns_the_configured_rule(
    buyer: str | None, unmatched: Literal["ask", "reject"], expected_rule: str
) -> None:
    """BR-R03, BR-R07: a DNI or missing buyer follows routing.unmatched."""
    reading = _reading(num_doc_adq=buyer, ruc_emisor=WORK_RUC)

    result = _route(reading, profile_flows=[FLOW_A, FLOW_B], unmatched=unmatched)

    assert result == RouteResult(flow=None, rule=expected_rule)


def test_route_flow_outside_profile_is_rejected() -> None:
    """BR-R05: a flow the sender's profile does not allow is refused, and named for the message."""
    result = _route(_reading(num_doc_adq=WORK_RUC), profile_flows=[FLOW_A])

    assert result == RouteResult(flow=FLOW_B, rule="BR-R05")


def test_route_tipo_outside_accepted_tipos_is_rejected() -> None:
    """BR-R08: a boleta routed to a flow that accepts only facturas is rejected."""
    reading = _reading(num_doc_adq=WORK_RUC, tipo="03")

    result = _route(reading, profile_flows=[FLOW_B])

    assert result == RouteResult(flow=FLOW_B, rule="BR-R08")
