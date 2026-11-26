"""Tests for the outbound WhatsApp payload builders (R-3.2)."""

from __future__ import annotations

import pytest

from invoicebot.channels.base import Button
from invoicebot.channels.whatsapp.payloads import (
    BUTTONS_LABEL_MAX_CHARS,
    LIST_ROW_TITLE_MAX_CHARS,
    buttons_payload,
    document_payload,
    list_payload,
    template_payload,
    text_payload,
)
from invoicebot.config.checks import BUTTON_TITLE_MAX

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


def test_document_payload_matches_meta_shape() -> None:
    """D194: a document message references the uploaded media id with its filename and caption."""
    assert document_payload(TO, "m1", "factura.pdf", "Tu factura") == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": TO,
        "type": "document",
        "document": {"id": "m1", "filename": "factura.pdf", "caption": "Tu factura"},
    }


def test_document_payload_rejects_a_caption_over_the_limit() -> None:
    """D194: a caption above 1024 characters is rejected before sending."""
    with pytest.raises(ValueError, match="1-1024"):
        document_payload(TO, "m1", "factura.pdf", "x" * 1025)


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


def test_configured_button_title_fits_whatsapp_limits() -> None:
    """R-1.7: a configured button title must fit the WhatsApp button and list-row caps."""
    assert BUTTON_TITLE_MAX <= BUTTONS_LABEL_MAX_CHARS
    assert BUTTON_TITLE_MAX <= LIST_ROW_TITLE_MAX_CHARS
