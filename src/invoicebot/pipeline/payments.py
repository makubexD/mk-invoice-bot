"""The ``/pagado`` command: propose the invoices a reimbursement covers (R-7.12, D107).

An amount form lists the sets of the sender's sent, unpaid invoices that add up to it:
one set is BR-P01, several are BR-P02 with one button per set, and none is BR-P03 with
the pending invoices. The ``#id`` form checks that the named invoices are candidates and
sums their claimed amounts instead. Every answer goes through the outbox, in the
caller's transaction.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from psycopg import AsyncConnection

from invoicebot.channels.base import Button, InboundMessage
from invoicebot.pipeline.outgoing import Addressee, queue_message, queue_stale
from invoicebot.pipeline.payment_match import (
    Candidate,
    PagadoAmount,
    PagadoIds,
    match_sets,
    parse_pagado,
)
from invoicebot.pipeline.steps import FOLDER_STATUS_STEP
from invoicebot.queue.jobs import enqueue
from invoicebot.rules.messages_es import (
    BUTTON_NO,
    BUTTON_OPTION,
    BUTTON_YES,
    HELP_PAGADO,
    MESSAGES,
    PAYMENT_LINE,
    PAYMENT_SET_LINE,
)
from invoicebot.states import DONE
from invoicebot.store.payments import (
    CONFIRMED,
    EXPIRED,
    PENDING,
    REJECTED,
    AlreadyPaid,
    Proposal,
    add_proposal,
    candidates,
    close_proposal,
    get_proposal,
    record_payment,
)

if TYPE_CHECKING:
    from invoicebot.pipeline.commands import CommandContext, PaymentFlow

#: BR-P01: exactly one set adds up to the amount.
_RULE_PAY = "BR-P01"
#: BR-P02: several sets add up, so the sender picks one.
_RULE_CHOOSE = "BR-P02"
#: BR-P03: no set adds up to the amount, so the sender lists the invoices.
_RULE_NO_MATCH = "BR-P03"
#: BR-P04: the payment was recorded and its invoices were marked as paid.
_RULE_PAID = "BR-P04"
#: How long a payment proposal stays confirmable, on the database clock (R-7.12).
PROPOSAL_MAX_AGE_HOURS = 24
#: The prefix of every payment proposal button id, so intake can route a press (R-7.12).
PAYMENT_BUTTON_PREFIX = "p"
#: A proposal button's id: ``p<proposal>:pay:<set>`` or ``p<proposal>:pay_no`` (R-7.12).
_PRESS_PATTERN = rf"{re.escape(PAYMENT_BUTTON_PREFIX)}([0-9]{{1,9}}):(?:pay:([0-9])|pay_no)"


async def pagado(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    arg: str,
    context: CommandContext,
) -> None:
    """Answer ``/pagado``: propose the invoices a payment covers, or list what is pending (R-7.12)."""
    flow = context.payment_flow
    if flow is None:
        await _reply(conn, msg, None, HELP_PAGADO)
        return
    parsed = parse_pagado(arg, await _today(conn, context.timezone))
    if parsed is None:
        await _reply(conn, msg, None, HELP_PAGADO)
        return
    found = await candidates(conn, msg.profile_id, flow.flow_id, flow.cap)
    if isinstance(parsed, PagadoAmount):
        await _answer_amount(conn, msg, flow, parsed, found)
    else:
        await _answer_ids(conn, msg, flow, parsed, found)


def _parse_press(button_id: str) -> tuple[int, int | None] | None:
    """Parse a proposal button id into its proposal and set index, or None (R-7.12).

    ``p<id>:pay_no`` returns a ``None`` set index; a malformed id returns ``None``.
    """
    match = re.fullmatch(_PRESS_PATTERN, button_id)
    if match is None:
        return None
    set_index = match.group(2)
    return int(match.group(1)), None if set_index is None else int(set_index)


async def handle_payment_press(conn: AsyncConnection[Any], msg: InboundMessage) -> None:
    """Confirm or reject the payment a proposal button names (R-7.12).

    A malformed id, a missing proposal or another profile's proposal changes nothing
    and answers nothing, so one profile's invoice ids never reach another. A stale
    proposal answers BR-S02; a pending one older than :data:`PROPOSAL_MAX_AGE_HOURS`
    is also closed as expired. [No] closes it as rejected. [Sí] records the payment
    and one ``folder_status`` job per invoice, closes it as confirmed and answers
    BR-P04, all in the caller's transaction; an invoice paid in the meantime rolls
    the savepoint back, answers BR-S02 and leaves the proposal pending.
    """
    parsed = None if msg.button_id is None else _parse_press(msg.button_id)
    if parsed is None:
        return
    proposal_id, set_index = parsed
    proposal = await get_proposal(conn, proposal_id)
    if proposal is None or proposal.profile_id != msg.profile_id:
        return
    if proposal.state != PENDING:
        await _answer_stale(conn, msg, proposal)
        return
    if not await _is_fresh(conn, proposal.created_at):
        await close_proposal(conn, proposal.id, EXPIRED)
        await _answer_stale(conn, msg, proposal)
        return
    if set_index is None:
        await close_proposal(conn, proposal_id, REJECTED)
        return
    if set_index >= len(proposal.sets):
        await _answer_stale(conn, msg, proposal)
        return
    await _confirm(conn, msg, proposal, proposal.sets[set_index])


async def _is_fresh(conn: AsyncConnection[Any], created_at: datetime) -> bool:
    """Return whether a proposal is under :data:`PROPOSAL_MAX_AGE_HOURS` old (R-7.12)."""
    cur = await conn.execute(
        "SELECT %s > now() - make_interval(hours => %s)",
        (created_at, PROPOSAL_MAX_AGE_HOURS),
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("SELECT now() - make_interval yielded no row")
    return bool(row[0])


async def _confirm(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    proposal: Proposal,
    invoice_ids: Sequence[int],
) -> None:
    """Record one set's payment with its jobs and close the proposal (R-7.12)."""
    try:
        async with conn.transaction():
            await record_payment(
                conn,
                proposal.flow_id,
                proposal.profile_id,
                proposal.paid_on,
                proposal.amount,
                invoice_ids,
            )
            for invoice_id in invoice_ids:
                await enqueue(conn, invoice_id, FOLDER_STATUS_STEP)
            await close_proposal(conn, proposal.id, CONFIRMED)
    except AlreadyPaid:
        await _answer_stale(conn, msg, proposal)
        return
    await queue_message(
        conn,
        Addressee(msg.channel, msg.profile_id),
        MESSAGES[_RULE_PAID].format(n=len(invoice_ids)),
        rule_id=_RULE_PAID,
    )


