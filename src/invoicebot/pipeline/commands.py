"""Chat commands: parse them, answer ``/ayuda`` and resolve the invoice they name (R-10).

A text starting with ``/`` is a command, handled before edits and overrides. A command
acts only on the sender profile's own invoices unless the sender is an admin; an id that
does not exist and one belonging to another profile get the same BR-S07 answer, so ids
cannot be probed (R-10.1). Everything here happens in the caller's transaction.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection

from invoicebot.channels.base import InboundMessage
from invoicebot.pipeline.actions import cancel_invoice
from invoicebot.pipeline.deliver import DELIVER_STEP, FAILED_BATCH_STATE, PREPARED_BATCH_STATE
from invoicebot.pipeline.fetch import FETCH_STEP
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.queue import outbox
from invoicebot.queue.jobs import enqueue
from invoicebot.rules.messages_es import (
    CANCEL_LATE,
    COMMAND_NEEDS_ID,
    HELP_AYUDA,
    HELP_CANCELAR,
    HELP_ESTADO,
    HELP_HEADER,
    HELP_PENDIENTES,
    HELP_REPROCESAR,
    MESSAGES,
    PENDING_BATCH_LINE,
    PENDING_HEADER,
    PENDING_LINE,
    PENDING_MORE,
    PENDING_NO_ISSUER,
    PENDING_NONE,
    REPROCESS_NOT_ALLOWED,
    REPROCESS_STARTED,
    STATUS,
    STATUS_RULE,
    STATUS_RULE_ONLY,
    state_desc,
)
from invoicebot.states import (
    BATCHED,
    CANCELLABLE,
    DELIVERING,
    FAILED,
    FETCHING_DOCS,
    MANUAL_REQUIRED,
    PENDING_USER,
)
from invoicebot.store.flow_b import get_batch_state
from invoicebot.store.invoices import (
    InvoiceHead,
    PendingRow,
    get_state_rule,
    list_pending,
    load_flow,
    load_invoice_head,
    set_state,
)

#: Rule that answers an id the sender cannot see, whether it exists or not (BR-S07).
RULE_NOT_FOUND = "BR-S07"
#: The most digits an invoice id may have; any 18-digit number fits a BIGINT (R-12.8).
MAX_ID_DIGITS = 18
#: How long a prepared batch waits before ``/pendientes`` mentions it (S11.4).
PREPARED_REMIND_DAYS = 2
#: The most lines ``/pendientes`` lists before it counts the rest (S11.4).
MAX_PENDING_LINES = 20
#: How many parked questions ``/pendientes`` sends again in one answer (S4.8).
PENDING_REQUEUES = 3

#: A command applied in the caller's transaction: its connection, sender, argument and
#: the admin profile ids (R-10.1).
type Handler = Callable[[AsyncConnection[Any], InboundMessage, str, frozenset[str]], Awaitable[None]]


@dataclass(frozen=True)
class Command:
    """One chat command: the handler it runs and the help line that describes it."""

    handler: Handler
    help: str


def parse_command(text: str) -> tuple[str, str] | None:
    """Return a leading ``/name`` and its argument, or None when the text is not a command.

    The name is lower-cased, so ``/AYUDA`` is ``/ayuda``; the argument is the rest of the
    text, stripped. A bare ``/`` names nothing and is not a command.
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped[1:].split(maxsplit=1)
    if not parts:
        return None
    return parts[0].lower(), parts[1].strip() if len(parts) == 2 else ""


async def handle_command(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    name: str,
    arg: str,
    *,
    admins: frozenset[str],
) -> None:
    """Run the named command in the caller's transaction; an unknown name answers the help."""
    command = COMMANDS.get(name)
    if command is None:
        await _reply(conn, msg, None, help_text())
        return
    await command.handler(conn, msg, arg, admins)


