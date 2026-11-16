"""The ``/pagado`` command: propose the invoices a reimbursement covers (R-7.12, D107).

An amount form lists the sets of the sender's sent, unpaid invoices that add up to it:
one set is BR-P01, several are BR-P02 with one button per set, and none is BR-P03 with
the pending invoices. The ``#id`` form checks that the named invoices are candidates and
sums their claimed amounts instead. Every answer goes through the outbox, in the
caller's transaction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection

from invoicebot.channels.base import Button, InboundMessage
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.payment_match import (
    Candidate,
    PagadoAmount,
    PagadoIds,
    match_sets,
    parse_pagado,
)
from invoicebot.rules.messages_es import (
    BUTTON_NO,
    BUTTON_YES,
    HELP_PAGADO,
    MESSAGES,
    PAYMENT_LINE,
    PAYMENT_SET_LABEL,
)
from invoicebot.store.payments import add_proposal, candidates

if TYPE_CHECKING:
    from invoicebot.pipeline.commands import CommandContext, PaymentFlow

#: BR-P01: exactly one set adds up to the amount.
_RULE_PAY = "BR-P01"
#: BR-P02: several sets add up, so the sender picks one.
_RULE_CHOOSE = "BR-P02"
#: BR-P03: no set adds up to the amount, so the sender lists the invoices.
_RULE_NO_MATCH = "BR-P03"


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
        await _answer_amount(conn, msg, flow, parsed, found, context.timezone)
    else:
        await _answer_ids(conn, msg, flow, parsed, found, context.timezone)


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
    timezone: str,
) -> None:
    """Propose every set adding up to the amount, or list the pending invoices (R-7.12)."""
    sets = match_sets(found, int(parsed.amount * 100))
    if not sets:
        text = _pending_text(parsed.paid_on, parsed.amount, found, timezone)
        await _reply(conn, msg, _RULE_NO_MATCH, text)
        return
    proposal_id = await add_proposal(conn, msg.profile_id, flow.flow_id, parsed.paid_on, parsed.amount, sets)
    if len(sets) == 1:
        text = _br_p01_text(parsed.paid_on, parsed.amount, sets[0])
        await _reply(conn, msg, _RULE_PAY, text, _yes_buttons(proposal_id))
        return
    text = MESSAGES[_RULE_CHOOSE].format(monto=_money(parsed.amount))
    await _reply(conn, msg, _RULE_CHOOSE, text, _set_buttons(proposal_id, sets, found, timezone))


async def _answer_ids(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    flow: PaymentFlow,
    parsed: PagadoIds,
    found: list[Candidate],
    timezone: str,
) -> None:
    """Propose the named candidates and their sum, or list what is pending (R-7.12)."""
    by_id = {candidate.invoice_id: candidate for candidate in found}
    named = [by_id[invoice_id] for invoice_id in parsed.invoice_ids if invoice_id in by_id]
    amount = _cents_amount(sum(candidate.claimed_cents for candidate in named))
    if len(named) != len(parsed.invoice_ids):
        text = _pending_text(parsed.paid_on, amount, found, timezone)
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
    timezone: str,
) -> str:
    """Return BR-P03's text listing every candidate invoice (R-7.12)."""
    lines = "\n".join(_pending_line(candidate, timezone) for candidate in found)
    return MESSAGES[_RULE_NO_MATCH].format(
        monto=_money(amount),
        fecha=_day_month(paid_on),
        invoice_lines=lines,
    )


def _pending_line(candidate: Candidate, timezone: str) -> str:
    """Return one BR-P03 invoice line: its id, date and claimed amount (R-7.12)."""
    return PAYMENT_LINE.format(
        id=candidate.invoice_id,
        fecha=_day_month(_local_date(candidate.created_at, timezone)),
        monto=_money(_cents_amount(candidate.claimed_cents)),
    )


def _yes_buttons(proposal_id: int) -> list[Button]:
    """Return BR-P01's [Sí] and [No] buttons for a proposal (R-7.12)."""
    return [
        Button(id=f"p{proposal_id}:pay:0", title=BUTTON_YES),
        Button(id=f"p{proposal_id}:pay_no", title=BUTTON_NO),
    ]


def _set_buttons(
    proposal_id: int,
    sets: Sequence[tuple[int, ...]],
    found: Sequence[Candidate],
    timezone: str,
) -> list[Button]:
    """Return BR-P02's one button per set plus [No], oldest set first (R-7.12, D107)."""
    by_id = {candidate.invoice_id: candidate for candidate in found}
    buttons = [
        Button(id=f"p{proposal_id}:pay:{index}", title=_set_label(ids, by_id, timezone))
        for index, ids in enumerate(sets)
    ]
    buttons.append(Button(id=f"p{proposal_id}:pay_no", title=BUTTON_NO))
    return buttons


def _set_label(ids: Sequence[int], by_id: Mapping[int, Candidate], timezone: str) -> str:
    """Return BR-P02's label for one set, dated by its oldest invoice (D107)."""
    oldest = min(by_id[invoice_id].created_at for invoice_id in ids)
    return PAYMENT_SET_LABEL.format(
        fecha_min=_day_month(_local_date(oldest, timezone)),
        n=len(ids),
        ids=_id_list(ids),
    )


def _local_date(moment: datetime, timezone: str) -> date:
    """Return a stored timestamp's date in the owner's timezone (R-7.12)."""
    return moment.astimezone(ZoneInfo(timezone)).date()


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
