"""Intake: persist one inbound message, acknowledge it and queue its invoice.

This module owns the first pipeline step only (R-1.1, R-1.8, BR-S01). It makes
no network calls and never commits: everything happens in the caller's
transaction, so a later failure leaves no message, invoice, job or outbox row.
The photo files a button press releases are deleted after that transaction (R-9.7).
"""

import asyncio
from dataclasses import dataclass
from typing import Any, Literal

from psycopg import AsyncConnection

from invoicebot.channels.base import InboundMessage
from invoicebot.pipeline.actions import handle_press
from invoicebot.pipeline.commands import CommandContext, PaymentFlow, handle_command, parse_command
from invoicebot.pipeline.edits import apply_edits, parse_edits
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.overrides import apply_overrides, parse_overrides
from invoicebot.pipeline.payments import handle_payment_press
from invoicebot.pipeline.steps import ATTACH_STEP
from invoicebot.queue.jobs import enqueue
from invoicebot.rules.messages_es import MESSAGES
from invoicebot.store.flow_b import merge_flow_data
from invoicebot.store.invoices import count_active_jobs, create_invoice, touch_window
from invoicebot.store.messages import (
    find_open_invoice_for_attachment,
    link_message_to_invoice,
    list_open_invoices,
    store_inbound,
)

#: Rule that tells a sender their queue is full (BR-S01).
RULE_BACK_PRESSURE = "BR-S01"
#: Rule that acknowledges a queued invoice (BR-S09).
RULE_ACK = "BR-S09"
#: Rule that asks which open invoice an ambiguous upload belongs to (BR-D14).
RULE_AMBIGUOUS_UPLOAD = "BR-D14"
#: Job step that downloads a photo before extraction (R-1.4).
DOWNLOAD_STEP = "download_media"
#: Advisory-lock namespace that serialises one profile's intake (R-1.6, PT-2).
LOCK_PREFIX = "intake:"
#: Attachment kind that starts an invoice (R-1.1).
PHOTO_KIND = "photo"
#: Attachment kinds a sender may upload for a stuck invoice (R-1.5, D101).
UPLOAD_KINDS = ("pdf", "xml")

type IntakeStatus = Literal["duplicate", "queued", "back_pressure", "stored"]


@dataclass(frozen=True)
class IntakeResult:
    """What intake did with one inbound message."""

    status: IntakeStatus
    invoice_id: int | None


async def handle_inbound(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    *,
    max_queued_per_sender: int,
    timezone: str,
    admins: frozenset[str] = frozenset(),
    payment_flow: PaymentFlow | None = None,
) -> IntakeResult:
    """Handle one inbound message inside the caller's transaction.

    A redelivered message is a ``duplicate`` (R-1.8). Every other message
    refreshes its channel window (R-1.10); a button press runs its action and is
    ``stored`` (R-3.5), except a payment proposal press, which is answered as stored
    without an invoice (R-7.12), a non-photo is ``stored`` (a pdf/xml upload is linked to
    the stuck invoice it belongs to, R-1.5) and a text-only message may also
    run a command (R-10.1), apply its edits (D97) and store its overrides (D70). A photo
    becomes an invoice, with its caption's overrides, a ``download_media`` job and a
    BR-S09 acknowledgement, unless the sender already has ``max_queued_per_sender``
    active jobs, in which case it is answered with BR-S01 and ``back_pressure`` (BR-S01).
    ``admins`` holds the profile ids a command may reach another profile's invoice for,
    ``timezone`` is the owner's zone for the dates a command prints (S11.4), and
    ``payment_flow`` is the flow whose payments a command may record (R-7.12).
    """
    async with conn.transaction():
        await _lock_profile(conn, msg.profile_id)
        if not await store_inbound(conn, msg):
            return IntakeResult(status="duplicate", invoice_id=None)
        await touch_window(conn, msg.channel, msg.profile_id, msg.received_at)
        if msg.button_id is not None:
            if msg.button_id.startswith("p"):
                await handle_payment_press(conn, msg)
                return IntakeResult(status="stored", invoice_id=None)
            pressed = await handle_press(conn, msg)
        elif any(attachment.kind == PHOTO_KIND for attachment in msg.attachments):
            return await _queue_or_back_pressure(conn, msg, max_queued_per_sender)
        else:
            return await _store_with_overrides(conn, msg, admins, timezone, payment_flow)
    for path in pressed.photo_paths:
        await asyncio.to_thread(path.unlink, missing_ok=True)
    return IntakeResult(status="stored", invoice_id=pressed.invoice_id)


async def _lock_profile(conn: AsyncConnection[Any], profile_id: str) -> None:
    """Serialise intake of one profile until the transaction ends (PT-2)."""
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        (LOCK_PREFIX + profile_id,),
    )


