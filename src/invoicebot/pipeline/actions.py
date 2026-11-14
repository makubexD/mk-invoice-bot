"""Button actions: apply one press to its invoice inside the caller's transaction.

A button id names the invoice and the action (R-3.5). :func:`handle_press` checks the
press against :data:`VALID_STATES` and runs the handler in :data:`ACTIONS`; adding an
action is a new entry in both mappings, not a new branch. ``replies.resolve_reply``
answers a stale or foreign press with BR-S02 (BR-S02).

A routing question's answer is stored in the invoice's flow data under ``answers``; the
action puts the invoice back in ``ROUTING`` and enqueues the route step, which validates
the answer again on its next run (D92). [Continuar] answers BR-D06's cap question,
[Cancelar] cancels the invoice from any cancellable state (R-3.4), and [Ya lo envié]
closes a prepared batch as sent (R-7.8, BR-D08). A press that answers the question a
``PENDING_USER`` invoice was parked on resumes the waiting state before its action runs:
:data:`ASKED_RULES` names the rule each action answers for ``resolve_reply`` to check (D94).

Everything here happens in the caller's transaction. The photo files [Ya lo envié]
releases are returned, not deleted: the caller removes them after committing (R-9.7).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from psycopg import AsyncConnection

from invoicebot.channels.base import InboundMessage
from invoicebot.pipeline.extract import ROUTE_STEP
from invoicebot.pipeline.outgoing import Addressee, queue_message, queue_stale
from invoicebot.pipeline.questions import (
    BRAND_ACTION,
    CANCEL_ACTION,
    CHANGE_ACTION,
    CONTINUE_ACTION,
    EDIT_ACTION,
    PICK_ACTION,
    RULE_BRAND,
    RULE_BUYER,
    RULE_CAP,
    YES_ACTION,
)
from invoicebot.pipeline.replies import resolve_reply
from invoicebot.queue.jobs import enqueue
from invoicebot.routing.router import RULE_ASK
from invoicebot.rules.messages_es import CANCELLED_BY_USER, EDIT_PROMPT, MESSAGES
from invoicebot.states import CANCELLABLE, EDITABLE
from invoicebot.store.flow_b import (
    get_batch_state,
    load_flow_data,
    merge_flow_data,
    open_batch,
    set_batch_state,
    take_photo_paths,
)
from invoicebot.store.invoices import get_state_rule, load_flow, set_state

#: The button action only this module applies; questions own the ones they show (R-3.5).
SENT_ACTION = "sent"

CONFIRMING_STATE = "CONFIRMING"
ROUTING_STATE = "ROUTING"
BATCHED_STATE = "BATCHED"
CANCELLED_STATE = "CANCELLED"
DONE_STATE = "DONE"

RULE_SENT = "BR-D08"

PREPARED_BATCH_STATE: Literal["prepared"] = "prepared"
SENT_BATCH_STATE = "sent"

#: The invoice states each action applies to; another state gets BR-S02 (R-3.5, BR-S02).
VALID_STATES: Mapping[str, frozenset[str]] = {
    PICK_ACTION: frozenset({CONFIRMING_STATE}),
    YES_ACTION: frozenset({CONFIRMING_STATE}),
    CHANGE_ACTION: frozenset({CONFIRMING_STATE}),
    BRAND_ACTION: frozenset({CONFIRMING_STATE}),
    CONTINUE_ACTION: frozenset({CONFIRMING_STATE}),
    CANCEL_ACTION: CANCELLABLE,
    EDIT_ACTION: EDITABLE,
    SENT_ACTION: frozenset({BATCHED_STATE}),
}

#: The rule each question action answers; a parked invoice resumes only for its own (D94).
ASKED_RULES: Mapping[str, str] = {
    PICK_ACTION: RULE_ASK,
    YES_ACTION: RULE_BUYER,
    CHANGE_ACTION: RULE_BUYER,
    BRAND_ACTION: RULE_BRAND,
    CONTINUE_ACTION: RULE_CAP,
}

#: An action applied to its invoice in the caller's transaction; it returns the photo
#: files whose rows it cleared, for the caller to delete after commit (R-9.7).
type Action = Callable[[AsyncConnection[Any], int, InboundMessage, int | None], Awaitable[tuple[Path, ...]]]


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
    resolved = await resolve_reply(conn, msg, VALID_STATES, ASKED_RULES)
    if resolved is None:
        return PressResult(invoice_id=None)
    photo_paths = await ACTIONS[resolved.action](conn, resolved.invoice_id, msg, resolved.choice)
    return PressResult(invoice_id=resolved.invoice_id, photo_paths=photo_paths)


async def _pick(
    conn: AsyncConnection[Any], invoice_id: int, msg: InboundMessage, choice: int | None
) -> tuple[Path, ...]:
    """Store the flow the sender picked from BR-R03's menu and re-run routing (BR-R03).

    A missing menu or an out-of-range choice is a stale button: BR-S02, no change.
    """
    if not await _still_asks(conn, invoice_id, msg, RULE_ASK):
        return ()
    picked = await _choice_at(conn, invoice_id, choice)
    if picked is None:
        await send_stale(conn, msg, invoice_id)
        return ()
    await _record_answer(conn, invoice_id, RULE_ASK, {"flow": picked})
    return ()


async def _yes(
    conn: AsyncConnection[Any], invoice_id: int, msg: InboundMessage, _choice: int | None
) -> tuple[Path, ...]:
    """Record that the buyer is right, so routing skips BR-R04 (BR-R04)."""
    if not await _still_asks(conn, invoice_id, msg, RULE_BUYER):
        return ()
    await _record_answer(conn, invoice_id, RULE_BUYER, {"buyer_ok": True})
    return ()


async def _change(
    conn: AsyncConnection[Any], invoice_id: int, msg: InboundMessage, _choice: int | None
) -> tuple[Path, ...]:
    """Record that the buyer is wrong, so routing asks BR-R03 again (BR-R04)."""
    if not await _still_asks(conn, invoice_id, msg, RULE_BUYER):
        return ()
    await _record_answer(conn, invoice_id, RULE_BUYER, {"buyer_ok": False})
    return ()


async def _brand(
    conn: AsyncConnection[Any], invoice_id: int, msg: InboundMessage, choice: int | None
) -> tuple[Path, ...]:
    """Store the comercio the sender picked from BR-R09's menu and re-run routing (BR-R09).

    A missing menu or an out-of-range choice is a stale button: BR-S02, no change.
    """
    if not await _still_asks(conn, invoice_id, msg, RULE_BRAND):
        return ()
    comercio = await _choice_at(conn, invoice_id, choice)
    if comercio is None:
        await send_stale(conn, msg, invoice_id)
        return ()
    await _record_answer(conn, invoice_id, RULE_BRAND, {"comercio": comercio})
    return ()


async def _continue(
    conn: AsyncConnection[Any], invoice_id: int, msg: InboundMessage, _choice: int | None
) -> tuple[Path, ...]:
    """Record that the sender accepted BR-D06's cap and re-run routing (BR-D06)."""
    if not await _still_asks(conn, invoice_id, msg, RULE_CAP):
        return ()
    await _record_answer(conn, invoice_id, RULE_CAP, {"cap_ok": True})
    return ()