async def resolve_target(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    command: str,
    arg: str,
    admins: frozenset[str],
) -> tuple[int, InvoiceHead] | None:
    """Resolve the invoice a command names, queueing its refusal and returning None.

    ``arg`` is ``12`` or ``#12``; anything else asks for the id. An invoice that does not
    exist, or belongs to another profile while the sender is not an admin, gets the same
    BR-S07 answer (R-10.1).
    """
    invoice_id = _invoice_id(arg)
    if invoice_id is None:
        await _reply(conn, msg, None, COMMAND_NEEDS_ID.format(command=command))
        return None
    head = await load_invoice_head(conn, invoice_id)
    if head is None or (head.profile_id != msg.profile_id and msg.profile_id not in admins):
        await _reply(conn, msg, RULE_NOT_FOUND, MESSAGES[RULE_NOT_FOUND].format(id=invoice_id))
        return None
    return invoice_id, head


def help_text() -> str:
    """Return the help: the header followed by each command's line, in command order."""
    return "\n".join([HELP_HEADER, *(command.help for command in COMMANDS.values())])


def _invoice_id(arg: str) -> int | None:
    """Return the invoice id in ``arg`` (``12`` or ``#12``), or None when it is not one."""
    digits = arg.removeprefix("#")
    if not (digits.isascii() and digits.isdigit()) or len(digits) > MAX_ID_DIGITS:
        return None
    return int(digits)


async def _ayuda(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    _arg: str,
    _admins: frozenset[str],
) -> None:
    """Answer with every command and what it does (R-10.1)."""
    await _reply(conn, msg, None, help_text())


async def _estado(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    arg: str,
    admins: frozenset[str],
) -> None:
    """Answer with the invoice's state and, when it waits on a rule, that rule's last text.

    A target the sender cannot see is refused by :func:`resolve_target` (R-10.1). The
    second line names the rule and repeats its newest stored question, or just the rule
    when the invoice has no stored text (S4.9, R-11.1).
    """
    target = await resolve_target(conn, msg, "estado", arg, admins)
    if target is None:
        return
    invoice_id, _head = target
    current = await get_state_rule(conn, invoice_id)
    if current is None:
        raise RuntimeError(f"invoice {invoice_id} does not exist")
    state, rule_id = current
    text = STATUS.format(id=invoice_id, state_desc=state_desc(state))
    if rule_id is not None:
        last = await outbox.last_text(conn, invoice_id, rule_id)
        line = (
            STATUS_RULE.format(rule_id=rule_id, text=last)
            if last is not None
            else STATUS_RULE_ONLY.format(rule_id=rule_id)
        )
        text = f"{text}\n{line}"
    await _reply(conn, msg, None, text)


async def _cancelar(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    arg: str,
    admins: frozenset[str],
) -> None:
    """Cancel the named invoice when its state is CANCELLABLE, or explain why not (S4.9, D96)."""
    target = await resolve_target(conn, msg, "cancelar", arg, admins)
    if target is None:
        return
    invoice_id, head = target
    if head.state in CANCELLABLE:
        await cancel_invoice(conn, invoice_id, msg)
        return
    text = CANCEL_LATE.format(id=invoice_id, state_desc=state_desc(head.state))
    await _reply(conn, msg, None, text)


async def _reprocesar(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    arg: str,
    admins: frozenset[str],
) -> None:
    """Restart the named invoice's fetch or failed delivery, or explain why it cannot (S4.9, D96)."""
    target = await resolve_target(conn, msg, "reprocesar", arg, admins)
    if target is None:
        return
    invoice_id, head = target
    if await _requeue(conn, invoice_id, head.state):
        await _reply(conn, msg, None, REPROCESS_STARTED.format(id=invoice_id))
        return
    text = REPROCESS_NOT_ALLOWED.format(id=invoice_id, state_desc=state_desc(head.state))
    await _reply(conn, msg, None, text)


async def _requeue(conn: AsyncConnection[Any], invoice_id: int, state: str) -> bool:
    """Queue the step the invoice's state needs, returning whether there is one (S4.9, D96)."""
    if state in (FAILED, MANUAL_REQUIRED):
        return await _retry_documents(conn, invoice_id, state)
    if state == BATCHED:
        return await _retry_delivery(conn, invoice_id)
    return False


