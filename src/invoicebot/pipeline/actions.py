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

from invoicebot.channels.base import Button, ButtonPress, InboundMessage
from invoicebot.channels.whatsapp.payloads import button_id
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
from invoicebot.pipeline.replies import choice_at, resolve_reply
from invoicebot.pipeline.steps import FOLDER_STATUS_STEP, RESEND_EML_STEP, RESEND_LAPTOP_STEP
from invoicebot.queue.jobs import enqueue
from invoicebot.routing.router import RULE_ASK
from invoicebot.rules.messages_es import (
    BUTTON_ARRIVED,
    BUTTON_SELF_SEND,
    BUTTON_VIA_LAPTOP,
    CANCELLED_BY_USER,
    EDIT_PROMPT,
    MESSAGES,
)
from invoicebot.states import CANCELLABLE, EDITABLE
from invoicebot.store.flow_b import (
    get_batch_state,
    load_flow_data,
    merge_flow_data,
    open_batch,
    set_batch_state,
    take_photo_paths,
)
from invoicebot.store.invoices import (
    clear_editing,
    get_state_rule,
    load_flow,
    load_invoice_head,
    load_reading,
    mark_editing,
    set_state,
)
from invoicebot.store.mail_jobs import has_mail_job

#: The button action only this module applies; questions own the ones they show (R-3.5).
SENT_ACTION = "sent"
#: BR-D13's [No llegó] action (R-7.8).
NOT_ARRIVED_ACTION = "not_arrived"
#: BR-D11's [Ya llegó] action (R-7.8).
ARRIVED_ACTION = "arrived"
#: BR-D11's two resend choices (R-7.14, R-7.8, D109).
VIA_LAPTOP_ACTION = "via_laptop"
SELF_SEND_ACTION = "self_send"

CONFIRMING_STATE = "CONFIRMING"
ROUTING_STATE = "ROUTING"
BATCHED_STATE = "BATCHED"
CANCELLED_STATE = "CANCELLED"
DONE_STATE = "DONE"

RULE_SENT = "BR-D08"
#: BR-D10: the invoice is filed and its mail was sent (spec 06).
RULE_DONE = "BR-D10"
#: BR-D11: the mail's delivery is not confirmed, so the sender is asked (spec 06).
RULE_NOT_CONFIRMED = "BR-D11"

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
    NOT_ARRIVED_ACTION: frozenset({DONE_STATE, BATCHED_STATE}),
    ARRIVED_ACTION: frozenset({DONE_STATE, BATCHED_STATE}),
    VIA_LAPTOP_ACTION: frozenset({DONE_STATE, BATCHED_STATE}),
    SELF_SEND_ACTION: frozenset({DONE_STATE, BATCHED_STATE}),
}

#: The rule each question action answers; a parked invoice resumes only for its own (D94).
ASKED_RULES: Mapping[str, str] = {
    PICK_ACTION: RULE_ASK,
    YES_ACTION: RULE_BUYER,
    CHANGE_ACTION: RULE_BUYER,
    BRAND_ACTION: RULE_BRAND,
    CONTINUE_ACTION: RULE_CAP,
}

#: An action applied to its invoice in the caller's transaction; the resolved press carries
#: the choice and its menu. It returns the photo files whose rows it cleared, for the
#: caller to delete after commit (R-9.7).
type Action = Callable[
    [AsyncConnection[Any], int, InboundMessage, ButtonPress, frozenset[str]],
    Awaitable[tuple[Path, ...]],
]


@dataclass(frozen=True)
class PressResult:
    """What one button press did: its invoice and the photo files to delete after commit."""

    invoice_id: int | None
    photo_paths: tuple[Path, ...] = ()


