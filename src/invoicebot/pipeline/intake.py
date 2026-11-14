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

from invoicebot.channels.base import InboundMessage, OutboundMessage
from invoicebot.pipeline.actions import handle_press
from invoicebot.pipeline.overrides import apply_overrides, parse_overrides
from invoicebot.queue import outbox
from invoicebot.queue.jobs import enqueue
from invoicebot.rules.messages_es import MESSAGES
from invoicebot.store.flow_b import merge_flow_data
from invoicebot.store.invoices import count_active_jobs, create_invoice, touch_window
from invoicebot.store.messages import link_message_to_invoice, store_inbound

#: Rule that tells a sender their queue is full (BR-S01).
RULE_BACK_PRESSURE = "BR-S01"
#: Rule that acknowledges a queued invoice (BR-S09).
RULE_ACK = "BR-S09"
#: Job step that downloads a photo before extraction (R-1.4).
DOWNLOAD_STEP = "download_media"
#: Advisory-lock namespace that serialises one profile's intake (R-1.6, PT-2).
LOCK_PREFIX = "intake:"
#: Attachment kind that starts an invoice (R-1.1).
PHOTO_KIND = "photo"

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
) -> IntakeResult:
    """Handle one inbound message inside the caller's transaction.

    A redelivered message is a ``duplicate`` (R-1.8). Every other message
    refreshes its channel window (R-1.10); a button press runs its action and is
    ``stored`` (R-3.5), a non-photo is ``stored`` and a text-only message may also
    store its overrides (D70). A photo becomes an invoice, with its caption's
    overrides, a ``download_media`` job and a BR-S09 acknowledgement, unless the
    sender already has ``max_queued_per_sender`` active jobs, in which case it is
    answered with BR-S01 and ``back_pressure`` (BR-S01).
    """
    async with conn.transaction():
        await _lock_profile(conn, msg.profile_id)
        if not await store_inbound(conn, msg):
            return IntakeResult(status="duplicate", invoice_id=None)
        await touch_window(conn, msg.channel, msg.profile_id, msg.received_at)
        if msg.button_id is not None:
            pressed = await handle_press(conn, msg)
        elif any(attachment.kind == PHOTO_KIND for attachment in msg.attachments):
            return await _queue_or_back_pressure(conn, msg, max_queued_per_sender)
        else:
            return await _store_with_overrides(conn, msg)
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


async def _store_with_overrides(conn: AsyncConnection[Any], msg: InboundMessage) -> IntakeResult:
    """Store a non-photo message, applying its overrides when it is text-only (D70, D90).

    A message with overrides moves onto the invoice they target and is ``stored`` with
    that invoice; without overrides, or for a non-photo attachment, it changes nothing.
    """
    overrides = parse_overrides(msg.text)
    if not overrides or msg.attachments:
        return IntakeResult(status="stored", invoice_id=None)
    invoice_id = await apply_overrides(conn, msg, overrides)
    return IntakeResult(status="stored", invoice_id=invoice_id)


async def _add_outbox(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    rule_id: str,
    text: str,
) -> None:
    """Queue one reply to the message's sender in the caller's transaction."""
    reply = OutboundMessage(
        channel=msg.channel,
        recipient=msg.profile_id,
        profile_id=msg.profile_id,
        rule_id=rule_id,
        text=text,
    )
    await outbox.add(conn, msg.channel, msg.profile_id, reply.model_dump(mode="json"))
