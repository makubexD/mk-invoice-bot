"""Unit tests for the routing questions and their buttons (BR-R03, BR-R04, BR-R09)."""

from __future__ import annotations

from invoicebot.config.models import Brand, Flow, Store
from invoicebot.extract.fields import Field, Reading
from invoicebot.pipeline.questions import (
    brand_question,
    buyer_question,
    candidate_flows,
    flow_question,
)

OWNER_RUC = "20000000001"
UNKNOWN_BUYER_RUC = "20000000002"


def _flow(label: str, accepted_tipos: list[str]) -> Flow:
    return Flow(
        label=label,
        accepted_tipos=accepted_tipos,
        fetchers=["portal_registry"],
        required_docs=["pdf"],
        deliver_without_docs=False,
        sinks=[],
    )


def _reading() -> Reading:
    return Reading(
        fields={
            "tipo_doc_adq": Field(value="1", source="qr", confidence=1.0),
            "num_doc_adq": Field(value=UNKNOWN_BUYER_RUC, source="qr", confidence=1.0),
        },
        document_url=None,
    )


def _candidates() -> list[tuple[str, Flow]]:
    return [("work", _flow("Trabajo", ["01"])), ("personal", _flow("Personal", ["01"]))]


def _store() -> Store:
    return Store(
        ruc=OWNER_RUC,
        razon_social="Comercio Sintético",
        tipo="restaurante",
        brands=[Brand(comercio="Local Uno"), Brand(comercio="Local Dos")],
        portal=None,
    )


def test_flow_question_offers_one_pick_button_per_candidate_then_cancel() -> None:
    """BR-R03: two candidate flows give two pick buttons and a last cancel button."""
    text, buttons = flow_question(42, _reading(), _candidates())

    assert [button.id for button in buttons] == ["42:pick:0", "42:pick:1", "42:cancel"]
    assert [button.title for button in buttons] == ["Trabajo", "Personal", "Cancelar"]
    assert "del DNI 20000000002" in text


def test_buyer_question_offers_yes_change_and_cancel() -> None:
    """BR-R04: a doubtful buyer gets [Sí], [Cambiar] and [Cancelar]."""
    flow = _flow("Trabajo", ["01"])

    text, buttons = buyer_question(7, _reading(), flow)

    assert [button.id for button in buttons] == ["7:yes", "7:change", "7:cancel"]
    assert [button.title for button in buttons] == ["Sí", "Cambiar", "Cancelar"]
    assert "20000000002" in text
    assert "Trabajo" in text


def test_brand_question_offers_one_brand_button_per_comercio_then_cancel() -> None:
    """BR-R09: a two-brand store gets two brand buttons and a last cancel button."""
    store = _store()

    text, buttons = brand_question(9, store)

    assert [button.id for button in buttons] == ["9:brand:0", "9:brand:1", "9:cancel"]
    assert [button.title for button in buttons] == ["Local Uno", "Local Dos", "Cancelar"]
    assert "Comercio Sintético" in text


def test_candidate_flows_keeps_config_order() -> None:
    """BR-R03: the candidates follow the config's flow order, not the profile's."""
    flows = {"work": _flow("Trabajo", ["01"]), "personal": _flow("Personal", ["01"])}

    candidates = candidate_flows(["personal", "work"], flows, "01")

    assert [flow_id for flow_id, _flow_model in candidates] == ["work", "personal"]


def test_candidate_flows_drops_a_flow_that_rejects_the_tipo() -> None:
    """BR-R03: a flow whose accepted_tipos lacks the invoice's tipo is not a candidate."""
    flows = {"work": _flow("Trabajo", ["01"]), "personal": _flow("Personal", ["01", "03"])}

    candidates = candidate_flows(["personal", "work"], flows, "03")

    assert [flow_id for flow_id, _flow_model in candidates] == ["personal"]