async def _retry_documents(conn: AsyncConnection[Any], invoice_id: int, state: str) -> bool:
    """Send a failed or manual invoice back to its fetch step, if it has a flow (S4.9, D96)."""
    if state == FAILED and await load_flow(conn, invoice_id) is None:
        return False
    await set_state(conn, invoice_id, FETCHING_DOCS, expected=state)
    await enqueue(conn, invoice_id, FETCH_STEP)
    return True


async def _retry_delivery(conn: AsyncConnection[Any], invoice_id: int) -> bool:
    """Send an invoice whose batch failed back to deliver, which resends it (S4.9, D96)."""
    if await get_batch_state(conn, invoice_id) != FAILED_BATCH_STATE:
        return False
    await set_state(conn, invoice_id, DELIVERING, expected=BATCHED)
    await enqueue(conn, invoice_id, DELIVER_STEP)
    return True


async def _pendientes(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    _arg: str,
    _admins: frozenset[str],
) -> None:
    """List what waits on the sender and send their parked questions again (S4.8, S4.9, S11.4).

    Only the sender's own invoices are listed; a parked row shows the state it was
    waiting in (D94). The first :data:`PENDING_REQUEUES` parked invoices get their
    question sent again with the same buttons, so a lapsed question comes back.
    """
    rows, total = await list_pending(
        conn,
        msg.profile_id,
        limit=MAX_PENDING_LINES,
        remind_days=PREPARED_REMIND_DAYS,
        prepared_batch_state=PREPARED_BATCH_STATE,
    )
    if not rows:
        await _reply(conn, msg, None, PENDING_NONE)
        return
    lines = [PENDING_HEADER, *(_pending_line(row) for row in rows)]
    if total > len(rows):
        lines.append(PENDING_MORE.format(count=total - len(rows)))
    await _reply(conn, msg, None, "\n".join(lines))
    await _requeue_parked(conn, rows)


def _pending_line(row: PendingRow) -> str:
    """Return the line ``/pendientes`` lists for one invoice (S4.8, S11.4)."""
    issuer = row.razon_social or PENDING_NO_ISSUER
    if row.state == BATCHED:
        # shortcut: prepared_at is the database session's date, not the owner's timezone,
        # so late evenings in Lima show the next day; commands get no timezone yet.
        return PENDING_BATCH_LINE.format(id=row.id, issuer=issuer, prepared_at=row.batch_prepared_at)
    total = "—" if row.total is None else f"{row.total:.2f}"
    state = row.waiting_state if row.state == PENDING_USER and row.waiting_state else row.state
    return PENDING_LINE.format(
        id=row.id,
        issuer=issuer,
        total=total,
        state_desc=state_desc(state),
    )


async def _requeue_parked(conn: AsyncConnection[Any], rows: list[PendingRow]) -> None:
    """Queue the stored question again for the first few parked invoices (S4.8, D94)."""
    requeued = 0
    for row in rows:
        if requeued == PENDING_REQUEUES:
            return
        if row.state != PENDING_USER or row.waiting_rule_id is None:
            continue
        await outbox.requeue_last(conn, row.id, row.waiting_rule_id)
        requeued += 1


async def _reply(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    rule_id: str | None,
    text: str,
) -> None:
    """Queue ``text`` for the command's sender in the caller's transaction."""
    await queue_message(conn, Addressee(msg.channel, msg.profile_id), text, rule_id=rule_id)


#: The commands a text may name; the help lists them in this order.
COMMANDS: Mapping[str, Command] = {
    "ayuda": Command(_ayuda, HELP_AYUDA),
    "estado": Command(_estado, HELP_ESTADO),
    "cancelar": Command(_cancelar, HELP_CANCELAR),
    "reprocesar": Command(_reprocesar, HELP_REPROCESAR),
    "pendientes": Command(_pendientes, HELP_PENDIENTES),
}
