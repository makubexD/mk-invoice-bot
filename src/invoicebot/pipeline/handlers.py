"""Job handlers of the pipeline: download an invoice's media (R-1.4, R-1.11).

:func:`make_download_handler` builds the handler of the ``download_media`` job
step with its pool, channel client, data directory and media limits injected.
The download itself runs with no transaction open; only its outcome is
recorded, in one transaction, so the state change and the outbox message (or
the file row) commit together.
"""

from pathlib import Path

from invoicebot.channels.base import Attachment, AttachmentKind, InboundMessage, OutboundMessage
from invoicebot.channels.media_limits import MediaRejected, RejectionReason, max_bytes_for
from invoicebot.channels.whatsapp.client import WhatsAppClient
from invoicebot.channels.whatsapp.media import download_media
from invoicebot.config.models import MediaLimits
from invoicebot.queue import outbox
from invoicebot.queue.jobs import Job
from invoicebot.queue.worker import Handler
from invoicebot.rules.messages_es import MESSAGES, REJECTION_DESC
from invoicebot.store.db import Pool
from invoicebot.store.invoices import FileRecord, add_file, has_file, set_state

#: Rule that cancels media over a cap or of an unsupported type (BR-E07).
RULE_MEDIA_REJECTED = "BR-E07"
#: Bytes in a megabyte, for BR-E07's ``{max_mb}``.
MEGABYTE = 1024 * 1024
TMP_DIRECTORY = "tmp"


def make_download_handler(
    pool: Pool,
    client: WhatsAppClient,
    data_dir: Path,
    limits: MediaLimits,
) -> Handler:
    """Build the handler that downloads an invoice's first attachment (R-1.4).

    A successful download stores a ``files`` row and leaves the invoice in
    ``RECEIVED``; an invoice that already has a file is left untouched, so a
    retried job never downloads it twice. ``MediaRejected`` cancels the invoice
    with BR-E07 and replies to the sender; the handler returns normally, so the
    job completes rather than retrying. Any other exception propagates for the
    queue to retry.
    """

    async def handle(job: Job) -> None:
        async with pool.connection() as conn:
            if await has_file(conn, job.invoice_id):
                return
        msg = await _load_message(pool, job.invoice_id)
        attachment = _first_attachment(msg)
        try:
            media = await download_media(
                client,
                attachment.media_ref,
                attachment.kind,
                data_dir / TMP_DIRECTORY,
                limits,
            )
        except MediaRejected as rejected:
            await _reject(pool, job.invoice_id, msg, attachment.kind, rejected.reason, limits)
            return
        record = FileRecord(
            kind=attachment.kind,
            local_path=str(media.path),
            remote_ref=attachment.media_ref,
            sha256=media.sha256,
            size=media.size,
        )
        async with pool.connection() as conn, conn.transaction():
            await add_file(conn, job.invoice_id, record)

    return handle


async def _load_message(pool: Pool, invoice_id: int) -> InboundMessage:
    """Read the oldest message linked to ``invoice_id`` (R-1.5)."""
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            """
            SELECT payload_json
            FROM messages
            WHERE invoice_id = %s
            ORDER BY received_at, external_id
            LIMIT 1
            """,
            (invoice_id,),
        )
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError(f"invoice {invoice_id} has no message")
    return InboundMessage.model_validate(row[0])


def _first_attachment(msg: InboundMessage) -> Attachment:
    """Return the message's first attachment, raising when it has none."""
    if not msg.attachments:
        raise RuntimeError(f"invoice message {msg.external_id} has no attachment")
    return msg.attachments[0]


async def _reject(
    pool: Pool,
    invoice_id: int,
    msg: InboundMessage,
    kind: AttachmentKind,
    reason: RejectionReason,
    limits: MediaLimits,
) -> None:
    """Cancel the invoice with BR-E07 and tell its sender why (R-1.11)."""
    text = MESSAGES[RULE_MEDIA_REJECTED].format(
        reason_desc=REJECTION_DESC[reason],
        max_mb=_max_mb(kind, limits),
    )
    reply = OutboundMessage(
        channel=msg.channel,
        recipient=msg.profile_id,
        profile_id=msg.profile_id,
        rule_id=RULE_MEDIA_REJECTED,
        text=text,
    )
    async with pool.connection() as conn, conn.transaction():
        await set_state(conn, invoice_id, "CANCELLED", rule_id=RULE_MEDIA_REJECTED)
        await outbox.add(conn, msg.channel, msg.profile_id, reply.model_dump(mode="json"))


def _max_mb(kind: AttachmentKind, limits: MediaLimits) -> int:
    """Return the kind's byte cap in whole megabytes for BR-E07's text."""
    try:
        cap = max_bytes_for(kind, limits)
    except MediaRejected:
        # An unsupported kind has no cap of its own; the photo cap is the default.
        cap = limits.max_bytes.image
    return cap // MEGABYTE
