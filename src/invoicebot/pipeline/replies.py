"""Button replies: check a press against the invoice's state (R-3.5, BR-S02).

A reply id carries the invoice and the action. The routing phase decides which
states accept each action; this module only checks the press against them and
answers a stale button with BR-S02, changing nothing else.
"""

from collections.abc import Mapping
from typing import Any

from psycopg import AsyncConnection

from invoicebot.channels.base import InboundMessage
from invoicebot.channels.whatsapp.payloads import ButtonPress, parse_button_id
from invoicebot.pipeline.outgoing import Addressee, queue_stale
from invoicebot.store.invoices import get_state


async def resolve_reply(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    valid_states: Mapping[str, frozenset[str]],
) -> ButtonPress | None:
    """Resolve a button press to its :class:`ButtonPress`, choice included, or None.

    A missing or malformed id, an unknown action, an unknown invoice or another
    profile's invoice resolves to None with nothing sent. A button pressed for
    an invoice outside the action's valid states sends BR-S02 and resolves to
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
    await queue_stale(
        conn,
        Addressee(msg.channel, msg.profile_id),
        invoice_id=invoice_id,
        state=state,
    )
    return None


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
