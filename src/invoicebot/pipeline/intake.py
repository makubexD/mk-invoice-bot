"""Intake: persist one inbound message, acknowledge it and queue its invoice.

This module owns the first pipeline step only (R-1.1, R-1.8, BR-S01). It makes
no network calls and never commits: everything happens in the caller's
transaction, so a later failure leaves no message, invoice, job or outbox row.
"""

from dataclasses import dataclass
from typing import Any, Literal

from psycopg import AsyncConnection

from invoicebot.channels.base import InboundMessage, OutboundMessage
from invoicebot.queue import outbox
from invoicebot.queue.jobs import enqueue
from invoicebot.rules.messages_es import MESSAGES
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
    refreshes its channel window (R-1.10); a non-photo is only ``stored``. A
    photo becomes an invoice with a ``download_media`` job and a BR-S09
    acknowledgement, unless the sender already has ``max_queued_per_sender``
    active jobs, in which case it is answered with BR-S01 and ``back_pressure``
    (BR-S01).
    """
    async with conn.transaction():
        await _lock_profile(conn, msg.profile_id)
        if not await store_inbound(conn, msg):
            return IntakeResult(status="duplicate", invoice_id=None)
        await touch_window(conn, msg.channel, msg.profile_id, msg.received_at)
        if not any(attachment.kind == PHOTO_KIND for attachment in msg.attachments):
            return IntakeResult(status="stored", invoice_id=None)
        return await _queue_or_back_pressure(conn, msg, max_queued_per_sender)


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
    """Create the invoice and its job, or send BR-S01 when the queue is full."""
    active_jobs = await count_active_jobs(conn, msg.profile_id)
    if active_jobs >= max_queued_per_sender:
        text = MESSAGES[RULE_BACK_PRESSURE].format(n=active_jobs)
        await _add_outbox(conn, msg, RULE_BACK_PRESSURE, text)
        return IntakeResult(status="back_pressure", invoice_id=None)
    invoice_id = await create_invoice(conn, msg.profile_id, msg.channel)
    await link_message_to_invoice(conn, msg.channel, msg.external_id, invoice_id)
    await enqueue(conn, invoice_id, DOWNLOAD_STEP)
    await _add_outbox(conn, msg, RULE_ACK, MESSAGES[RULE_ACK].format(id=invoice_id))
    return IntakeResult(status="queued", invoice_id=invoice_id)


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