async def _answer_stale(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    proposal: Proposal,
) -> None:
    """Queue BR-S02 naming the proposal's first invoice (R-7.12)."""
    await queue_stale(
        conn,
        Addressee(msg.channel, msg.profile_id),
        invoice_id=proposal.sets[0][0],
        state=DONE,
    )


async def _today(conn: AsyncConnection[Any], timezone: str) -> date:
    """Return today's date on the database clock, in the owner's timezone (R-7.12)."""
    cur = await conn.execute("SELECT (now() AT TIME ZONE %s)::date", (timezone,))
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("SELECT now() AT TIME ZONE yielded no row")
    return cast(date, row[0])


async def _answer_amount(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    flow: PaymentFlow,
    parsed: PagadoAmount,
    found: list[Candidate],
) -> None:
    """Propose every set adding up to the amount, or list the pending invoices (R-7.12)."""
    sets = await asyncio.to_thread(match_sets, found, int(parsed.amount * 100))
    if not sets:
        text = _pending_text(parsed.paid_on, parsed.amount, found)
        await _reply(conn, msg, _RULE_NO_MATCH, text)
        return
    proposal_id = await add_proposal(conn, msg.profile_id, flow.flow_id, parsed.paid_on, parsed.amount, sets)
    if len(sets) == 1:
        text = _br_p01_text(parsed.paid_on, parsed.amount, sets[0])
        await _reply(conn, msg, _RULE_PAY, text, _yes_buttons(proposal_id))
        return
    text = _br_p02_text(parsed.amount, sets, found)
    await _reply(conn, msg, _RULE_CHOOSE, text, _set_buttons(proposal_id, sets))


async def _answer_ids(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    flow: PaymentFlow,
    parsed: PagadoIds,
    found: list[Candidate],
) -> None:
    """Propose the named candidates and their sum, or list what is pending (R-7.12)."""
    by_id = {candidate.invoice_id: candidate for candidate in found}
    named = [by_id[invoice_id] for invoice_id in parsed.invoice_ids if invoice_id in by_id]
    amount = _cents_amount(sum(candidate.claimed_cents for candidate in named))
    if len(named) != len(parsed.invoice_ids):
        text = _pending_text(parsed.paid_on, amount, found)
        await _reply(conn, msg, _RULE_NO_MATCH, text)
        return
    proposal_id = await add_proposal(
        conn, msg.profile_id, flow.flow_id, parsed.paid_on, amount, [parsed.invoice_ids]
    )
    text = _br_p01_text(parsed.paid_on, amount, parsed.invoice_ids)
    await _reply(conn, msg, _RULE_PAY, text, _yes_buttons(proposal_id))


