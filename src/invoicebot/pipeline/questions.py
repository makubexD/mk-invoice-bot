"""The questions the pipeline asks and their buttons (BR-R03, BR-R04, BR-R09, BR-D06, D125).

Each builder is pure: it returns the es-PE text from the catalog and the buttons that
carry the answer back, with ids ``"<invoice_id>:<action>[:<choice>:<menu>]"`` (R-3.5).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection

from invoicebot.channels.base import Button, ButtonPress, InboundMessage
from invoicebot.channels.buttons import button_id
from invoicebot.config.models import Flow, Store
from invoicebot.extract.fields import REQUIRED, Reading
from invoicebot.routing.router import RULE_ASK
from invoicebot.rules.messages_es import (
    BUTTON_CANCEL,
    BUTTON_CHANGE,
    BUTTON_CONFIRM,
    BUTTON_CONTINUE,
    BUTTON_DONT_KNOW,
    BUTTON_EDIT,
    BUTTON_FIX,
    BUTTON_SEND_PDF,
    BUTTON_TEACH_PORTAL,
    BUTTON_VOID,
    BUTTON_WAIT_MAIL,
    BUTTON_YES,
    MESSAGES,
    buyer_desc,
)

#: The button actions routing asks with (R-3.5); the actions that answer them import these.
PICK_ACTION = "pick"
YES_ACTION = "yes"
CHANGE_ACTION = "change"
BRAND_ACTION = "brand"
CONTINUE_ACTION = "continue"
CANCEL_ACTION = "cancel"
EDIT_ACTION = "edit"
#: The [Ver #{id}] button's action: it answers like ``/estado {id}`` (D188).
STATUS_ACTION = "status"
#: The [Sí] that accepts a BR-V02/BR-V06 comprobante (D125).
INVALID_OK_ACTION = "invalid_ok"
#: D126's [Confirmar]: the sender accepts the routed summary before validation.
CONFIRM_ACTION = "confirm"
#: BR-F13's three actions: send the PDF, teach the portal, or keep waiting (D189).
SEND_PDF_ACTION = "send_pdf"
TEACH_PORTAL_ACTION = "teach_portal"
WAIT_MAIL_ACTION = "wait_mail"
#: BR-F13: the question asking the sender for the documents no fetcher could get.
RULE_DOCS_NEEDED = "BR-F13"
#: The rule ids of the questions only routing asks.
RULE_BUYER = "BR-R04"
RULE_BRAND = "BR-R09"
RULE_CAP = "BR-D06"
#: The confirmation question's rule when BR-E03's date check did not ask it (D126).
RULE_CONFIRM = "BR-S10"
#: The sources whose field was read from the printed document (D189): a required field
#: from one of these makes the summary doubtful and worth its buttons.
PRINTED_SOURCES = frozenset({"ocr", "llm"})
#: BR-F14 and BR-F02's button ids: ``u<invoice_id>:yes``, ``u<invoice_id>:no`` and
#: ``u<invoice_id>:dk`` (D189, D190). ``u`` starts no other id: invoice ids start with a
#: digit, payments with ``p`` and BR-S06 with ``t``.
OFFER_BUTTON_PREFIX = "u"
BUTTON_DONT_KNOW_SUFFIX = "dk"

#: An action applied to its invoice in the caller's transaction; the resolved press carries
#: the choice and its menu. It returns the photo files whose rows it cleared, for the
#: caller to delete after commit (R-9.7).
type Action = Callable[
    [AsyncConnection[Any], int, InboundMessage, ButtonPress, frozenset[str]],
    Awaitable[tuple[Path, ...]],
]


def candidate_flows(
    profile_flows: Collection[str], flows: Mapping[str, Flow], tipo: str
) -> list[tuple[str, Flow]]:
    """Return the sender's flows that accept ``tipo``, in config order (BR-R03)."""
    return [
        (flow_id, flow)
        for flow_id, flow in flows.items()
        if flow_id in profile_flows and tipo in flow.accepted_tipos
    ]


def flow_question(
    invoice_id: int, reading: Reading, candidates: Sequence[tuple[str, Flow]], menu: int
) -> tuple[str, list[Button]]:
    """Return BR-R03's question, one [pick] button per candidate flow, then [Cancelar].

    Menu number ``menu`` tells a press on this question apart from one on an older ask (R-3.5).
    """
    buyer = buyer_desc(reading.value("tipo_doc_adq") or "", reading.value("num_doc_adq") or "")
    buttons = [
        Button(id=button_id(invoice_id, PICK_ACTION, choice, menu), title=flow.label)
        for choice, (_flow_id, flow) in enumerate(candidates)
    ]
    buttons.append(Button(id=button_id(invoice_id, CANCEL_ACTION), title=BUTTON_CANCEL))
    return MESSAGES[RULE_ASK].format(buyer_desc=buyer), buttons


def buyer_question(invoice_id: int, reading: Reading, flow: Flow) -> tuple[str, list[Button]]:
    """Return BR-R04's question and its [Sí] / [Cambiar] / [Cancelar] buttons."""
    text = MESSAGES[RULE_BUYER].format(num_doc_adq=reading.value("num_doc_adq") or "", flow=flow)
    buttons = [
        Button(id=button_id(invoice_id, YES_ACTION), title=BUTTON_YES),
        Button(id=button_id(invoice_id, CHANGE_ACTION), title=BUTTON_CHANGE),
        Button(id=button_id(invoice_id, CANCEL_ACTION), title=BUTTON_CANCEL),
    ]
    return text, buttons


