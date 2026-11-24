"""Intake: persist one inbound message, acknowledge it and queue its invoice.

This module owns the first pipeline step only (R-1.1, R-1.8, BR-S01). It never
commits: everything happens in the caller's transaction, so a later failure
leaves no message, invoice, job or outbox row.
The photo files a button press releases are deleted after that transaction (R-9.7).
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal

from psycopg import AsyncConnection

from invoicebot.channels.base import InboundMessage
from invoicebot.channels.buttons import COMMAND_BUTTON_PREFIX, is_button_command
from invoicebot.config.models import MediaMaxBytes
from invoicebot.pipeline.actions import PressResult, handle_press
from invoicebot.pipeline.commands import (
    CommandContext,
    PaymentFlow,
    handle_command,
    parse_command,
)
from invoicebot.pipeline.edits import apply_edits, parse_edits
from invoicebot.pipeline.handlers import MEGABYTE, RULE_MEDIA_REJECTED
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.overrides import apply_overrides, parse_overrides
from invoicebot.pipeline.payments import PAYMENT_BUTTON_PREFIX, handle_payment_press
from invoicebot.pipeline.portal_offer import OFFER_BUTTON_PREFIX
from invoicebot.pipeline.portal_offer import handle_press as handle_offer_press
from invoicebot.pipeline.steps import ATTACH_STEP
from invoicebot.pipeline.steps import DOWNLOAD_STEP as DOWNLOAD_STEP
from invoicebot.pipeline.teach import (
    BUTTON_ID_PREFIX as TEACH_BUTTON_PREFIX,
)
from invoicebot.pipeline.teach import (
    TeachSetup,
    answer,
)
from invoicebot.pipeline.teach import (
    handle_press as handle_teach_press,
)
from invoicebot.queue.jobs import enqueue
from invoicebot.rules.messages_es import MESSAGES, REJECTION_DESC
from invoicebot.store.flow_b import merge_flow_data
from invoicebot.store.invoices import count_active_jobs, create_invoice, touch_window
from invoicebot.store.messages import (
    find_open_invoice_for_attachment,
    link_message_to_invoice,
    list_open_invoices,
    store_inbound,
)

logger = logging.getLogger(__name__)

#: Rule that tells a sender their queue is full (BR-S01).
RULE_BACK_PRESSURE = "BR-S01"
#: Rule that acknowledges a queued invoice (BR-S09).
RULE_ACK = "BR-S09"
#: Rule that asks which open invoice an ambiguous upload belongs to (BR-D14).
RULE_AMBIGUOUS_UPLOAD = "BR-D14"
#: Most invoices BR-D14 lists before ending the list with ``, …`` (BR-D14, D140).
BR_D14_MAX_ITEMS = 10
#: Advisory-lock namespace that serialises one profile's intake (R-1.6, PT-2).
LOCK_PREFIX = "intake:"
#: Attachment kind that starts an invoice (R-1.1).
PHOTO_KIND = "photo"
#: Attachment kind that starts an invoice when nothing is open for it (D121).
PDF_KIND = "pdf"
#: Attachment kinds a sender may upload for a stuck invoice (R-1.5, D101).
UPLOAD_KINDS = ("pdf", "xml")

type IntakeStatus = Literal["duplicate", "queued", "back_pressure", "stored"]


@dataclass(frozen=True)
class IntakeResult:
    """What intake did with one inbound message."""

    status: IntakeStatus
    invoice_id: int | None


@dataclass(frozen=True)
class IntakeRoles:
    """The config-derived roles intake hands on: who is an admin and which flows are special.

    ``admins`` holds the profile ids a command may reach another profile's invoice for,
    ``payment_flow`` is the flow whose payments a command may record (R-7.12),
    ``laptop_flows`` names the flows whose mailer lists ``laptop_outlook`` (D109), and
    ``teach`` is the profiles and resolver a /portal or BR-F02 answer teaches with,
    None when ``gaps.teach_via_chat`` is off (R-11.4), and ``reprocess_min_interval_s``
    is the least time between two fetch rounds ``/reprocesar`` may start, 0 for no
    wait (D171). It has no default, as in ``CommandContext``, so roles built by hand
    cannot drop D171's brake; ``NO_ROLES`` (tests, callers without config) has none.
    """

    reprocess_min_interval_s: int
    admins: frozenset[str] = frozenset()
    payment_flow: PaymentFlow | None = None
    laptop_flows: frozenset[str] = frozenset()
    teach: TeachSetup | None = None


#: No admins, no payment flow, no laptop flows and no /reprocesar wait: what a caller without config passes.
NO_ROLES = IntakeRoles(reprocess_min_interval_s=0)


@dataclass(frozen=True)
class IntakeSettings:
    """The config-derived arguments intake needs for every inbound message (D165).

    ``max_queued_per_sender``, ``media_max_bytes`` and ``timezone`` are the
    per-message knobs the webhook and the console CLI share; ``roles`` carries
    the admins, payment flow, laptop flows and teaching setup
    (:class:`IntakeRoles`). ``roles`` has no default, so an entry point built from
    config cannot drop D171's brake.
    """

    max_queued_per_sender: int
    media_max_bytes: MediaMaxBytes
    timezone: str
    roles: IntakeRoles


async def handle_inbound(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    *,
    max_queued_per_sender: int,
    media_max_bytes: MediaMaxBytes,
    timezone: str,
    roles: IntakeRoles = NO_ROLES,
) -> IntakeResult:
    """Handle one inbound message inside the caller's transaction.

    A redelivered message is a ``duplicate`` (R-1.8). Every other message
    refreshes its channel window (R-1.10); a button press runs its action and is
    ``stored`` (R-3.5), except a command button (D189), a payment proposal press
    (R-7.12), a BR-S06 decision press (D118) or a BR-F14 portal offer press (D189),
    which are answered as stored without an invoice. A photo, and a PDF
    with no open invoice for the sender, becomes an invoice with its caption's overrides,
    a ``download_media`` job and a BR-S09 acknowledgement (R-1.1, D121), unless the
    sender already has ``max_queued_per_sender`` active jobs, in which case it is
    answered with BR-S01 and ``back_pressure`` (BR-S01). A pdf or xml with an open
    invoice is ``stored`` as an upload for it (R-1.5); an xml with no open invoice is
    refused with BR-E07, naming the photo and PDF size caps from
    ``media_max_bytes``, and creates nothing (D58, D121). A text-only message may also
    run a command (R-10.1), answer a BR-F02 portal question (R-11.4), apply its edits
    (D97) and store its overrides (D70). ``timezone`` is the owner's zone for the dates
    a command prints (S11.4), and ``roles`` carries the admins, the payment flow and
    the laptop flows (:class:`IntakeRoles`).
    """
    async with conn.transaction():
        await _lock_profile(conn, msg.profile_id)
        if not await store_inbound(conn, msg):
            return IntakeResult(status="duplicate", invoice_id=None)
        await touch_window(conn, msg.channel, msg.profile_id, msg.received_at)
        if msg.button_id is not None:
            pressed = await _handle_press(conn, msg, msg.button_id, roles, timezone)
            if pressed is None:
                return IntakeResult(status="stored", invoice_id=None)
        else:
            return await _handle_non_press(conn, msg, roles, timezone, max_queued_per_sender, media_max_bytes)
    for path in pressed.photo_paths:
        await asyncio.to_thread(path.unlink, missing_ok=True)
    return IntakeResult(status="stored", invoice_id=pressed.invoice_id)


async def _handle_press(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    button_id: str,
    roles: IntakeRoles,
    timezone: str,
) -> PressResult | None:
    """Run one button press, or return None when a special prefix's press is already answered (D189).

    A command button, a payment proposal, a BR-S06 decision and a BR-F14 portal offer
    change no invoice; every other button goes through :func:`handle_press`. Only a
    command in D190's allowlist runs; any other is logged and ignored.
    """
    if button_id.startswith(COMMAND_BUTTON_PREFIX):
        command = parse_command(button_id)
        if command is not None:
            name, arg = command
            if is_button_command(name, arg):
                await handle_command(conn, msg, name, arg, context=_command_context(roles, timezone))
            else:
                logger.info("ignored command button", extra={"profile_id": msg.profile_id})
        return None
    if button_id.startswith(PAYMENT_BUTTON_PREFIX):
        await handle_payment_press(conn, msg)
        return None
    if button_id.startswith(TEACH_BUTTON_PREFIX) and roles.teach is not None:
        await handle_teach_press(conn, msg, roles.teach)
        return None
    if button_id.startswith(OFFER_BUTTON_PREFIX) and roles.teach is not None:
        await handle_offer_press(conn, msg, roles.teach)
        return None
    return await handle_press(conn, msg, roles.laptop_flows)


async def _handle_non_press(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    roles: IntakeRoles,
    timezone: str,
    max_queued_per_sender: int,
    media_max_bytes: MediaMaxBytes,
) -> IntakeResult:
    """Route a message with no button press: queue it, refuse it or store it (D121)."""
    if any(attachment.kind == PHOTO_KIND for attachment in msg.attachments):
        return await _queue_or_back_pressure(conn, msg, max_queued_per_sender)
    if await _upload_without_open_invoice(conn, msg):
        if any(attachment.kind == PDF_KIND for attachment in msg.attachments):
            return await _queue_or_back_pressure(conn, msg, max_queued_per_sender)
        return await _refuse_lone_xml(conn, msg, media_max_bytes)
    return await _store_with_overrides(conn, msg, roles, timezone)


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


async def _upload_without_open_invoice(conn: AsyncConnection[Any], msg: InboundMessage) -> bool:
    """Return whether a pdf/xml has no open invoice to attach to (D121).

    With no open invoice there is no quote that could resolve to one, so the
    PDF starts a new invoice and the XML is refused. With an open invoice the
    upload stays an upload for it (R-1.5).
    """
    if not any(attachment.kind in UPLOAD_KINDS for attachment in msg.attachments):
        return False
    return not await list_open_invoices(conn, msg.profile_id)


async def _refuse_lone_xml(
    conn: AsyncConnection[Any], msg: InboundMessage, media_max_bytes: MediaMaxBytes
) -> IntakeResult:
    """Answer BR-E07 for an XML sent on its own and create nothing (D58, D121).

    The text quotes the photo and PDF size caps from ``media_max_bytes``.
    """
    text = MESSAGES[RULE_MEDIA_REJECTED].format(
        reason_desc=REJECTION_DESC["unsupported_type"],
        image_mb=media_max_bytes.image // MEGABYTE,
        pdf_mb=media_max_bytes.pdf // MEGABYTE,
    )
    await _add_outbox(conn, msg, RULE_MEDIA_REJECTED, text)
    return IntakeResult(status="stored", invoice_id=None)


def _command_context(roles: IntakeRoles, timezone: str) -> CommandContext:
    """Build the context a command runs with, shared by a typed command and a command button (D189)."""
    return CommandContext(
        admins=roles.admins,
        timezone=timezone,
        payment_flow=roles.payment_flow,
        teach=roles.teach,
        reprocess_min_interval_s=roles.reprocess_min_interval_s,
    )


async def _store_with_overrides(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    roles: IntakeRoles,
    timezone: str,
) -> IntakeResult:
    """Store a non-photo text, running its command or applying its edits and overrides.

    A text starting with ``/`` is a command and is answered before anything else
    (R-10.1). A text that answers the sender's newest BR-F02 question teaches a portal
    (R-11.4). Otherwise it may hold both ``campo=valor`` edits when ``parse_edits`` finds
    fields or errors, and ``motivo``/``descripcion`` overrides (D70, D90, D97). Each is
    applied independently to the invoice it targets, and the result names the invoice the
    edits first touched. A pdf/xml upload is linked to the stuck invoice it belongs to
    (R-1.5); another attachment kind, or a text with neither, changes nothing.
    """
    if msg.attachments:
        return IntakeResult(status="stored", invoice_id=await _link_upload(conn, msg))
    command = parse_command(msg.text)
    if command is not None:
        await handle_command(conn, msg, command[0], command[1], context=_command_context(roles, timezone))
        return IntakeResult(status="stored", invoice_id=None)
    if roles.teach is not None and await answer(conn, roles.teach, msg):
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
    """Queue BR-D14 when the sender has more than one open invoice to choose from (D102).

    The list is newest first (highest id) and shows at most ``BR_D14_MAX_ITEMS``
    invoices, then ``, …`` when there are more (BR-D14, D140).
    """
    open_invoices = await list_open_invoices(conn, msg.profile_id)
    if len(open_invoices) <= 1:
        return
    newest_first = sorted(open_invoices, key=lambda invoice: invoice[0], reverse=True)
    invoices = ", ".join(_invoice_label(*invoice) for invoice in newest_first[:BR_D14_MAX_ITEMS])
    if len(newest_first) > BR_D14_MAX_ITEMS:
        invoices += ", …"
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
