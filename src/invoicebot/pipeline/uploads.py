"""Job handler that attaches a sender's uploads to a stuck invoice (S7.5, R-1.5).

:func:`make_attach_handler` builds the handler of the ``attach_upload`` job step. It acts
on a ``MANUAL_REQUIRED`` invoice only (D101): each linked PDF or XML the flow lets a sender
upload is downloaded, an XML is checked like a fetched one (R-5.6), and the kept files are
stored with their media ref in one transaction. When every required document is present
the invoice moves to ``DELIVERING`` with its deliver job; otherwise BR-D09 says what is
still missing. A run that stores nothing leaves the invoice as it was.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from psycopg import AsyncConnection

from invoicebot.channels.base import Attachment
from invoicebot.channels.media_limits import MediaRejected
from invoicebot.channels.whatsapp.client import WhatsAppClient
from invoicebot.channels.whatsapp.media import DownloadedMedia, download_media
from invoicebot.config.models import Config, Flow, MediaLimits
from invoicebot.extract.fields import Reading
from invoicebot.extract.ubl import XmlRejected, matches_invoice, parse_invoice_xml
from invoicebot.pipeline.handlers import TMP_DIRECTORY
from invoicebot.pipeline.intake import UPLOAD_KINDS
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.steps import DELIVER_STEP
from invoicebot.queue.jobs import Job, enqueue
from invoicebot.queue.worker import Handler
from invoicebot.rules.messages_es import (
    MESSAGES,
    UPLOAD_XML_INVALID,
    UPLOAD_XML_MISMATCH,
    docs_desc,
)
from invoicebot.store.db import Pool
from invoicebot.store.invoices import (
    FileKind,
    FileRecord,
    InvoiceHead,
    add_file,
    get_file_path,
    list_unattached_uploads,
    load_flow,
    load_invoice_head,
    load_reading,
    set_state,
)

logger = logging.getLogger(__name__)

#: The only state this handler acts on; any other leaves the invoice as it is (R-1.5, D101).
MANUAL_STATE = "MANUAL_REQUIRED"
#: Where an invoice goes once every required document is stored (R-1.5).
DELIVERING_STATE = "DELIVERING"
#: BR-F09's reply when an uploaded XML is unsafe or invalid.
RULE_XML_INVALID = "BR-F09"
#: BR-F06's reply when an uploaded XML is not this invoice's.
RULE_XML_MISMATCH = "BR-F06"
#: BR-D09's reply while a required document is still missing.
RULE_MISSING_DOCS = "BR-D09"


@dataclass(frozen=True)
class _Inputs:
    """One invoice's attach inputs, shared by every decision branch."""

    pool: Pool
    job: Job
    head: InvoiceHead
    flow: Flow
    reading: Reading
    uploads: Sequence[Attachment]
    serie: str
    numero: str


@dataclass(frozen=True)
class _Rejection:
    """A reply the sender gets for one upload that was not kept."""

    rule_id: str
    text: str


@dataclass(frozen=True)
class _Outcome:
    """The files one run keeps, the kinds they add and the replies it queues."""

    kept: tuple[FileRecord, ...]
    stored_kinds: tuple[str, ...]
    replies: tuple[_Rejection, ...]


def make_attach_handler(
    pool: Pool,
    cfg: Config,
    client: WhatsAppClient,
    data_dir: Path,
    limits: MediaLimits,
) -> Handler:
    """Build the handler that attaches a stuck invoice's uploaded files (S7.5).

    An invoice outside ``MANUAL_REQUIRED`` is left untouched, and so is one whose linked
    uploads are all stored already, so a redelivered job writes nothing (D101). A missing
    reading or flow raises, so the queue retries the job.
    """

    async def handle(job: Job) -> None:
        async with pool.connection() as conn:
            head = await load_invoice_head(conn, job.invoice_id)
            if head is None or head.state != MANUAL_STATE:
                return
            uploads = await list_unattached_uploads(conn, job.invoice_id, UPLOAD_KINDS)
            if not uploads:
                return
            reading = await load_reading(conn, job.invoice_id)
            flow_name = await load_flow(conn, job.invoice_id)
        if reading is None:
            raise RuntimeError(f"invoice {job.invoice_id} has no reading")
        if flow_name is None:
            raise RuntimeError(f"invoice {job.invoice_id} was not routed")
        flow = cfg.flows.get(flow_name)
        if flow is None:
            raise RuntimeError(f"invoice {job.invoice_id} has unknown flow {flow_name!r}")
        serie, numero = _serie_numero(reading, job.invoice_id)
        inputs = _Inputs(
            pool=pool,
            job=job,
            head=head,
            flow=flow,
            reading=reading,
            uploads=uploads,
            serie=serie,
            numero=numero,
        )
        out_dir = data_dir / TMP_DIRECTORY / str(job.invoice_id) / f"{job.id}-{uuid.uuid4().hex}"
        try:
            outcome = await _collect(inputs, client, out_dir, limits)
            stored = await _commit(inputs, outcome)
        except BaseException:
            # R-9.2: a failed or interrupted run leaves no partial files behind.
            shutil.rmtree(out_dir, ignore_errors=True)
            raise
        if not stored:
            shutil.rmtree(out_dir, ignore_errors=True)

    return handle