def _br_p01_text(paid_on: date, amount: Decimal, invoice_ids: Sequence[int]) -> str:
    """Return BR-P01's text for one set of invoices (R-7.12)."""
    return MESSAGES[_RULE_PAY].format(
        monto=_money(amount),
        fecha=_day_month(paid_on),
        n=len(invoice_ids),
        lista=_id_list(invoice_ids),
    )


def _pending_text(
    paid_on: date,
    amount: Decimal,
    found: Sequence[Candidate],
) -> str:
    """Return BR-P03's text listing every candidate invoice (R-7.12)."""
    lines = "\n".join(_pending_line(candidate) for candidate in found)
    return MESSAGES[_RULE_NO_MATCH].format(
        monto=_money(amount),
        fecha=_day_month(paid_on),
        invoice_lines=lines,
    )


def _pending_line(candidate: Candidate) -> str:
    """Return one BR-P03 invoice line: its id, invoice date and claimed amount (R-7.12)."""
    return PAYMENT_LINE.format(
        id=candidate.invoice_id,
        fecha=_day_month(candidate.fecha),
        monto=_money(_cents_amount(candidate.claimed_cents)),
    )


def _yes_buttons(proposal_id: int) -> list[Button]:
    """Return BR-P01's [Sí] and [No] buttons for a proposal (R-7.12)."""
    return [
        Button(id=f"{PAYMENT_BUTTON_PREFIX}{proposal_id}:pay:0", title=BUTTON_YES),
        Button(id=f"{PAYMENT_BUTTON_PREFIX}{proposal_id}:pay_no", title=BUTTON_NO),
    ]


def _br_p02_text(
    amount: Decimal,
    sets: Sequence[tuple[int, ...]],
    found: Sequence[Candidate],
) -> str:
    """Return BR-P02's text listing every matching set, oldest first (R-7.12, D107)."""
    by_id = {candidate.invoice_id: candidate for candidate in found}
    set_lines = "\n".join(_set_line(number, ids, by_id) for number, ids in enumerate(sets, start=1))
    return MESSAGES[_RULE_CHOOSE].format(monto=_money(amount), set_lines=set_lines)


def _set_buttons(proposal_id: int, sets: Sequence[tuple[int, ...]]) -> list[Button]:
    """Return BR-P02's one button per set plus [No], oldest set first (R-7.12, D107)."""
    buttons = [
        Button(
            id=f"{PAYMENT_BUTTON_PREFIX}{proposal_id}:pay:{index}",
            title=BUTTON_OPTION.format(n=index + 1),
        )
        for index in range(len(sets))
    ]
    buttons.append(Button(id=f"{PAYMENT_BUTTON_PREFIX}{proposal_id}:pay_no", title=BUTTON_NO))
    return buttons


def _set_line(
    number: int,
    ids: Sequence[int],
    by_id: Mapping[int, Candidate],
) -> str:
    """Return one BR-P02 set line numbered from 1, dated by its oldest invoice (R-7.12, D107)."""
    oldest = min(by_id[invoice_id].fecha for invoice_id in ids)
    return PAYMENT_SET_LINE.format(
        n=number,
        fecha_min=_day_month(oldest),
        count=len(ids),
        ids=_id_list(ids),
    )


def _day_month(value: date) -> str:
    """Return a date as ``dd/mm``, the format the payment texts use (R-7.12)."""
    return value.strftime("%d/%m")


def _id_list(invoice_ids: Sequence[int]) -> str:
    """Return invoice ids as ``#12 #14``, the format the payment texts use (R-7.12)."""
    return " ".join(f"#{invoice_id}" for invoice_id in invoice_ids)


def _money(amount: Decimal) -> str:
    """Return an amount as ``x.xx``, the format the payment texts use (R-7.12)."""
    return f"{amount:.2f}"


def _cents_amount(cents: int) -> Decimal:
    """Return whole cents as an amount of soles (R-7.12)."""
    return Decimal(cents) / Decimal(100)


async def _reply(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    rule_id: str | None,
    text: str,
    buttons: Sequence[Button] = (),
) -> None:
    """Queue ``text`` and its buttons for the command's sender (R-7.12)."""
    await queue_message(
        conn,
        Addressee(msg.channel, msg.profile_id),
        text,
        rule_id=rule_id,
        buttons=buttons,
    )
