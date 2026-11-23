"""The questions the pipeline asks and their buttons (BR-R03, BR-R04, BR-R09, BR-D06, D125).

Each builder is pure: it returns the es-PE text from the catalog and the buttons that
carry the answer back, with ids ``"<invoice_id>:<action>[:<choice>:<menu>]"`` (R-3.5).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence

from invoicebot.channels.base import Button
from invoicebot.channels.buttons import button_id
from invoicebot.config.models import Flow, Store
from invoicebot.extract.fields import Reading
from invoicebot.routing.router import RULE_ASK
from invoicebot.rules.messages_es import (
    BUTTON_CANCEL,
    BUTTON_CHANGE,
    BUTTON_CONFIRM,
    BUTTON_CONTINUE,
    BUTTON_EDIT,
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
#: The [Sí] that accepts a BR-V02/BR-V06 comprobante (D125).
INVALID_OK_ACTION = "invalid_ok"
#: D126's [Confirmar]: the sender accepts the routed summary before validation.
CONFIRM_ACTION = "confirm"
#: The rule ids of the questions only routing asks.
RULE_BUYER = "BR-R04"
RULE_BRAND = "BR-R09"
RULE_CAP = "BR-D06"
#: The confirmation question's rule when BR-E03's date check did not ask it (D126).
RULE_CONFIRM = "BR-S10"


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


def summary_buttons(invoice_id: int) -> list[Button]:
    """Return the routed summary's [Editar] and [Cancelar] buttons (R-3.4)."""
    return [
        Button(id=button_id(invoice_id, EDIT_ACTION), title=BUTTON_EDIT),
        Button(id=button_id(invoice_id, CANCEL_ACTION), title=BUTTON_CANCEL),
    ]


def confirm_buttons(invoice_id: int) -> list[Button]:
    """Return the confirmation's [Confirmar], [Editar] and [Cancelar] buttons (D126, R-3.4)."""
    return [
        Button(id=button_id(invoice_id, CONFIRM_ACTION), title=BUTTON_CONFIRM),
        *summary_buttons(invoice_id),
    ]