async def _cancel(
    conn: AsyncConnection[Any], invoice_id: int, msg: InboundMessage, _choice: int | None
) -> tuple[Path, ...]:
    """Cancel the invoice from any CANCELLABLE state and tell its sender (R-3.4, BR-D06).

    CANCELLED is in ``NEEDS_RULE`` and the catalog has no sender-cancel rule, so this
    keeps recording BR-D06's id, as it did when [Cancelar] only answered BR-D06.
    """
    # shortcut: spec 06 has no sender-cancel rule, so a cancel outside BR-D06 still records
    # BR-D06; it needs its own catalogued rule before reports read rule_id.
    await set_state(conn, invoice_id, CANCELLED_STATE, rule_id=RULE_CAP)
    await _reply(conn, msg, invoice_id, None, CANCELLED_BY_USER.format(id=invoice_id))
    return ()


async def _edit(
    conn: AsyncConnection[Any], invoice_id: int, msg: InboundMessage, _choice: int | None
) -> tuple[Path, ...]:
    """Ask the sender for ``campo=valor`` edits, leaving the invoice as it is (R-3.4, D97)."""
    await _reply(conn, msg, invoice_id, None, EDIT_PROMPT)
    return ()


async def _sent(
    conn: AsyncConnection[Any], invoice_id: int, msg: InboundMessage, _choice: int | None
) -> tuple[Path, ...]:
    """Close the invoice's prepared batch as sent and clear its photos (R-7.8, BR-D08).

    A BATCHED invoice whose batch is not ``prepared`` (a failed send, for example) gets
    BR-S02 and changes nothing.
    """
    flow_id = await load_flow(conn, invoice_id)
    if flow_id is None or await get_batch_state(conn, invoice_id) != PREPARED_BATCH_STATE:
        await send_stale(conn, msg, invoice_id)
        return ()
    batch_id = await open_batch(conn, invoice_id, flow_id, PREPARED_BATCH_STATE)
    await set_batch_state(conn, batch_id, SENT_BATCH_STATE)
    await set_state(conn, invoice_id, DONE_STATE, rule_id=RULE_SENT)
    await _reply(conn, msg, invoice_id, RULE_SENT, MESSAGES[RULE_SENT])
    return tuple(await take_photo_paths(conn, invoice_id))