def brand_question(invoice_id: int, store: Store, menu: int) -> tuple[str, list[Button]]:
    """Return BR-R09's question for the issuer's store, one [brand] button per comercio, then [Cancelar].

    Menu number ``menu`` tells a press on this question apart from one on an older ask (R-3.5).
    """
    buttons = [
        Button(id=button_id(invoice_id, BRAND_ACTION, choice, menu), title=brand.comercio)
        for choice, brand in enumerate(store.brands)
    ]
    buttons.append(Button(id=button_id(invoice_id, CANCEL_ACTION), title=BUTTON_CANCEL))
    return MESSAGES[RULE_BRAND].format(razon_social=store.razon_social), buttons


def cap_question(reading: Reading, flow: Flow, invoice_id: int) -> tuple[str, list[Button]] | None:
    """Return BR-D06's question and its two buttons when the total is above the cap (R-7.13)."""
    if flow.reimbursement is None:
        return None
    total = reading.amount("total")
    if total is None or total <= flow.reimbursement.cap:
        return None
    cap = f"{flow.reimbursement.cap:.2f}"
    buttons = [
        Button(id=button_id(invoice_id, CONTINUE_ACTION), title=BUTTON_CONTINUE),
        Button(id=button_id(invoice_id, CANCEL_ACTION), title=BUTTON_CANCEL),
    ]
    return MESSAGES[RULE_CAP].format(total=f"{total:.2f}", cap=cap), buttons


def invalid_question(invoice_id: int, rule_id: str) -> tuple[str, list[Button]]:
    """Return BR-V02/BR-V06's question and its [Sí] / [Cancelar] buttons (D125)."""
    buttons = [
        Button(id=button_id(invoice_id, INVALID_OK_ACTION), title=BUTTON_YES),
        Button(id=button_id(invoice_id, CANCEL_ACTION), title=BUTTON_CANCEL),
    ]
    return MESSAGES[rule_id], buttons


def summary_buttons(invoice_id: int, reading: Reading) -> list[Button]:
    """Return the moving summary's [✏️ Corregir] and [✖️ Anular] buttons, or none (R-3.4, D189).

    The buttons appear only when a required field (R-2.13) was read from the printed
    text. A summary whose required fields all came from the QR, an XML, the padrón or the
    sender is not asking the sender for anything, so it carries no buttons; ``/cancelar
    {id}`` still works.
    """
    printed = any(
        (field := reading.fields.get(name)) is not None and field.source in PRINTED_SOURCES
        for name in REQUIRED
    )
    return _edit_cancel(invoice_id, BUTTON_FIX, BUTTON_VOID) if printed else []


def unreadable_buttons(invoice_id: int) -> list[Button]:
    """Return BR-E01's [Editar] and [Cancelar] buttons (BR-E01, D188)."""
    return _edit_cancel(invoice_id, BUTTON_EDIT, BUTTON_CANCEL)


def confirm_buttons(invoice_id: int) -> list[Button]:
    """Return the confirmation's [Confirmar], [Editar] and [Cancelar] buttons (D126, R-3.4)."""
    return [
        Button(id=button_id(invoice_id, CONFIRM_ACTION), title=BUTTON_CONFIRM),
        *_edit_cancel(invoice_id, BUTTON_EDIT, BUTTON_CANCEL),
    ]


def docs_needed_buttons(invoice_id: int, offer_portal: bool) -> list[Button]:
    """Return BR-F13's [📎], [🔗] and [⏳] buttons, the portal one only when offered (D189)."""
    buttons = [Button(id=button_id(invoice_id, SEND_PDF_ACTION), title=BUTTON_SEND_PDF)]
    if offer_portal:
        buttons.append(Button(id=button_id(invoice_id, TEACH_PORTAL_ACTION), title=BUTTON_TEACH_PORTAL))
    buttons.append(Button(id=button_id(invoice_id, WAIT_MAIL_ACTION), title=BUTTON_WAIT_MAIL))
    return buttons


def offer_press_id(invoice_id: int, suffix: str) -> str:
    """Return a BR-F14 or BR-F02 press id: ``u<invoice_id>:<suffix>`` (D189, D190)."""
    return f"{OFFER_BUTTON_PREFIX}{invoice_id}:{suffix}"


def dont_know_button(invoice_id: int) -> Button:
    """Return the [No sé] button BR-F14's offer and BR-F02's question carry (D190)."""
    return Button(id=offer_press_id(invoice_id, BUTTON_DONT_KNOW_SUFFIX), title=BUTTON_DONT_KNOW)


def _edit_cancel(invoice_id: int, edit_title: str, cancel_title: str) -> list[Button]:
    """Return the edit and cancel buttons with the given titles; the actions never change."""
    return [
        Button(id=button_id(invoice_id, EDIT_ACTION), title=edit_title),
        Button(id=button_id(invoice_id, CANCEL_ACTION), title=cancel_title),
    ]
