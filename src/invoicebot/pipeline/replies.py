"""Button replies: check a press against the invoice's state (R-3.5, BR-S02, D94).

A reply id carries the invoice and the action. The routing phase decides which
states accept each action; this module checks the press against them and answers a
stale button with BR-S02. A press that answers the question a ``PENDING_USER``
invoice was parked on resumes its waiting state first; any other press leaves it
parked (D94).
"""

from collections.abc import Mapping
from typing import Any

from psycopg import AsyncConnection

from invoicebot.channels.base import ButtonPress, InboundMessage
from invoicebot.channels.whatsapp.payloads import parse_button_id
from invoicebot.pipeline.outgoing import Addressee, queue_stale
from invoicebot.states import PENDING_USER
from invoicebot.store.flow_b import load_flow_data
from invoicebot.store.invoices import get_state, get_waiting, lock_invoice, unpark


async def resolve_reply(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    valid_states: Mapping[str, frozenset[str]],
    asked_rules: Mapping[str, frozenset[str]],
) -> ButtonPress | None:
    """Resolve a button press to its :class:`ButtonPress`, choice included, or None.

    A missing or malformed id, an unknown action, an unknown invoice or another
    profile's invoice resolves to None with nothing sent. A press valid in the
    invoice's current state resolves at once. A press on a ``PENDING_USER`` invoice
    resumes it first when the stored ``waiting_state`` accepts the action and the
    stored ``waiting_rule_id`` is one of ``asked_rules``' rules for it, so the action then runs
    against the restored state (D94). A press that carries a choice the invoice no longer
    offers, because it came from another menu or an out-of-range option, does not resume
    the parked invoice either (D94, BR-S02). Any other press sends BR-S02 and resolves to
    None, changing nothing else (R-3.5, BR-S02). The invoice row stays locked for the
    caller's transaction, so the state checked here is the state the action sees (D95).
    """
    pressed = _parse_press(msg, valid_states)
    if pressed is None:
        return None
    button_press, states = pressed
    invoice_id = button_press.invoice_id
    # The row lock holds the checked state until the caller commits, so the action
    # cannot act on a state a job moved meanwhile (D95, D96).
    await lock_invoice(conn, invoice_id)
    invoice = await get_state(conn, invoice_id)
    if invoice is None or invoice[1] != msg.profile_id:
        return None
    state = invoice[0]
    if state in states:
        return button_press
    if await _choice_still_offered(conn, invoice_id, button_press) and await _resumes_parked(
        conn, invoice_id, state, button_press.action, states, asked_rules
    ):
        return button_press
    await queue_stale(
        conn,
        Addressee(msg.channel, msg.profile_id),
        invoice_id=invoice_id,
        state=state,
    )
    return None


async def _resumes_parked(
    conn: AsyncConnection[Any],
    invoice_id: int,
    state: str,
    action: str,
    valid_states: frozenset[str],
    asked_rules: Mapping[str, frozenset[str]],
) -> bool:
    """Unpark invoice_id when the press answers its parked question, and say whether it did (D94)."""
    if state != PENDING_USER:
        return False
    waiting = await get_waiting(conn, invoice_id)
    if waiting is None:
        return False
    waiting_state, waiting_rule_id = waiting
    asked_rule = asked_rules.get(action)
    if waiting_state not in valid_states or asked_rule is None or waiting_rule_id not in asked_rule:
        return False
    return await unpark(conn, invoice_id)


async def _choice_still_offered(conn: AsyncConnection[Any], invoice_id: int, press: ButtonPress) -> bool:
    """Say whether press's choice still matches the menu the invoice stored (D94, BR-S02).

    A press without a choice is always fine; one whose choice is missing, out of range or
    from another menu is stale and must not resume a parked invoice.
    """
    if press.choice is None:
        return True
    return await choice_at(conn, invoice_id, press.choice, press.menu) is not None


async def choice_at(
    conn: AsyncConnection[Any], invoice_id: int, choice: int | None, menu: int | None
) -> Any | None:
    """Return ``choices[choice]`` from the invoice's flow data, or None when it is stale (BR-S02).

    A missing or out-of-range choice is stale, and so is a choice from another menu: the
    stored ``menu`` must be an int equal to the press's, so an older menu is a stale button.
    """
    flow_data = await load_flow_data(conn, invoice_id)
    choices = flow_data.get("choices")
    stored_menu = flow_data.get("menu")
    if isinstance(stored_menu, bool) or not isinstance(stored_menu, int) or menu != stored_menu:
        return None
    if choice is None or not isinstance(choices, list) or not 0 <= choice < len(choices):
        return None
    return choices[choice]


def _parse_press(
    msg: InboundMessage,
    valid_states: Mapping[str, frozenset[str]],
) -> tuple[ButtonPress, frozenset[str]] | None:
    """Return the parsed press and its action's valid states, or None."""
    if msg.button_id is None:
        return None
    parsed = parse_button_id(msg.button_id)
    if parsed is None:
        return None
    states = valid_states.get(parsed.action)
    if states is None:
        return None
    return parsed, states