async def _still_asks(conn: AsyncConnection[Any], invoice_id: int, msg: InboundMessage, rule_id: str) -> bool:
    """Return whether the invoice still waits on rule_id's question (BR-S02).

    A button from another question is stale: it is answered BR-S02 and changes nothing.
    """
    _, current_rule = await _invoice_state(conn, invoice_id)
    if current_rule == rule_id:
        return True
    await send_stale(conn, msg, invoice_id)
    return False


async def _choice_at(conn: AsyncConnection[Any], invoice_id: int, choice: int | None) -> Any | None:
    """Return ``choices[choice]`` from the invoice's flow data, or None when it is absent (BR-S02)."""
    choices = (await load_flow_data(conn, invoice_id)).get("choices")
    if choice is None or not isinstance(choices, list) or not 0 <= choice < len(choices):
        return None
    return choices[choice]


async def _record_answer(
    conn: AsyncConnection[Any], invoice_id: int, rule_id: str, answer: Mapping[str, Any]
) -> None:
    """Store one answer, put the invoice back in ROUTING and enqueue the route step (D92).

    ``||`` is a shallow merge, so the answers stored before this one are read first and
    kept. The route step validates the answer again on its next run.
    """
    flow_data = await load_flow_data(conn, invoice_id)
    existing = flow_data.get("answers")
    answers: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    answers.update(answer)
    await merge_flow_data(conn, invoice_id, {"answers": answers})
    await set_state(conn, invoice_id, ROUTING_STATE, rule_id=rule_id, expected=CONFIRMING_STATE)
    await enqueue(conn, invoice_id, ROUTE_STEP)


async def _invoice_state(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str, str | None]:
    """Return the invoice's state and rule id, raising when it disappeared mid-transaction."""
    state = await get_state_rule(conn, invoice_id)
    if state is None:
        raise RuntimeError(f"invoice {invoice_id} does not exist")
    return state


async def send_stale(conn: AsyncConnection[Any], msg: InboundMessage, invoice_id: int) -> None:
    """Queue BR-S02 for a press or override the pipeline refuses in the caller's transaction.

    One home for BR-S02's text, shared with the override path (BR-S02).
    """
    state, _ = await _invoice_state(conn, invoice_id)
    await queue_stale(
        conn,
        Addressee(msg.channel, msg.profile_id),
        invoice_id=invoice_id,
        state=state,
    )


async def _reply(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    invoice_id: int,
    rule_id: str | None,
    text: str,
) -> None:
    """Queue ``text`` for the press's sender in the caller's transaction."""
    await queue_message(
        conn,
        Addressee(msg.channel, msg.profile_id),
        invoice_id=invoice_id,
        rule_id=rule_id,
        text=text,
    )


#: The action each button runs; its keys match :data:`VALID_STATES`.
ACTIONS: Mapping[str, Action] = {
    PICK_ACTION: _pick,
    YES_ACTION: _yes,
    CHANGE_ACTION: _change,
    BRAND_ACTION: _brand,
    CONTINUE_ACTION: _continue,
    CANCEL_ACTION: _cancel,
    EDIT_ACTION: _edit,
    SENT_ACTION: _sent,
}