async def _queue_or_back_pressure(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    max_queued_per_sender: int,
) -> IntakeResult:
    """Create the invoice and its job, or send BR-S01 when the queue is full.

    The photo's caption overrides are stored in the new invoice's flow data (D70).
    """
    active_jobs = await count_active_jobs(conn, msg.profile_id)
    if active_jobs >= max_queued_per_sender:
        text = MESSAGES[RULE_BACK_PRESSURE].format(n=active_jobs)
        await _add_outbox(conn, msg, RULE_BACK_PRESSURE, text)
        return IntakeResult(status="back_pressure", invoice_id=None)
    invoice_id = await create_invoice(conn, msg.profile_id, msg.channel)
    overrides = parse_overrides(msg.text)
    if overrides:
        await merge_flow_data(conn, invoice_id, {"overrides": overrides})
    await link_message_to_invoice(conn, msg.channel, msg.external_id, invoice_id)
    await enqueue(conn, invoice_id, DOWNLOAD_STEP)
    await _add_outbox(conn, msg, RULE_ACK, MESSAGES[RULE_ACK].format(id=invoice_id))
    return IntakeResult(status="queued", invoice_id=invoice_id)


async def _store_with_overrides(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    admins: frozenset[str],
    timezone: str,
    payment_flow: PaymentFlow | None = None,
) -> IntakeResult:
    """Store a non-photo text, running its command or applying its edits and overrides.

    A text starting with ``/`` is a command and is answered before anything else
    (R-10.1). Otherwise it may hold both ``campo=valor`` edits when ``parse_edits`` finds
    fields or errors, and ``motivo``/``descripcion`` overrides (D70, D90, D97). Each is
    applied independently to the invoice it targets, and the result names the invoice the
    edits first touched. A pdf/xml upload is linked to the stuck invoice it belongs to
    (R-1.5); another attachment kind, or a text with neither, changes nothing.
    """
    if msg.attachments:
        return IntakeResult(status="stored", invoice_id=await _link_upload(conn, msg))
    command = parse_command(msg.text)
    if command is not None:
        context = CommandContext(admins=admins, timezone=timezone, payment_flow=payment_flow)
        await handle_command(conn, msg, command[0], command[1], context=context)
        return IntakeResult(status="stored", invoice_id=None)
    edits = parse_edits(msg.text)
    invoice_id = None
    if edits.fields or edits.errors:
        invoice_id = await apply_edits(conn, msg, edits)
    overrides = parse_overrides(msg.text)
    if overrides:
        invoice_id = await apply_overrides(conn, msg, overrides) or invoice_id
    return IntakeResult(status="stored", invoice_id=invoice_id)


async def _link_upload(conn: AsyncConnection[Any], msg: InboundMessage) -> int | None:
    """Link a pdf/xml upload to the stuck invoice it belongs to (R-1.5, S7.5, D102).

    The invoice is the one the message quotes, else the profile's only open
    one. With several open invoices and no resolved quote nothing is linked:
    BR-D14 lists them and asks the sender to reply to the right photo. The
    link, the reply and the ``attach_upload`` job join the caller's
    transaction; no invoice (or another attachment kind) links nothing.
    """
    if not any(attachment.kind in UPLOAD_KINDS for attachment in msg.attachments):
        return None
    reply_to = None if msg.reply_to_external_id is None else (msg.channel, msg.reply_to_external_id)
    invoice_id = await find_open_invoice_for_attachment(conn, msg.profile_id, reply_to)
    if invoice_id is None:
        await _ask_which_invoice(conn, msg)
        return None
    await link_message_to_invoice(conn, msg.channel, msg.external_id, invoice_id)
    await enqueue(conn, invoice_id, ATTACH_STEP)
    return invoice_id


async def _ask_which_invoice(conn: AsyncConnection[Any], msg: InboundMessage) -> None:
    """Queue BR-D14 when the sender has more than one open invoice to choose from (D102)."""
    open_invoices = await list_open_invoices(conn, msg.profile_id)
    if len(open_invoices) <= 1:
        return
    invoices = ", ".join(_invoice_label(*invoice) for invoice in open_invoices)
    text = MESSAGES[RULE_AMBIGUOUS_UPLOAD].format(invoices=invoices)
    await _add_outbox(conn, msg, RULE_AMBIGUOUS_UPLOAD, text)


def _invoice_label(invoice_id: int, serie: str | None, numero: str | None) -> str:
    """Return ``#{id} {serie}-{numero}``, or ``#{id}`` when either field is missing (BR-D14)."""
    if serie is None or numero is None:
        return f"#{invoice_id}"
    return f"#{invoice_id} {serie}-{numero}"


async def _add_outbox(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    rule_id: str,
    text: str,
) -> None:
    """Queue one reply to the message's sender in the caller's transaction."""
    await queue_message(conn, Addressee(msg.channel, msg.profile_id), text, rule_id=rule_id)
