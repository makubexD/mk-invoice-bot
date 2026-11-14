"""Button actions: apply one press to its invoice inside the caller's transaction.

A button id names the invoice and the action (R-3.5). :func:`handle_press` checks the
press against :data:`VALID_STATES` and runs the handler in :data:`ACTIONS`; adding an
action is a new entry in both mappings, not a new branch. ``replies.resolve_reply``
answers a stale or foreign press with BR-S02 (BR-S02). [Continuar] resumes a BR-D06
invoice (R-7.13), [Cancelar] cancels it, and [Ya lo envié] closes a prepared batch as
sent (R-7.8, BR-D08).

Everything here happens in the caller's transaction. The photo files [Ya lo envié]
releases are returned, not deleted: the caller removes them after committing (R-9.7).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from psycopg import AsyncConnection

from invoicebot.channels.base import InboundMessage, OutboundMessage
from invoicebot.pipeline.fetch import FETCH_STEP
from invoicebot.pipeline.replies import resolve_reply
from invoicebot.queue import outbox
from invoicebot.queue.jobs import enqueue
from invoicebot.rules.messages_es import CANCELLED_BY_USER, MESSAGES, state_desc
from invoicebot.store.flow_b import get_batch_state, open_batch, set_batch_state, take_photo_paths
from invoicebot.store.invoices import get_state_rule, load_flow, set_state

#: The button actions; the handlers that show a button import these, so ids always match (R-3.5).
CONTINUE_ACTION = "continue"
CANCEL_ACTION = "cancel"
SENT_ACTION = "sent"

CONFIRMING_STATE = "CONFIRMING"
BATCHED_STATE = "BATCHED"
FETCHING_STATE = "FETCHING_DOCS"
CANCELLED_STATE = "CANCELLED"
DONE_STATE = "DONE"

RULE_CAP = "BR-D06"
RULE_SENT = "BR-D08"
RULE_STALE_BUTTON = "BR-S02"

PREPARED_BATCH_STATE: Literal["prepared"] = "prepared"
SENT_BATCH_STATE = "sent"

#: The invoice states each action applies to; another state gets BR-S02 (R-3.5, BR-S02).
VALID_STATES: Mapping[str, frozenset[str]] = {
    CONTINUE_ACTION: frozenset({CONFIRMING_STATE}),
    CANCEL_ACTION: frozenset({CONFIRMING_STATE}),
    SENT_ACTION: frozenset({BATCHED_STATE}),
}

#: An action applied to its invoice in the caller's transaction; it returns the photo
#: files whose rows it cleared, for the caller to delete after commit (R-9.7).
type Action = Callable[[AsyncConnection[Any], int, InboundMessage], Awaitable[tuple[Path, ...]]]


@dataclass(frozen=True)
class PressResult:
    """What one button press did: its invoice and the photo files to delete after commit."""

    invoice_id: int | None
    photo_paths: tuple[Path, ...] = ()


async def handle_press(conn: AsyncConnection[Any], msg: InboundMessage) -> PressResult:
    """Apply one button press in the caller's transaction (R-3.5, R-7.8, R-7.13).

    A press that does not resolve changes nothing and returns no invoice; a stale one has
    already been answered with BR-S02 by :func:`resolve_reply`. The returned photo paths
    are the files whose rows the action cleared: the caller deletes them after committing.
    """
    resolved = await resolve_reply(conn, msg, VALID_STATES)
    if resolved is None:
        return PressResult(invoice_id=None)
    invoice_id, action = resolved
    photo_paths = await ACTIONS[action](conn, invoice_id, msg)
    return PressResult(invoice_id=invoice_id, photo_paths=photo_paths)


async def _continue(conn: AsyncConnection[Any], invoice_id: int, msg: InboundMessage) -> tuple[Path, ...]:
    """Continue a BR-D06 invoice into its fetch step (R-7.13).

    Any other CONFIRMING invoice gets BR-S02 instead, whatever button it shows.
    """
    _, rule_id = await _invoice_state(conn, invoice_id)
    if rule_id != RULE_CAP:
        await _stale(conn, msg, invoice_id)
        return ()
    await set_state(conn, invoice_id, FETCHING_STATE, rule_id=RULE_CAP)
    await enqueue(conn, invoice_id, FETCH_STEP)
    return ()


async def _cancel(conn: AsyncConnection[Any], invoice_id: int, msg: InboundMessage) -> tuple[Path, ...]:
    """Cancel a BR-D06 invoice and tell its sender in one line (R-7.13)."""
    await set_state(conn, invoice_id, CANCELLED_STATE, rule_id=RULE_CAP)
    await _reply(conn, msg, None, CANCELLED_BY_USER.format(id=invoice_id))
    return ()


async def _sent(conn: AsyncConnection[Any], invoice_id: int, msg: InboundMessage) -> tuple[Path, ...]:
    """Close the invoice's prepared batch as sent and clear its photos (R-7.8, BR-D08).

    A BATCHED invoice whose batch is not ``prepared`` (a failed send, for example) gets
    BR-S02 and changes nothing.
    """
    flow_id = await load_flow(conn, invoice_id)
    if flow_id is None or await get_batch_state(conn, invoice_id) != PREPARED_BATCH_STATE:
        await _stale(conn, msg, invoice_id)
        return ()
    batch_id = await open_batch(conn, invoice_id, flow_id, PREPARED_BATCH_STATE)
    await set_batch_state(conn, batch_id, SENT_BATCH_STATE)
    await set_state(conn, invoice_id, DONE_STATE, rule_id=RULE_SENT)
    await _reply(conn, msg, RULE_SENT, MESSAGES[RULE_SENT])
    return tuple(await take_photo_paths(conn, invoice_id))


async def _invoice_state(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str, str | None]:
    """Return the invoice's state and rule id, raising when it disappeared mid-transaction."""
    state = await get_state_rule(conn, invoice_id)
    if state is None:
        raise RuntimeError(f"invoice {invoice_id} does not exist")
    return state


async def _stale(conn: AsyncConnection[Any], msg: InboundMessage, invoice_id: int) -> None:
    """Queue BR-S02 for a press the action refuses in the caller's transaction (BR-S02)."""
    state, _ = await _invoice_state(conn, invoice_id)
    text = MESSAGES[RULE_STALE_BUTTON].format(id=invoice_id, state_desc=state_desc(state))
    await _reply(conn, msg, RULE_STALE_BUTTON, text)


async def _reply(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    rule_id: str | None,
    text: str,
) -> None:
    """Queue ``text`` for the press's sender in the caller's transaction."""
    message = OutboundMessage(
        channel=msg.channel,
        recipient=msg.profile_id,
        profile_id=msg.profile_id,
        rule_id=rule_id,
        text=text,
    )
    await outbox.add(conn, msg.channel, msg.profile_id, message.model_dump(mode="json"))


#: The action each button runs; its keys match :data:`VALID_STATES`.
ACTIONS: Mapping[str, Action] = {
    CONTINUE_ACTION: _continue,
    CANCEL_ACTION: _cancel,
    SENT_ACTION: _sent,
}