async def handle_press(
    conn: AsyncConnection[Any], msg: InboundMessage, laptop_flows: frozenset[str]
) -> PressResult:
    """Apply one button press in the caller's transaction (R-3.5, R-7.8, R-7.13).

    A press that does not resolve changes nothing and returns no invoice; a stale one has
    already been answered with BR-S02 by :func:`resolve_reply`. The returned photo paths
    are the files whose rows the action cleared: the caller deletes them after committing.
    ``laptop_flows`` names the flows whose mailer lists ``laptop_outlook`` (D109).
    """
    resolved = await resolve_reply(conn, msg, VALID_STATES, ASKED_RULES)
    if resolved is None:
        return PressResult(invoice_id=None)
    # Any press ends a pending [Editar] on its invoice; [Editar] itself pins it again (D90).
    await clear_editing(conn, resolved.invoice_id)
    photo_paths = await ACTIONS[resolved.action](conn, resolved.invoice_id, msg, resolved, laptop_flows)
    return PressResult(invoice_id=resolved.invoice_id, photo_paths=photo_paths)


async def _pick(
    conn: AsyncConnection[Any],
    invoice_id: int,
    msg: InboundMessage,
    press: ButtonPress,
    _laptop_flows: frozenset[str],
) -> tuple[Path, ...]:
    """Store the flow the sender picked from BR-R03's menu and re-run routing (BR-R03).

    A missing menu, a choice from an older menu or an out-of-range choice is a stale
    button: BR-S02, no change.
    """
    if not await _still_asks(conn, invoice_id, msg, RULE_ASK):
        return ()
    picked = await choice_at(conn, invoice_id, press.choice, press.menu)
    if picked is None:
        await send_stale(conn, msg, invoice_id)
        return ()
    await _record_answer(conn, invoice_id, RULE_ASK, {"flow": picked})
    return ()


async def _yes(
    conn: AsyncConnection[Any],
    invoice_id: int,
    msg: InboundMessage,
    _press: ButtonPress,
    _laptop_flows: frozenset[str],
) -> tuple[Path, ...]:
    """Record that the buyer is right, so routing skips BR-R04 (BR-R04)."""
    if not await _still_asks(conn, invoice_id, msg, RULE_BUYER):
        return ()
    await _record_answer(conn, invoice_id, RULE_BUYER, {"buyer_ok": True})
    return ()


async def _change(
    conn: AsyncConnection[Any],
    invoice_id: int,
    msg: InboundMessage,
    _press: ButtonPress,
    _laptop_flows: frozenset[str],
) -> tuple[Path, ...]:
    """Record that the buyer is wrong, so routing asks BR-R03 again (BR-R04)."""
    if not await _still_asks(conn, invoice_id, msg, RULE_BUYER):
        return ()
    await _record_answer(conn, invoice_id, RULE_BUYER, {"buyer_ok": False})
    return ()


async def _brand(
    conn: AsyncConnection[Any],
    invoice_id: int,
    msg: InboundMessage,
    press: ButtonPress,
    _laptop_flows: frozenset[str],
) -> tuple[Path, ...]:
    """Store the comercio the sender picked from BR-R09's menu and re-run routing (BR-R09).

    A missing menu, a choice from an older menu or an out-of-range choice is a stale
    button: BR-S02, no change.
    """
    if not await _still_asks(conn, invoice_id, msg, RULE_BRAND):
        return ()
    comercio = await choice_at(conn, invoice_id, press.choice, press.menu)
    if comercio is None:
        await send_stale(conn, msg, invoice_id)
        return ()
    await _record_answer(conn, invoice_id, RULE_BRAND, {"comercio": comercio})
    return ()


async def _continue(
    conn: AsyncConnection[Any],
    invoice_id: int,
    msg: InboundMessage,
    _press: ButtonPress,
    _laptop_flows: frozenset[str],
) -> tuple[Path, ...]:
    """Record that the sender accepted BR-D06's cap and re-run routing (BR-D06)."""
    if not await _still_asks(conn, invoice_id, msg, RULE_CAP):
        return ()
    await _record_answer(conn, invoice_id, RULE_CAP, {"cap_ok": True})
    return ()


async def cancel_invoice(
    conn: AsyncConnection[Any], invoice_id: int, msg: InboundMessage, *, expected: str
) -> None:
    """Cancel the invoice and tell its sender (R-3.4, BR-D06, D95, D96).

    Shared by the [Cancelar] button and ``/cancelar``, which both pass the state they
    checked; an invoice that moved on raises :class:`StateMoved` and nothing is cancelled
    (D95). CANCELLED is in ``NEEDS_RULE`` and the catalog has no sender-cancel rule, so
    this keeps recording BR-D06's id, as it did when [Cancelar] only answered BR-D06.
    """
    # shortcut: spec 06 has no sender-cancel rule, so a cancel outside BR-D06 still records
    # BR-D06; it needs its own catalogued rule before reports read rule_id.
    await set_state(conn, invoice_id, CANCELLED_STATE, rule_id=RULE_CAP, expected=expected)
    await _reply(conn, msg, invoice_id, None, CANCELLED_BY_USER.format(id=invoice_id))


