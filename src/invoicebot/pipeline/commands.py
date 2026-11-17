"""Chat commands: parse them, answer ``/ayuda`` and resolve the invoice they name (R-10).

A text starting with ``/`` is a command, handled before edits and overrides. A command
acts only on the sender profile's own invoices unless the sender is an admin; an id that
does not exist and one belonging to another profile get the same BR-S07 answer, so ids
cannot be probed (R-10.1). Everything here happens in the caller's transaction.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from psycopg import AsyncConnection

from invoicebot.channels.base import InboundMessage
from invoicebot.pipeline.actions import cancel_invoice
from invoicebot.pipeline.deliver import DELIVER_STEP, FAILED_BATCH_STATE, PREPARED_BATCH_STATE
from invoicebot.pipeline.fetch import FETCH_STEP
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.payments import pagado
from invoicebot.pipeline.teach import TeachContext, TeachSetup, teach
from invoicebot.queue import outbox
from invoicebot.queue.jobs import enqueue
from invoicebot.rules.messages_es import (
    CANCEL_LATE,
    COMMAND_NEEDS_ID,
    GAP_IGNORED,
    GAP_UNKNOWN,
    GAPS_EMPTY,
    GAPS_HEADER,
    GAPS_ISSUE,
    GAPS_LINE,
    HELP_AYUDA,
    HELP_CANCELAR,
    HELP_ESTADO,
    HELP_GAP,
    HELP_GAPS,
    HELP_HEADER,
    HELP_PAGADO,
    HELP_PENDIENTES,
    HELP_PORTAL,
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
from invoicebot.store.gaps import GapRow, ignore_gap, open_gaps
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
#: A RUC's exact number of digits (R-11.4).
RUC_DIGITS = 11
#: How long a prepared batch waits before ``/pendientes`` mentions it (S11.4).
PREPARED_REMIND_DAYS = 2
#: The most lines ``/pendientes`` lists before it counts the rest (S11.4).
MAX_PENDING_LINES = 20
#: How many parked questions ``/pendientes`` sends again in one answer (S4.8).
PENDING_REQUEUES = 3
#: The least time between two sends of the same parked question (S4.8, D94).
PENDING_REQUEUE_INTERVAL = timedelta(minutes=10)
#: The most open gaps ``/gaps`` lists before it says there are more (S8.9, D139).
MAX_GAP_LINES = 20
#: The last line of ``/gaps`` when exactly :data:`MAX_GAP_LINES` gaps came back (S8.9, D139).
GAPS_TRUNCATED = "…"
#: The ``/gap`` argument that ignores a gap (S8.9, D139).
GAP_IGNORE_ARG = "ignorar"


@dataclass(frozen=True)
class PaymentFlow:
    """The flow whose payments a sender may record (R-7.12): its id and reimbursement cap."""

    flow_id: str
    cap: Decimal | None


@dataclass(frozen=True)
class CommandContext:
    """What a command needs beyond its message: admin ids, the timezone, the payment flow and teaching."""

    admins: frozenset[str]
    timezone: str
    payment_flow: PaymentFlow | None = None
    teach: TeachSetup | None = None


#: A command applied in the caller's transaction: its connection, sender, argument and context.
type Handler = Callable[[AsyncConnection[Any], InboundMessage, str, CommandContext], Awaitable[None]]


@dataclass(frozen=True)
class Command:
    """One chat command: the handler it runs, its help line and who may run it."""

    handler: Handler
    help: str
    admin_only: bool = False


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
    context: CommandContext,
) -> None:
    """Run the named command in the caller's transaction.

    An unknown name, or an admin command from a sender who is not an admin, answers
    the help exactly alike (R-10.1).
    """
    is_admin = msg.profile_id in context.admins
    command = COMMANDS.get(name)
    if command is None or (command.admin_only and not is_admin):
        await _reply(conn, msg, None, help_text(is_admin))
        return
    await command.handler(conn, msg, arg, context)


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


def help_text(is_admin: bool) -> str:
    """Return the help: the header followed by each command's line, in command order.

    An admin command's line is listed only when ``is_admin`` is true, so a sender who
    cannot run it does not learn it exists (R-10.1).
    """
    lines = [command.help for command in COMMANDS.values() if is_admin or not command.admin_only]
    return "\n".join([HELP_HEADER, *lines])


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
    context: CommandContext,
) -> None:
    """Answer with every command the sender may run and what it does (R-10.1)."""
    await _reply(conn, msg, None, help_text(msg.profile_id in context.admins))


async def _estado(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    arg: str,
    context: CommandContext,
) -> None:
    """Answer with the invoice's state and, when it waits on a rule, that rule's last text.

    A target the sender cannot see is refused by :func:`resolve_target` (R-10.1). The
    second line names the rule and repeats its newest stored question, or just the rule
    when the invoice has no stored text (S4.9, R-11.1).
    """
    target = await resolve_target(conn, msg, "estado", arg, context.admins)
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
    context: CommandContext,
) -> None:
    """Cancel the named invoice when its state is CANCELLABLE, or explain why not (S4.9, D96)."""
    target = await resolve_target(conn, msg, "cancelar", arg, context.admins)
    if target is None:
        return
    invoice_id, head = target
    if head.state in CANCELLABLE:
        await cancel_invoice(conn, invoice_id, msg, expected=head.state)
        return
    text = CANCEL_LATE.format(id=invoice_id, state_desc=state_desc(head.state))
    await _reply(conn, msg, None, text)


async def _reprocesar(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    arg: str,
    context: CommandContext,
) -> None:
    """Restart the named invoice's fetch or failed delivery, or explain why it cannot (S4.9, D96)."""
    target = await resolve_target(conn, msg, "reprocesar", arg, context.admins)
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
    context: CommandContext,
) -> None:
    """List what waits on the sender and send their parked questions again (S4.8, S4.9, S11.4).

    Only the sender's own invoices are listed; a parked row shows the state it was
    waiting in (D94). The first :data:`PENDING_REQUEUES` parked invoices get their
    question sent again with the same buttons, so a lapsed question comes back, but
    one sent within :data:`PENDING_REQUEUE_INTERVAL` is not repeated (S4.8).
    """
    rows, total = await list_pending(
        conn,
        msg.profile_id,
        limit=MAX_PENDING_LINES,
        remind_days=PREPARED_REMIND_DAYS,
        prepared_batch_state=PREPARED_BATCH_STATE,
        timezone=context.timezone,
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
        await outbox.requeue_last(conn, row.id, row.waiting_rule_id, PENDING_REQUEUE_INTERVAL)
        requeued += 1


async def _portal(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    arg: str,
    context: CommandContext,
) -> None:
    """Propose the named issuer's portal link and ask the owner to approve it (R-11.4).

    A malformed argument answers the usage line; when teaching is not wired, the
    command is unknown and answers the help (R-10.1).
    """
    if context.teach is None:
        await _reply(conn, msg, None, help_text(msg.profile_id in context.admins))
        return
    parts = arg.split()
    if len(parts) != 2 or not _is_ruc(parts[0]):
        await _reply(conn, msg, None, HELP_PORTAL)
        return
    ruc, url = parts
    await teach(
        conn,
        TeachContext(
            profiles=context.teach.profiles,
            sender=Addressee(msg.channel, msg.profile_id),
            resolver=context.teach.resolver,
        ),
        int(ruc),
        None,
        url,
    )


def _is_ruc(token: str) -> bool:
    """Return whether ``token`` is exactly :data:`RUC_DIGITS` ASCII digits (R-11.4)."""
    return token.isascii() and token.isdigit() and len(token) == RUC_DIGITS


async def _gaps(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    _arg: str,
    _context: CommandContext,
) -> None:
    """List the open or reported gaps, most recently seen first (S8.9, D139)."""
    rows = await open_gaps(conn, MAX_GAP_LINES)
    if not rows:
        await _reply(conn, msg, None, GAPS_EMPTY)
        return
    lines = [GAPS_HEADER, *(_gap_line(row) for row in rows)]
    if len(rows) == MAX_GAP_LINES:
        lines.append(GAPS_TRUNCATED)
    await _reply(conn, msg, None, "\n".join(lines))


def _gap_line(row: GapRow) -> str:
    """Return the line ``/gaps`` lists for one gap, with its issue link when it has one (S8.9)."""
    issue = "" if row.issue_url is None else GAPS_ISSUE.format(issue_url=row.issue_url)
    return GAPS_LINE.format(
        fingerprint=row.fingerprint,
        kind=row.kind,
        subject=row.subject,
        count=row.count,
        issue=issue,
    )


async def _gap(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    arg: str,
    _context: CommandContext,
) -> None:
    """Stop reporting the gap ``/gap ignorar <fingerprint>`` names (S8.9, D139).

    Any other argument answers the usage line.
    """
    parts = arg.split()
    if len(parts) != 2 or parts[0] != GAP_IGNORE_ARG:
        await _reply(conn, msg, None, HELP_GAP)
        return
    fingerprint = parts[1]
    text = GAP_IGNORED if await ignore_gap(conn, fingerprint) else GAP_UNKNOWN
    await _reply(conn, msg, None, text.format(fingerprint=fingerprint))


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
    "pagado": Command(pagado, HELP_PAGADO),
    "portal": Command(_portal, HELP_PORTAL),
    "gaps": Command(_gaps, HELP_GAPS, admin_only=True),
    "gap": Command(_gap, HELP_GAP, admin_only=True),
}
