"""Job handlers of the pipeline: download an invoice's media (R-1.4, R-1.11).

:func:`make_download_handler` builds the handler of the ``download_media`` job
step with its pool, channel client, data directory and media limits injected.
The download itself runs with no transaction open; only its outcome is
recorded, in one transaction, so the state change and the outbox message (or
the file row and its ``extract`` job) commit together.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from invoicebot.channels.base import Attachment, AttachmentKind, InboundMessage
from invoicebot.channels.media_limits import MediaRejected, RejectionReason, max_bytes_for
from invoicebot.channels.whatsapp.client import WhatsAppClient
from invoicebot.channels.whatsapp.media import download_media
from invoicebot.config.models import MediaLimits
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.queue.jobs import Job, enqueue
from invoicebot.queue.worker import Handler
from invoicebot.rules.messages_es import MESSAGES, REJECTION_DESC
from invoicebot.states import CANCELLED
from invoicebot.store.db import Pool
from invoicebot.store.invoices import FileRecord, add_file, has_file, set_state

#: Job step that reads the downloaded photo or PDF (S3.11).
EXTRACT_STEP = "extract"
#: Rule that cancels media over a cap or of an unsupported type (BR-E07).
RULE_MEDIA_REJECTED = "BR-E07"
#: Bytes in a megabyte, for BR-E07's ``{max_mb}``.
MEGABYTE = 1024 * 1024
TMP_DIRECTORY = "tmp"

#: Why a file is refused: the media checks' reasons plus a PDF over the page cap (D122).
type RejectReason = RejectionReason | Literal["too_many_pages"]


@dataclass(frozen=True)
class MediaRejection:
    """A file the pipeline refuses with BR-E07, and who to tell why (R-1.11, BR-E07)."""

    invoice_id: int
    addressee: Addressee
    kind: AttachmentKind
    reason: RejectReason
    limits: MediaLimits
    #: The state the invoice must still be in, or None to cancel it whatever its state (D95).
    expected: str | None = None


def make_download_handler(
    pool: Pool,
    client: WhatsAppClient,
    data_dir: Path,
    limits: MediaLimits,
) -> Handler:
    """Build the handler that downloads an invoice's first attachment (R-1.4).

    A successful download stores a ``files`` row, enqueues the ``extract`` step
    in the same transaction and leaves the invoice in ``RECEIVED``; an invoice
    that already has a file is left untouched, so a retried job neither
    downloads nor enqueues it twice. ``MediaRejected`` cancels the invoice
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
            await reject_media(
                pool,
                MediaRejection(
                    job.invoice_id,
                    Addressee(msg.channel, msg.profile_id),
                    attachment.kind,
                    rejected.reason,
                    limits,
                ),
            )
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
            await enqueue(conn, job.invoice_id, EXTRACT_STEP)

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


async def reject_media(pool: Pool, rejection: MediaRejection) -> None:
    """Cancel the invoice with BR-E07 and tell its sender why (R-1.11, BR-E07).

    Shared by the download step, which refuses an oversized or malformed file, and the
    extract step, which refuses a PDF over the page cap (D122). With ``expected`` set, an
    invoice moved on by ``/cancelar`` raises :class:`StateMoved` and nothing is stored.
    """
    text = MESSAGES[RULE_MEDIA_REJECTED].format(
        reason_desc=_reason_desc(rejection.reason, rejection.limits),
        max_mb=_max_mb(rejection.kind, rejection.limits),
    )
    async with pool.connection() as conn, conn.transaction():
        await set_state(
            conn,
            rejection.invoice_id,
            CANCELLED,
            rule_id=RULE_MEDIA_REJECTED,
            expected=rejection.expected,
        )
        await queue_message(
            conn,
            rejection.addressee,
            invoice_id=rejection.invoice_id,
            rule_id=RULE_MEDIA_REJECTED,
            text=text,
        )


def _reason_desc(reason: RejectReason, limits: MediaLimits) -> str:
    """Return BR-E07's reason text, naming the page cap when that is the reason (D122)."""
    return REJECTION_DESC[reason].format(max_pages=limits.max_pdf_pages)


def _max_mb(kind: AttachmentKind, limits: MediaLimits) -> int:
    """Return the kind's byte cap in whole megabytes for BR-E07's text."""
    try:
        cap = max_bytes_for(kind, limits)
    except MediaRejected:
        # An unsupported kind has no cap of its own; the photo cap is the default.
        cap = limits.max_bytes.image
    return cap // MEGABYTE
