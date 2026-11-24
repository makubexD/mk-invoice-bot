"""Unit tests for the routing questions and their buttons (BR-R03, BR-R04, BR-R09)."""

from __future__ import annotations

from invoicebot.config.models import Brand, Flow, Store
from invoicebot.extract.fields import REQUIRED, Field, Reading, Source
from invoicebot.pipeline.questions import (
    brand_question,
    buyer_question,
    candidate_flows,
    flow_question,
    summary_buttons,
)
from invoicebot.rules.messages_es import BUTTON_FIX, BUTTON_VOID

OWNER_RUC = "20000000001"
UNKNOWN_BUYER_RUC = "20000000002"
#: One synthetic value per required field (R-2.13), for the summary-button tests.
REQUIRED_VALUES = {
    "ruc_emisor": "51900000000",
    "tipo": "01",
    "serie": "F001",
    "numero": "1234",
    "fecha_emision": "2026-11-24",
    "total": "49.90",
}


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
    text, buttons = flow_question(42, _reading(), _candidates(), 4)

    assert [button.id for button in buttons] == ["42:pick:0:4", "42:pick:1:4", "42:cancel"]
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

    text, buttons = brand_question(9, store, 4)

    assert [button.id for button in buttons] == ["9:brand:0:4", "9:brand:1:4", "9:cancel"]
    assert [button.title for button in buttons] == ["Local Uno", "Local Dos", "Cancelar"]
    assert "Comercio Sintético" in text


def test_question_choice_button_ids_carry_the_menu_and_cancel_does_not() -> None:
    """R-3.5: every choice button id carries the menu, and the cancel id carries none."""
    menu = 7
    questions = (
        flow_question(42, _reading(), _candidates(), menu),
        brand_question(9, _store(), menu),
    )

    for _text, buttons in questions:
        assert buttons[:-1], "a question with choices offers at least one"
        for button in buttons[:-1]:
            assert button.id.endswith(f":{menu}")
        assert buttons[-1].id.endswith(":cancel")
        assert f":{menu}" not in buttons[-1].id


def _reading_with_sources(**sources: Source) -> Reading:
    """Build a reading whose required fields come from ``sources``, the QR by default."""
    fields = {
        name: Field(value=REQUIRED_VALUES[name], source=sources.get(name, "qr"), confidence=1.0)
        for name in REQUIRED
    }
    return Reading(fields=fields, document_url=None)


def test_summary_buttons_are_empty_when_every_required_field_came_from_the_qr() -> None:
    """D189, R-3.4: a summary read fully from the QR carries no buttons."""
    assert summary_buttons(42, _reading_with_sources()) == []


def test_summary_buttons_are_offered_when_one_required_field_came_from_ocr() -> None:
    """D189, R-3.4: one OCR required field makes the summary offer [Corregir] and [Anular]."""
    buttons = summary_buttons(42, _reading_with_sources(total="ocr"))

    assert [button.id for button in buttons] == ["42:edit", "42:cancel"]
    assert [button.title for button in buttons] == [BUTTON_FIX, BUTTON_VOID]


def test_summary_buttons_are_offered_when_one_required_field_came_from_the_llm() -> None:
    """D189, R-3.4: one LLM required field makes the summary offer both buttons."""
    buttons = summary_buttons(42, _reading_with_sources(fecha_emision="llm"))

    assert [button.id for button in buttons] == ["42:edit", "42:cancel"]
    assert [button.title for button in buttons] == [BUTTON_FIX, BUTTON_VOID]


def test_summary_buttons_ignore_a_non_required_field_from_ocr() -> None:
    """D189, R-3.4: an OCR value outside the required fields does not make the summary doubtful."""
    reading = _reading_with_sources()
    fields = {**reading.fields, "igv": Field(value="7.61", source="ocr", confidence=1.0)}

    assert summary_buttons(42, Reading(fields=fields, document_url=None)) == []


def test_summary_buttons_are_empty_when_required_fields_came_from_qr_and_sender() -> None:
    """D189, R-3.4: required fields from the QR and from the sender carry no buttons."""
    assert summary_buttons(42, _reading_with_sources(ruc_emisor="user", numero="user")) == []


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
