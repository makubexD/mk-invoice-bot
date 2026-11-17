"""Job handler that files a flow A invoice in Google Drive and its row in Sheets (S9.2 to S9.4).

:func:`make_google_deliver_handler` builds the handler of the ``deliver`` job step for the
``personal_google`` flow. It acts only on an invoice in ``DELIVERING`` (R-9.4): it reads the
invoice and its local files in one short connection, runs the flow's Drive and Sheets sinks in
config order with no connection held, then in one transaction moves the invoice to ``DONE``,
queues its completion message and clears the photo paths. Only after that transaction commits
are the photo, PDF and XML files deleted (R-9.7). A revoked Google token parks the invoice in
``PENDING_AUTH`` with BR-D01; a Google failure propagates so the queue retries it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection

from invoicebot.auth.msal_cache import AuthRequired
from invoicebot.config.models import (
    Config,
    Flow,
    GoogleDriveSink,
    GoogleSheetsSink,
    Profile,
)
from invoicebot.extract.fields import Reading
from invoicebot.extract.ubl import parse_invoice_xml
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.pending_auth import park_for_auth
from invoicebot.pipeline.route import natural_key
from invoicebot.pipeline.steps import DELIVER_STEP
from invoicebot.pipeline.values import invoice_values
from invoicebot.queue.jobs import Job
from invoicebot.queue.worker import Handler
from invoicebot.rules.messages_es import MESSAGES
from invoicebot.sinks.drive_files import LINK_FOLDER, DriveInput, file_to_drive
from invoicebot.sinks.google_drive import GoogleDrive
from invoicebot.sinks.google_sheets import GoogleSheets
from invoicebot.sinks.sheet_rows import LINK_ROW, upsert_row
from invoicebot.states import DELIVERING, DONE
from invoicebot.store.db import Pool
from invoicebot.store.flow_b import take_photo_paths
from invoicebot.store.invoices import (
    InvoiceHead,
    get_file_path,
    load_flow,
    load_invoice_head,
    load_reading,
    load_sunat_status,
    photo_paths,
    set_state,
)

logger = logging.getLogger(__name__)

#: BR-D16: the rule that reports a delivered invoice with its row and folder links (S9.4).
RULE_DONE = "BR-D16"
#: BR-D17: the rule that reports a delivered invoice that still has no PDF (R-8.1).
RULE_DONE_NO_PDF = "BR-D17"
#: The provider slug in BR-D01's admin text, which names ``scripts/auth_<slug>.py``.
AUTH_PROVIDER_SLUG = "google"


@dataclass(frozen=True)
class GoogleDeliverContext:
    """The flow A delivery handler's dependencies (D129)."""

    pool: Pool
    cfg: Config
    drive: GoogleDrive
    sheets: GoogleSheets
    admins: Mapping[str, Profile]


@dataclass(frozen=True)
class _Invoice:
    """One invoice's delivery inputs, read before any Google call."""

    job: Job
    head: InvoiceHead
    reading: Reading
    flow: Flow
    files: DriveInput
    base: dict[str, object]
    pdf_path: Path | None
    xml_path: Path | None


@dataclass(frozen=True)
class _Sinks:
    """The flow A sinks a delivery needs, in configuration order (R-6.1, R-6.2)."""

    drive: GoogleDriveSink
    sheets: GoogleSheetsSink


def make_google_deliver_handler(context: GoogleDeliverContext) -> Handler:
    """Build the handler that files a flow A invoice and writes its Sheets row (S9.2 to S9.4).

    An invoice outside ``DELIVERING`` is left untouched (R-9.4). The Drive and Sheets sinks
    run with no database connection held; a retry repeats both, which is safe because Drive
    reuses names and Sheets upserts by key (R-6.3). A revoked token parks the invoice until
    the account signs in (BR-D01).
    """

    async def handle(job: Job) -> None:
        started = time.perf_counter()
        invoice = await _load(context, job)
        if invoice is None:
            return
        sinks = _sinks(invoice.flow)
        if sinks is None:
            logger.info(
                "step skipped",
                extra={"step": DELIVER_STEP, "invoice_id": job.invoice_id, "reason": "no_drive_sink"},
            )
            return
        try:
            values = await _run_sinks(context, invoice, sinks)
        except AuthRequired as required:
            await _park_auth(context, invoice, required)
            return
        await _complete(context, invoice, values)
        logger.info(
            "step done",
            extra={
                "step": DELIVER_STEP,
                "invoice_id": job.invoice_id,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            },
        )

    return handle


