"""Tests for the outbound WhatsApp payload builders (R-3.2)."""

from __future__ import annotations

import pytest

from invoicebot.channels.base import Button
from invoicebot.channels.whatsapp.payloads import (
    button_id,
    buttons_payload,
    list_payload,
    parse_button_id,
    template_payload,
    text_payload,
)

TO = "51999999999"


def _buttons(count: int, *, title: str = "Opción") -> list[Button]:
    return [Button(id=f"{index}:choose", title=title) for index in range(count)]


def test_text_payload_matches_meta_shape() -> None:
    """R-3.2: a text reply is a text message whose body sits under text.body."""
    assert text_payload(TO, "hola") == {
        "messaging_product": "whatsapp",
        "to": TO,
        "type": "text",
        "text": {"body": "hola"},
    }


def test_buttons_payload_builds_two_reply_buttons() -> None:
    """R-3.2: the unmatched-buyer menu is one interactive message with reply buttons."""
    buttons = [
        Button(id="1:personal_google", title="Mi empresa"),
        Button(id="2:work_microsoft", title="Trabajo"),
    ]
    assert buttons_payload(TO, "¿A qué flujo?", buttons) == {
        "messaging_product": "whatsapp",
        "to": TO,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "¿A qué flujo?"},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {"id": "1:personal_google", "title": "Mi empresa"},
                    },
                    {
                        "type": "reply",
                        "reply": {"id": "2:work_microsoft", "title": "Trabajo"},
                    },
                ]
            },
        },
    }


@pytest.mark.parametrize(
    ("buttons", "match"),
    [
        (_buttons(4), "1-3"),
        ([Button(id="1:choose", title="x" * 21)], "1-20"),
        (
            [
                Button(id="1:choose", title="Uno"),
                Button(id="1:choose", title="Dos"),
            ],
            "unique",
        ),
        ([], "1-3"),
    ],
    ids=["four-buttons", "title-too-long", "duplicate-ids", "no-buttons"],
)
def test_buttons_payload_rejects_broken_limits(buttons: list[Button], match: str) -> None:
    """R-3.2: a button menu outside Meta's limits is rejected before sending."""
    with pytest.raises(ValueError, match=match):
        buttons_payload(TO, "¿A qué flujo?", buttons)


def test_list_payload_builds_ten_rows() -> None:
    """R-3.2: a flow menu is one interactive list with a section of up to ten rows."""
    rows = _buttons(10)
    payload = list_payload(TO, "Elegí el flujo", "Ver opciones", rows)
    assert payload["messaging_product"] == "whatsapp"
    assert payload["to"] == TO
    assert payload["type"] == "interactive"
    assert payload["interactive"] == {
        "type": "list",
        "body": {"text": "Elegí el flujo"},
        "action": {
            "button": "Ver opciones",
            "sections": [{"rows": [{"id": row.id, "title": "Opción"} for row in rows]}],
        },
    }


@pytest.mark.parametrize(
    ("rows", "match"),
    [
        (_buttons(11), "1-10"),
        (_buttons(1, title="x" * 25), "1-24"),
    ],
    ids=["eleven-rows", "row-title-too-long"],
)
def test_list_payload_rejects_broken_limits(rows: list[Button], match: str) -> None:
    """R-3.2: a list menu outside Meta's limits is rejected before sending."""
    with pytest.raises(ValueError, match=match):
        list_payload(TO, "Elegí el flujo", "Ver opciones", rows)


def test_template_payload_builds_body_component() -> None:
    """R-3.2: a completion template carries its body parameters as text parameters."""
    assert template_payload(TO, "invoice_done", "es_PE", ["hola"]) == {
        "messaging_product": "whatsapp",
        "to": TO,
        "type": "template",
        "template": {
            "name": "invoice_done",
            "language": {"code": "es_PE"},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": "hola"}]}],
        },
    }


def test_template_payload_without_params_omits_components() -> None:
    """R-3.2: a template with no body parameters has no components key."""
    payload = template_payload(TO, "invoice_done", "es_PE", [])
    assert payload == {
        "messaging_product": "whatsapp",
        "to": TO,
        "type": "template",
        "template": {"name": "invoice_done", "language": {"code": "es_PE"}},
    }
    assert "components" not in payload["template"]


def test_button_id_round_trips_through_parse() -> None:
    """R-3.2: a reply id encodes the invoice and the action and parses back."""
    assert button_id(12, "confirm") == "12:confirm"
    assert parse_button_id("12:confirm") == (12, "confirm")


def test_parse_button_id_bounds_the_invoice_to_nine_digits() -> None:
    """R-3.2: a ten-digit invoice id parses to None; nine digits still parse."""
    assert parse_button_id("9999999999:confirm") is None
    assert parse_button_id("123456789:confirm") == (123456789, "confirm")


@pytest.mark.parametrize("value", ["x:confirm", "12", "12:Bad-Action", ""])
def test_parse_button_id_returns_none_for_malformed_values(value: str) -> None:
    """R-3.2: a malformed reply id parses to None instead of raising."""
    assert parse_button_id(value) is None


def test_button_id_rejects_bad_action() -> None:
    """R-3.2: an action that is not lowercase words is rejected."""
    with pytest.raises(ValueError, match=r"\[a-z_\]\{1,32\}"):
        button_id(1, "Bad")