async def _collect(
    inputs: _Inputs,
    client: WhatsAppClient,
    out_dir: Path,
    limits: MediaLimits,
) -> _Outcome:
    """Download each allowed upload and keep the ones that pass the XML checks (R-1.5, R-5.6)."""
    kept: list[FileRecord] = []
    stored_kinds: list[str] = []
    replies: list[_Rejection] = []
    for upload in inputs.uploads:
        if upload.kind not in inputs.flow.manual_uploads:
            logger.debug(
                "upload kind not allowed",
                extra={"invoice_id": inputs.job.invoice_id, "kind": upload.kind},
            )
            continue
        media = await _download(client, upload, out_dir, limits, inputs.job.invoice_id)
        if media is None:
            continue
        rejection = await _xml_rejection(media, upload, inputs)
        # shortcut: a rejected XML leaves no files row, so a later attach job for this
        # invoice re-checks it and repeats the reply; settle in the plan's finish review.
        if rejection is not None:
            _discard(media)
            replies.append(rejection)
            continue
        kept.append(
            FileRecord(
                kind=upload.kind,
                local_path=str(media.path),
                remote_ref=upload.media_ref,
                sha256=media.sha256,
                size=media.size,
            )
        )
        stored_kinds.append(upload.kind)
    return _Outcome(kept=tuple(kept), stored_kinds=tuple(stored_kinds), replies=tuple(replies))


async def _download(
    client: WhatsAppClient,
    upload: Attachment,
    out_dir: Path,
    limits: MediaLimits,
    invoice_id: int,
) -> DownloadedMedia | None:
    """Download one upload, logging and skipping a rejected or ill-formed media ref (R-1.11)."""
    try:
        return await download_media(client, upload.media_ref, upload.kind, out_dir, limits)
    except (MediaRejected, ValueError) as rejected:
        logger.warning(
            "upload skipped",
            extra={"invoice_id": invoice_id, "reason": str(rejected)},
        )
        return None


async def _xml_rejection(media: DownloadedMedia, upload: Attachment, inputs: _Inputs) -> _Rejection | None:
    """Return the reply for an XML that is unsafe or not this invoice's, or None (BR-F06, BR-F09)."""
    if upload.kind != "xml":
        return None
    try:
        xml = parse_invoice_xml(await asyncio.to_thread(media.path.read_bytes))
    except XmlRejected:
        return _Rejection(RULE_XML_INVALID, UPLOAD_XML_INVALID)
    if matches_invoice(xml, inputs.reading):
        return None
    return _Rejection(
        RULE_XML_MISMATCH,
        UPLOAD_XML_MISMATCH.format(serie=inputs.serie, numero=inputs.numero),
    )


async def _commit(inputs: _Inputs, outcome: _Outcome) -> bool:
    """Store the kept files and move the invoice on, in one transaction; return whether it stored any.

    The state is re-read first, so a run whose invoice moved on writes nothing (D95).
    """
    async with inputs.pool.connection() as conn, conn.transaction():
        head = await load_invoice_head(conn, inputs.job.invoice_id)
        if head is None or head.state != MANUAL_STATE:
            return False
        for record in outcome.kept:
            await add_file(conn, inputs.job.invoice_id, record)
        addressee = Addressee(inputs.head.origin_channel, inputs.head.profile_id)
        for rejection in outcome.replies:
            await queue_message(
                conn,
                addressee,
                invoice_id=inputs.job.invoice_id,
                rule_id=rejection.rule_id,
                text=rejection.text,
            )
        missing = await _missing_docs(conn, inputs)
        if not missing:
            await set_state(conn, inputs.job.invoice_id, DELIVERING_STATE, expected=MANUAL_STATE)
            await enqueue(conn, inputs.job.invoice_id, DELIVER_STEP)
        elif outcome.stored_kinds:
            await _reply_missing(conn, inputs, outcome, missing, addressee)
    return bool(outcome.kept)


async def _missing_docs(conn: AsyncConnection[Any], inputs: _Inputs) -> list[str]:
    """Return the flow's required kinds the invoice still lacks, in flow order (R-1.5)."""
    missing: list[str] = []
    for kind in inputs.flow.required_docs:
        if await get_file_path(conn, inputs.job.invoice_id, cast(FileKind, kind)) is None:
            missing.append(kind)
    return missing


async def _reply_missing(
    conn: AsyncConnection[Any],
    inputs: _Inputs,
    outcome: _Outcome,
    missing: Sequence[str],
    addressee: Addressee,
) -> None:
    """Queue BR-D09 naming what arrived and what is still missing (BR-D09)."""
    text = MESSAGES[RULE_MISSING_DOCS].format(
        received_desc=docs_desc(outcome.stored_kinds),
        missing_desc=docs_desc(missing),
        serie=inputs.serie,
        numero=inputs.numero,
    )
    await queue_message(
        conn,
        addressee,
        invoice_id=inputs.job.invoice_id,
        rule_id=RULE_MISSING_DOCS,
        text=text,
    )


def _serie_numero(reading: Reading, invoice_id: int) -> tuple[str, str]:
    """Return the invoice's serie and numero, raising when the reading lacks one."""
    serie = reading.value("serie")
    numero = reading.value("numero")
    if serie is None or numero is None:
        raise RuntimeError(f"invoice {invoice_id} lacks its serie or numero")
    return serie, numero


def _discard(media: DownloadedMedia) -> None:
    """Remove a rejected upload's file and the directory the download made for it."""
    shutil.rmtree(media.path.parent, ignore_errors=True)