async def _load(context: GoogleDeliverContext, job: Job) -> _Invoice | None:
    """Read the invoice's delivery inputs, or return None when it is not ``DELIVERING`` (R-9.4).

    All database reads happen in one short connection; the file bytes are read off the event
    loop afterwards, so no connection is held while Google is called.
    """
    async with context.pool.connection() as conn:
        head = await load_invoice_head(conn, job.invoice_id)
        if head is None or head.state != DELIVERING:
            return None
        reading = await load_reading(conn, job.invoice_id)
        flow_name = await load_flow(conn, job.invoice_id)
        photos = await photo_paths(conn, job.invoice_id)
        pdf_path = await get_file_path(conn, job.invoice_id, "pdf")
        xml_path = await get_file_path(conn, job.invoice_id, "xml")
        sunat_status = await load_sunat_status(conn, job.invoice_id)
    if reading is None or flow_name is None:
        raise RuntimeError(f"invoice {job.invoice_id} lacks its reading or flow")
    flow = context.cfg.flows.get(flow_name)
    if flow is None:
        raise RuntimeError(f"invoice {job.invoice_id} has unknown flow {flow_name!r}")
    pdf = None if pdf_path is None else await asyncio.to_thread(Path(pdf_path).read_bytes)
    xml = None if xml_path is None else await asyncio.to_thread(Path(xml_path).read_bytes)
    xml_reading = None if xml is None else parse_invoice_xml(xml)
    return _Invoice(
        job=job,
        head=head,
        reading=reading,
        flow=flow,
        files=DriveInput(photos=photos, pdf=pdf, xml=xml),
        base=invoice_values(reading, xml_reading, sunat_status),
        pdf_path=None if pdf_path is None else Path(pdf_path),
        xml_path=None if xml_path is None else Path(xml_path),
    )


def _sinks(flow: Flow) -> _Sinks | None:
    """Return the flow's Drive and Sheets sinks, or None without a Drive sink (R-6.1, R-6.2)."""
    drive: GoogleDriveSink | None = None
    sheets: GoogleSheetsSink | None = None
    for sink in flow.sinks:
        if isinstance(sink, GoogleDriveSink):
            drive = sink
        elif isinstance(sink, GoogleSheetsSink):
            sheets = sink
    if drive is None:
        return None
    if sheets is None:
        raise RuntimeError("a flow with a google_drive sink needs a google_sheets sink")
    return _Sinks(drive=drive, sheets=sheets)


async def _run_sinks(context: GoogleDeliverContext, invoice: _Invoice, sinks: _Sinks) -> dict[str, object]:
    """Run the Drive and Sheets sinks in order, merging each one's links into the values (R-6.1, R-6.2).

    The natural key is claimed from the reading before the first call; a missing part is a
    ``RuntimeError``, like any other missing delivery input. A retry repeats both sinks, which
    is safe because Drive reuses names and Sheets upserts by key (R-6.3).
    """
    key = natural_key(invoice.head.profile_id, invoice.reading)
    if key is None:
        raise RuntimeError(f"invoice {invoice.job.invoice_id} has no natural key")
    values = dict(invoice.base)
    values.update(await file_to_drive(sinks.drive, context.drive, invoice.files, values))
    values.update(await upsert_row(sinks.sheets, context.sheets, key, values))
    return values


async def _park_auth(context: GoogleDeliverContext, invoice: _Invoice, required: AuthRequired) -> None:
    """Park the invoice until the Google account signs in and alert its sender and admins (BR-D01)."""
    async with context.pool.connection() as conn, conn.transaction():
        if not await _still_delivering(conn, invoice.job.invoice_id):
            return
        await park_for_auth(
            conn,
            invoice.head,
            invoice.job.invoice_id,
            AUTH_PROVIDER_SLUG,
            required.account,
            context.admins,
        )


async def _complete(context: GoogleDeliverContext, invoice: _Invoice, values: dict[str, object]) -> None:
    """Close the delivery and, after the commit, delete the local files (R-9.7, BR-D16, BR-D17).

    The state change, the completion message and the photo-path clearing commit together; only
    once they have does the handler unlink the photo, PDF and XML files. A cancellation before
    the commit deletes nothing, and a ``StateMoved`` rolls the transaction back.
    """
    rule = RULE_DONE_NO_PDF if invoice.files.pdf is None else RULE_DONE
    text = MESSAGES[rule].format(
        serie=values["serie"],
        numero=values["numero"],
        razon_social=values["razon_social"],
        total=values["total"],
        link_row=values[LINK_ROW],
        link_folder=values[LINK_FOLDER],
    )
    async with context.pool.connection() as conn, conn.transaction():
        await set_state(conn, invoice.job.invoice_id, DONE, rule_id=rule, expected=DELIVERING)
        await queue_message(
            conn,
            Addressee(invoice.head.origin_channel, invoice.head.profile_id),
            text,
            invoice_id=invoice.job.invoice_id,
            rule_id=rule,
            buttons=[],
        )
        photos = await take_photo_paths(conn, invoice.job.invoice_id)
    # shortcut: the PDF and XML rows keep their local_path pointing at the files deleted here;
    # no later flow A step reads them (R-9.7).
    documents = [path for path in (invoice.pdf_path, invoice.xml_path) if path is not None]
    for path in [*photos, *documents]:
        await asyncio.to_thread(path.unlink, missing_ok=True)


async def _still_delivering(conn: AsyncConnection[Any], invoice_id: int) -> bool:
    """Return True when the invoice is still ``DELIVERING`` inside the caller's transaction."""
    head = await load_invoice_head(conn, invoice_id)
    return head is not None and head.state == DELIVERING