async def _cancel(
    conn: AsyncConnection[Any],
    invoice_id: int,
    msg: InboundMessage,
    _press: ButtonPress,
    _laptop_flows: frozenset[str],
) -> tuple[Path, ...]:
    """Cancel the invoice from any CANCELLABLE state (R-3.4, BR-D06, D95, D96).

    The state :func:`resolve_reply` checked is reloaded and passed as ``expected``, so a
    press that lost a race to another transition changes nothing (D95).
    """
    head = await load_invoice_head(conn, invoice_id)
    if head is None:
        return ()
    await cancel_invoice(conn, invoice_id, msg, expected=head.state)
    return ()


async def _edit(
    conn: AsyncConnection[Any],
    invoice_id: int,
    msg: InboundMessage,
    _press: ButtonPress,
    _laptop_flows: frozenset[str],
) -> tuple[Path, ...]:
    """Ask the sender for ``campo=valor`` edits and pin this invoice as their target (R-3.4, R-3.5, D90)."""
    await mark_editing(conn, msg.profile_id, invoice_id)
    await _reply(conn, msg, invoice_id, None, EDIT_PROMPT)
    return ()


async def _sent(
    conn: AsyncConnection[Any],
    invoice_id: int,
    msg: InboundMessage,
    _press: ButtonPress,
    _laptop_flows: frozenset[str],
) -> tuple[Path, ...]:
    """Close the invoice's prepared batch as sent and clear its photos (R-7.8, R-7.11, BR-D08).

    A BATCHED invoice whose batch is not ``prepared`` (a failed send, for example) gets
    BR-S02 and changes nothing. A sent batch enqueues the ``folder_status`` job in the same
    transaction.
    """
    flow_id = await load_flow(conn, invoice_id)
    if flow_id is None or await get_batch_state(conn, invoice_id) != PREPARED_BATCH_STATE:
        await send_stale(conn, msg, invoice_id)
        return ()
    batch_id = await open_batch(conn, invoice_id, flow_id, PREPARED_BATCH_STATE)
    await set_batch_state(conn, batch_id, SENT_BATCH_STATE)
    await enqueue(conn, invoice_id, FOLDER_STATUS_STEP)
    await set_state(conn, invoice_id, DONE_STATE, rule_id=RULE_SENT)
    await _reply(conn, msg, invoice_id, RULE_SENT, MESSAGES[RULE_SENT])
    return tuple(await take_photo_paths(conn, invoice_id))


async def _not_arrived(
    conn: AsyncConnection[Any],
    invoice_id: int,
    _msg: InboundMessage,
    _press: ButtonPress,
    laptop_flows: frozenset[str],
) -> tuple[Path, ...]:
    """Ask the invoice's sender how the mail went out (R-7.8, BR-D11)."""
    await send_not_confirmed(conn, invoice_id, laptop_flows)
    return ()


async def _arrived(
    conn: AsyncConnection[Any],
    invoice_id: int,
    msg: InboundMessage,
    _press: ButtonPress,
    _laptop_flows: frozenset[str],
) -> tuple[Path, ...]:
    """File the invoice DONE with BR-D10 and answer the press (R-7.8, BR-D11, D109).

    A ``prepared`` batch becomes ``sent`` and queues its ``folder_status`` job in the
    press's transaction, and its photo files are returned for deletion (R-9.7); a batch
    already sent only gets BR-D10's text.
    """
    photos: tuple[Path, ...] = ()
    if await get_batch_state(conn, invoice_id) == PREPARED_BATCH_STATE:
        flow_id = await load_flow(conn, invoice_id)
        if flow_id is None:
            raise RuntimeError(f"invoice {invoice_id} has no flow")
        batch_id = await open_batch(conn, invoice_id, flow_id, PREPARED_BATCH_STATE)
        await set_batch_state(conn, batch_id, SENT_BATCH_STATE)
        await enqueue(conn, invoice_id, FOLDER_STATUS_STEP)
        await set_state(conn, invoice_id, DONE_STATE, rule_id=RULE_DONE)
        photos = tuple(await take_photo_paths(conn, invoice_id))
    folder = (await load_flow_data(conn, invoice_id))["folder"]
    await _reply(conn, msg, invoice_id, RULE_DONE, MESSAGES[RULE_DONE].format(folder=folder))
    return photos


