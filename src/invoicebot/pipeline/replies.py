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

from invoicebot.channels.base import InboundMessage
from invoicebot.channels.whatsapp.payloads import ButtonPress, parse_button_id
from invoicebot.pipeline.outgoing import Addressee, queue_stale
from invoicebot.states import PENDING_USER
from invoicebot.store.invoices import get_state, get_waiting, unpark


async def resolve_reply(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    valid_states: Mapping[str, frozenset[str]],
    asked_rules: Mapping[str, str],
) -> ButtonPress | None:
    """Resolve a button press to its :class:`ButtonPress`, choice included, or None.

    A missing or malformed id, an unknown action, an unknown invoice or another
    profile's invoice resolves to None with nothing sent. A press valid in the
    invoice's current state resolves at once. A press on a ``PENDING_USER`` invoice
    resumes it first when the stored ``waiting_state`` accepts the action and the
    stored ``waiting_rule_id`` is ``asked_rules``' rule for it, so the action then runs
    against the restored state (D94). Any other press sends BR-S02 and resolves to
    None, changing nothing else (R-3.5, BR-S02).
    """
    pressed = _parse_press(msg, valid_states)
    if pressed is None:
        return None
    button_press, states = pressed
    invoice_id = button_press.invoice_id
    invoice = await get_state(conn, invoice_id)
    if invoice is None or invoice[1] != msg.profile_id:
        return None
    state = invoice[0]
    if state in states:
        return button_press
    if await _resumes_parked(conn, invoice_id, state, button_press.action, states, asked_rules):
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
    asked_rules: Mapping[str, str],
) -> bool:
    """Unpark invoice_id when the press answers its parked question, and say whether it did (D94)."""
    if state != PENDING_USER:
        return False
    waiting = await get_waiting(conn, invoice_id)
    if waiting is None:
        return False
    waiting_state, waiting_rule_id = waiting
    asked_rule = asked_rules.get(action)
    if waiting_state not in valid_states or asked_rule is None or waiting_rule_id != asked_rule:
        return False
    return await unpark(conn, invoice_id)


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
