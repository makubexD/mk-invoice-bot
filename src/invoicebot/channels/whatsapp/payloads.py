"""Build Meta WhatsApp Cloud API outbound payloads (R-3.2).

Pure functions only: each builder returns the JSON body for ``/messages`` and
rejects a value outside Meta's documented limits before the caller sends it.
Reply ids encode the invoice and the action as ``"<invoice_id>:<action>"``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from invoicebot.channels.base import Button

PRODUCT = "whatsapp"
TEXT_MAX_CHARS = 4096
INTERACTIVE_BODY_MAX_CHARS = 1024
BUTTONS_MAX_COUNT = 3
BUTTONS_LABEL_MAX_CHARS = 20
BUTTON_ID_MAX_CHARS = 256
LIST_ROWS_MAX_COUNT = 10
LIST_ROW_TITLE_MAX_CHARS = 24
ACTION_MAX_CHARS = 32

_MIN_LENGTH = 1
_ACTION_PATTERN = rf"[a-z_]{{1,{ACTION_MAX_CHARS}}}"
_BUTTON_ID_PATTERN = rf"([0-9]+):({_ACTION_PATTERN})"


def _check_chars(value: str, *, maximum: int, field: str) -> None:
    """Raise ValueError when a text field is empty or above its character limit."""
    if not _MIN_LENGTH <= len(value) <= maximum:
        raise ValueError(f"{field} must be 1-{maximum} characters, got {len(value)}")


def _check_count(count: int, *, maximum: int, field: str) -> None:
    """Raise ValueError when a list is empty or above its item limit."""
    if not _MIN_LENGTH <= count <= maximum:
        raise ValueError(f"{field} count must be 1-{maximum}, got {count}")


def _check_unique_ids(buttons: Sequence[Button], *, field: str) -> None:
    """Raise ValueError when two buttons or rows share an id, which Meta rejects."""
    if len({button.id for button in buttons}) != len(buttons):
        raise ValueError(f"{field} ids must be unique")


def button_id(invoice_id: int, action: str) -> str:
    """Encode a reply button id, rejecting an action outside ``[a-z_]{1,32}``."""
    if re.fullmatch(_ACTION_PATTERN, action) is None:
        raise ValueError(f"action must match [a-z_]{{1,{ACTION_MAX_CHARS}}}, got {action!r}")
    return f"{invoice_id}:{action}"


def parse_button_id(value: str) -> tuple[int, str] | None:
    """Parse a reply button id back to ``(invoice_id, action)``, or None if malformed."""
    match = re.fullmatch(_BUTTON_ID_PATTERN, value)
    if match is None:
        return None
    return int(match.group(1)), match.group(2)


def text_payload(to: str, body: str) -> dict[str, Any]:
    """Build a plain text message with a body of 1-4096 characters."""
    _check_chars(body, maximum=TEXT_MAX_CHARS, field="body")
    return {
        "messaging_product": PRODUCT,
        "to": to,
        "type": "text",
        "text": {"body": body},
    }


def buttons_payload(to: str, body: str, buttons: Sequence[Button]) -> dict[str, Any]:
    """Build an interactive message with 1-3 unique reply buttons."""
    _check_chars(body, maximum=INTERACTIVE_BODY_MAX_CHARS, field="body")
    _check_count(len(buttons), maximum=BUTTONS_MAX_COUNT, field="buttons")
    for button in buttons:
        _check_chars(button.id, maximum=BUTTON_ID_MAX_CHARS, field="button id")
        _check_chars(button.title, maximum=BUTTONS_LABEL_MAX_CHARS, field="button title")
    _check_unique_ids(buttons, field="button")
    return {
        "messaging_product": PRODUCT,
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": button.id, "title": button.title}} for button in buttons
                ]
            },
        },
    }


def list_payload(to: str, body: str, button_label: str, rows: Sequence[Button]) -> dict[str, Any]:
    """Build an interactive list message with one section of 1-10 rows."""
    _check_chars(body, maximum=INTERACTIVE_BODY_MAX_CHARS, field="list body")
    _check_chars(button_label, maximum=BUTTONS_LABEL_MAX_CHARS, field="list button label")
    _check_count(len(rows), maximum=LIST_ROWS_MAX_COUNT, field="list rows")
    for row in rows:
        _check_chars(row.id, maximum=BUTTON_ID_MAX_CHARS, field="row id")
        _check_chars(row.title, maximum=LIST_ROW_TITLE_MAX_CHARS, field="row title")
    _check_unique_ids(rows, field="row")
    return {
        "messaging_product": PRODUCT,
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body},
            "action": {
                "button": button_label,
                "sections": [{"rows": [{"id": row.id, "title": row.title} for row in rows]}],
            },
        },
    }


def template_payload(to: str, name: str, language: str, body_params: Sequence[str]) -> dict[str, Any]:
    """Build a template message, omitting the components key when it has no body params."""
    template: dict[str, Any] = {"name": name, "language": {"code": language}}
    if body_params:
        template["components"] = [
            {
                "type": "body",
                "parameters": [{"type": "text", "text": param} for param in body_params],
            }
        ]
    return {
        "messaging_product": PRODUCT,
        "to": to,
        "type": "template",
        "template": template,
    }