async def _via_laptop(
    conn: AsyncConnection[Any],
    invoice_id: int,
    _msg: InboundMessage,
    _press: ButtonPress,
    _laptop_flows: frozenset[str],
) -> tuple[Path, ...]:
    """Enqueue the laptop resend; its job sends and answers (R-7.14, D109).

    The press runs in intake's transaction, so no file, drive or mail call may happen
    here: the job does that work after the commit.
    """
    await enqueue(conn, invoice_id, RESEND_LAPTOP_STEP)
    return ()


async def _self_send(
    conn: AsyncConnection[Any],
    invoice_id: int,
    _msg: InboundMessage,
    _press: ButtonPress,
    _laptop_flows: frozenset[str],
) -> tuple[Path, ...]:
    """Enqueue the .eml resend; its job rewrites the file and answers (R-7.8).

    Like [Por la laptop], the press only enqueues so all I/O happens after the commit.
    """
    await enqueue(conn, invoice_id, RESEND_EML_STEP)
    return ()


async def _still_asks(conn: AsyncConnection[Any], invoice_id: int, msg: InboundMessage, rule_id: str) -> bool:
    """Return whether the invoice still waits on rule_id's question (BR-S02).

    A button from another question is stale: it is answered BR-S02 and changes nothing.
    """
    _, current_rule = await _invoice_state(conn, invoice_id)
    if current_rule == rule_id:
        return True
    await send_stale(conn, msg, invoice_id)
    return False


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


async def send_not_confirmed(
    conn: AsyncConnection[Any], invoice_id: int, laptop_flows: frozenset[str] | None = None
) -> None:
    """Queue BR-D11 with D109's buttons for the invoice's sender (R-7.8, BR-D11, D109).

    The one BR-D11 sender: [No llegó], an error receipt and an overdue receipt all
    call it. [Por la laptop] is offered only when the invoice's flow lists
    ``laptop_outlook``, named by ``laptop_flows``. A ``None`` set keeps the older
    ``mail_jobs`` rule for callers not yet threaded with the set (D109).
    """
    head = await load_invoice_head(conn, invoice_id)
    reading = await load_reading(conn, invoice_id)
    if head is None or reading is None:
        raise RuntimeError(f"invoice {invoice_id} cannot be asked about its mail")
    buttons: list[Button] = []
    if laptop_flows is None:
        # shortcut: 45c removes this rule once every caller passes the flow set (D109).
        offers_laptop = await has_mail_job(conn, invoice_id)
    else:
        offers_laptop = await load_flow(conn, invoice_id) in laptop_flows
    if offers_laptop:
        buttons.append(Button(id=button_id(invoice_id, VIA_LAPTOP_ACTION), title=BUTTON_VIA_LAPTOP))
    buttons.append(Button(id=button_id(invoice_id, SELF_SEND_ACTION), title=BUTTON_SELF_SEND))
    buttons.append(Button(id=button_id(invoice_id, ARRIVED_ACTION), title=BUTTON_ARRIVED))
    text = MESSAGES[RULE_NOT_CONFIRMED].format(
        serie=reading.value("serie"),
        numero=reading.value("numero"),
    )
    await queue_message(
        conn,
        Addressee(head.origin_channel, head.profile_id),
        text,
        invoice_id=invoice_id,
        rule_id=RULE_NOT_CONFIRMED,
        buttons=buttons,
    )


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
    NOT_ARRIVED_ACTION: _not_arrived,
    ARRIVED_ACTION: _arrived,
    VIA_LAPTOP_ACTION: _via_laptop,
    SELF_SEND_ACTION: _self_send,
}
