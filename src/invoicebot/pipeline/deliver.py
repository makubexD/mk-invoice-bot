"""Job handler that files an invoice in its folder and prepares or sends its mail (S11.2).

:func:`make_deliver_handler` builds the handler of the ``deliver`` job step. It acts only on
an invoice in ``DELIVERING`` whose flow has a OneDrive folder sink: it picks the folder and
sequence number, fills the pinned Excel and builds the mail from the step-6 values. The
flow's first mail mode then takes over. The assisted mode writes the ``.eml`` and, in one
transaction, opens a ``prepared`` batch, records the draft file and moves the invoice to
``BATCHED`` with BR-D07 and its [Ya lo envié] button (R-8.1). The automatic mode opens a
``sending`` batch, sends once, then closes the invoice as ``DONE`` with BR-D10 and deletes the
photo. A batch a previous attempt left ``sending`` is not sent again: it is marked ``failed``
and BR-D03 asks the owner to reprocess (D89). A sign-in failure leaves the invoice
``PENDING_AUTH`` with BR-D01, a bad Excel template leaves it ``MANUAL_REQUIRED`` with BR-D02,
and a final Graph error fails the batch and leaves the invoice ``BATCHED`` with BR-D03. A
transient Graph error during an automatic send propagates and leaves the batch ``sending``, so
the retry treats it as a crashed send: the batch fails, BR-D03 reports it, and the mail is
never resent (D89). Before the send (filing, Excel upload) a transient error is simply retried.
A receipt mode follows the same sending-first order, then leaves the batch ``prepared`` with
its laptop job id recorded and no message to the sender (R-7.14, D108). A mailer on
``send: review`` that the owner has not yet reviewed posts the mail to the chat instead:
a ``review`` batch, BR-D19 and the attachments as documents, nothing sent (D194).

:func:`make_resend_handler` builds the two handlers BR-D11's buttons enqueue. Both act on
an invoice that was already delivered, rebuild its delivery with the saved folder and
sequence (D88) and leave its state and batch alone. ``resend_laptop`` writes a new laptop
job, guarded by a ``resending`` marker so a crash never writes a second one; ``resend_eml``
rewrites the draft and answers BR-D07 (R-7.8, R-7.14, D109).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal, cast

from psycopg import AsyncConnection

from invoicebot.auth.msal_cache import AuthRequired
from invoicebot.channels.base import Button, Document, Link
from invoicebot.channels.buttons import button_id
from invoicebot.config.digest import config_digest
from invoicebot.config.models import (
    GMAIL_PROVIDER,
    GRAPH_MODE,
    Config,
    ExcelTemplateSink,
    Extraction,
    Flow,
    MailerSink,
    OneDriveFolderSink,
    Profile,
    Store,
    StoreDirectory,
)
from invoicebot.extract.fields import Reading
from invoicebot.extract.ubl import parse_invoice_xml
from invoicebot.gaps.kinds import fingerprint
from invoicebot.github.issue_text import IssueFields
from invoicebot.pipeline.actions import (
    HOLD_ACTION,
    NOT_ARRIVED_ACTION,
    SEND_ACTION,
    SENT_ACTION,
    send_not_confirmed,
)
from invoicebot.pipeline.done import MAIL_INVOICE_KEY, MAIL_TO_KEY, RULE_DONE, done_text
from invoicebot.pipeline.gaps import GapContext, GapSighting, close_if_fixed, raise_gap
from invoicebot.pipeline.outgoing import (
    Addressee,
    outbound,
    queue_admin_alert,
    queue_document,
    queue_outbound,
    safe_links,
)
from invoicebot.pipeline.pending_auth import (
    GOOGLE,
    MICROSOFT,
    park_for_auth,
)
from invoicebot.pipeline.pending_auth import (
    RULE_AUTH as RULE_AUTH,
)
from invoicebot.pipeline.pending_auth import (
    RULE_AUTH_ADMIN as RULE_AUTH_ADMIN,
)
from invoicebot.pipeline.questions import retry_button
from invoicebot.pipeline.review import (
    DIGEST_KEY,
    REVIEW_BATCH_STATE,
    REVIEWED_KEY,
    RULE_REVIEW,
    mail_digest,
    queue_review_documents,
    remove_review_files,
    review_text,
    write_review_files,
)
from invoicebot.pipeline.steps import DELIVER_STEP as DELIVER_STEP
from invoicebot.pipeline.steps import FOLDER_STATUS_STEP, RESEND_EML_STEP, RESEND_LAPTOP_STEP
from invoicebot.pipeline.summary import review_fields
from invoicebot.pipeline.values import invoice_values
from invoicebot.queue.jobs import Job, enqueue
from invoicebot.queue.outbox import has_pending_document
from invoicebot.queue.worker import Handler
from invoicebot.rules.messages_es import (
    BUTTON_HOLD,
    BUTTON_NOT_ARRIVED,
    BUTTON_SEND,
    BUTTON_SENT,
    KIND_SUBJECT,
    LINK_TITLE_FOLDER,
    MESSAGES,
    modes_desc,
)
from invoicebot.sinks.base import Attachment, DeliveryContext, Drive, FolderLinks, OutgoingMail
from invoicebot.sinks.excel_template import TemplateInvalid, build_excel
from invoicebot.sinks.expr import render
from invoicebot.sinks.folders import comercio_for
from invoicebot.sinks.mailer import Completion, MailMode, build_mail
from invoicebot.sinks.onedrive import GraphError
from invoicebot.sinks.onedrive_folder import file_folder
from invoicebot.states import BATCHED, DELIVERING, DONE, MANUAL_REQUIRED, StateMoved
from invoicebot.store.clock import owner_today
from invoicebot.store.db import Pool
from invoicebot.store.flow_b import (
    claim_seq,
    get_batch_state,
    load_flow_data,
    merge_flow_data,
    open_batch,
    set_batch_state,
    take_photo_paths,
)
from invoicebot.store.invoices import (
    FileRecord,
    InvoiceHead,
    add_file,
    get_file_path,
    load_flow,
    load_invoice_head,
    load_reading,
    lock_invoice,
    set_state,
)
from invoicebot.store.mail_jobs import add_mail_job, waiting_job_age_hours

logger = logging.getLogger(__name__)

#: BR-D07: the rule that reports a filed invoice whose mail waits for the owner.
RULE_BATCHED = "BR-D07"
#: The batch state committed before the automatic mode sends, so a crash never resends (D89).
SENDING_BATCH_STATE: Literal["sending"] = "sending"
#: The batch state of a draft, the only one ``actions._sent`` accepts (R-8.1).
PREPARED_BATCH_STATE: Literal["prepared"] = "prepared"
#: The batch state of a completed automatic send (R-7.5).
SENT_BATCH_STATE = "sent"
#: The batch state of a send that a previous attempt crashed during (D89).
FAILED_BATCH_STATE = "failed"
#: BR-D03: the rule that reports a mail that could not be sent.
RULE_SEND_FAILED = "BR-D03"
#: BR-D12: the rule that tells the sender a waiting laptop job has not gone out yet (R-7.7).
RULE_STALE = "BR-D12"
#: BR-D13: the rule that accepts the ``inbox_rule`` send and offers [No llegó] (R-7.8).
RULE_INBOX = "BR-D13"
#: BR-D03's admin-only alert, with the command the admin runs to reprocess (BR-D03).
RULE_SEND_FAILED_ADMIN = "BR-D03_admin"
#: BR-D02: the rule that reports an Excel template that cannot be filled (R-7.10).
RULE_TEMPLATE_INVALID = "BR-D02"
#: The gap kind of a flow's Excel template that cannot be filled (BR-D02, spec 06 §3).
TEMPLATE_INVALID = "TEMPLATE_INVALID"
#: The provider's slug in BR-D01's admin text, which names ``scripts/auth_<slug>.py``.
AUTH_PROVIDER_SLUG = MICROSOFT
#: Each inbox provider's slug in BR-D01's admin text, naming ``scripts/auth_<slug>.py``.
AUTH_PROVIDER_SLUGS: dict[str, str] = {GRAPH_MODE: MICROSOFT, GMAIL_PROVIDER: GOOGLE}
#: The assisted mode's name, used in BR-D07's ``{modes_desc}`` (R-7.7).
DRY_RUN_MODE = "dry_run"
#: The laptop mode's name, the one a flow lists as ``laptop_outlook`` (R-7.14).
LAPTOP_OUTLOOK_MODE = "laptop_outlook"
#: The flow-data key a laptop resend sets before its upload. ``False`` clears it, because
#: :func:`merge_flow_data` merges and cannot delete a key (D89).
RESENDING_KEY = "resending"
#: The flow-data key of the invoice folder's browser link, saved so a retry reuses it (D194).
FOLDER_URL_KEY = "folder_url"
#: The invoice states a resend job acts on: delivered, not still in flight (R-7.8, R-7.14).
RESEND_STATES = frozenset({DONE, BATCHED})

PDF_MIME = "application/pdf"
XML_MIME = "application/xml"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
EML_MIME = "message/rfc822"


@dataclass(frozen=True)
class DeliverContext:
    """The handler's dependencies, shared by every delivery branch.

    ``laptop_flows`` names the flows whose mailer lists ``laptop_outlook``; a BR-D11
    resend offers [Por la laptop] only to an invoice in one of them (D109). ``links``
    reads the invoice folder's browser link for the delivery messages (D194).
    """

    pool: Pool
    cfg: Config
    directory: StoreDirectory
    drive: Drive
    links: FolderLinks
    modes: Mapping[str, MailMode]
    data_dir: Path
    laptop_flows: frozenset[str]


@dataclass(frozen=True)
class _Invoice:
    """One invoice's delivery inputs, read before any external call."""

    job: Job
    head: InvoiceHead
    reading: Reading
    flow_name: str
    flow: Flow
    flow_data: Mapping[str, Any]
    pdf: bytes
    xml: bytes | None
    xml_reading: Reading | None
    today: date


@dataclass(frozen=True)
class _Sinks:
    """The three flow B sinks a delivery needs, in configuration order."""

    folder: OneDriveFolderSink
    excel: ExcelTemplateSink
    mailer: MailerSink


@dataclass(frozen=True)
class _Values:
    """The step-6 values and the date and overrides the templates render from (R-7.1, R-7.4)."""

    fecha: date
    values: Mapping[str, object]
    overrides: Mapping[str, str]


@dataclass(frozen=True)
class _Delivery:
    """What the mail mode receives: the filed invoice and its built mail (R-7.6)."""

    invoice_id: int
    head: InvoiceHead
    flow_name: str
    folder: str
    folder_path: str
    folder_url: str
    seq: int
    mail: OutgoingMail


def make_deliver_handler(context: DeliverContext) -> Handler:
    """Build the handler that files an invoice and hands its mail to the flow's first mode.

    A flow without a OneDrive folder sink is left alone (flow A is ``flow-a``'s). An invoice
    outside ``DELIVERING`` is left untouched (R-9.4). The folder and sequence claim are saved
    before the mail is built, so a retry after a crash reuses them (D88). An automatic mode
    commits its batch as ``sending`` before it sends, so a retry never sends twice (D89).
    """

    async def handle(job: Job) -> None:
        started = time.perf_counter()
        invoice = await _load(context, job, {DELIVERING})
        if invoice is None:
            return
        sinks = _sinks(invoice.flow)
        if sinks is None:
            logger.info(
                "step skipped",
                extra={"step": DELIVER_STEP, "invoice_id": job.invoice_id, "reason": "no_folder_sink"},
            )
            return
        prepared = await _prepare_values(context, invoice)
        result = await _handled(
            context, invoice, _deliver(context, invoice, sinks, prepared), sinks.excel.kind
        )
        if result is None:
            return
        delivery, mode_name = result
        logger.info(
            "step done",
            extra={
                "step": DELIVER_STEP,
                "invoice_id": job.invoice_id,
                "mode": mode_name,
                "folder_seq": delivery.seq,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            },
        )

    return handle


def make_resend_handler(context: DeliverContext) -> Mapping[str, Handler]:
    """Build the handlers of the two BR-D11 resend steps (R-7.8, R-7.14, D109).

    Both accept a ``DONE`` or ``BATCHED`` invoice, rebuild its delivery with the saved
    folder and sequence (D88) and leave its state and batch alone. ``resend_laptop``
    writes a new laptop job unless a ``resending`` marker shows the last write died, in
    which case BR-D11 asks the sender again. ``resend_eml`` rewrites the draft and
    answers BR-D07. A flow without a folder sink is left alone, as in the deliver step.
    """
    return {
        RESEND_LAPTOP_STEP: _resend_handler(context, _resend_laptop),
        RESEND_EML_STEP: _resend_handler(context, _resend_eml),
    }


def _resend_handler(
    context: DeliverContext,
    run: Callable[[DeliverContext, _Invoice, _Delivery, _Values], Awaitable[None]],
) -> Handler:
    """Build one resend step's handler around the action that does its work (R-7.8, R-7.14)."""

    async def handle(job: Job) -> None:
        invoice = await _load(context, job, RESEND_STATES)
        if invoice is None:
            return
        sinks = _sinks(invoice.flow)
        if sinks is None:
            logger.info(
                "step skipped",
                extra={"step": job.step, "invoice_id": job.invoice_id, "reason": "no_folder_sink"},
            )
            return
        prepared = await _prepare_values(context, invoice)
        try:
            delivery = await _handled(
                context, invoice, _resend(context, invoice, sinks, prepared, run), sinks.excel.kind
            )
        except StateMoved:
            # D95: a resend runs on a DONE or BATCHED invoice, so a sign-in failure's park
            # (guarded on DELIVERING) is refused and rolled back; the invoice keeps its state.
            return
        if delivery is None:
            return
        logger.info(
            "step done",
            extra={"step": job.step, "invoice_id": job.invoice_id, "folder_seq": delivery.seq},
        )

    return handle


async def _deliver(
    context: DeliverContext,
    invoice: _Invoice,
    sinks: _Sinks,
    prepared: _Values,
) -> tuple[_Delivery, str]:
    """Prepare the delivery and hand it to the flow's first mail mode (R-7.7).

    A mailer on ``send: review`` posts the mail for review instead, as does ``if_doubtful``
    when the reading has a doubtful or missing field (D203), until the owner's
    [Enviar] has set ``reviewed`` in the flow data and the mail rebuilt now is the mail the
    owner saw, by its digest (D194). Once the mode's completion has committed, the send, the
    draft or the laptop job holds the attachments, so the review files are removed, unless a
    review document is still waiting to send: that keeps them for the daily housekeeping
    sweep (D195). A reviewed delivery clears ``reviewed`` so the next delivery posts a new
    review (D194).
    """
    delivery = await _prepare(context, invoice, sinks, prepared)
    held = _held(sinks.mailer, invoice.reading, context.cfg.extraction, invoice.today)
    reviewed = held is not None
    if held is not None and not _was_reviewed(invoice.flow_data, delivery.mail):
        await _review(context, delivery, prepared, held)
        return delivery, REVIEW_BATCH_STATE
    mode_name, mode = _mode(context.modes, sinks.mailer)
    await _COMPLETIONS[mode.completion](context, delivery, prepared, mode)
    async with context.pool.connection() as conn:
        waiting = await has_pending_document(conn, delivery.invoice_id, RULE_REVIEW)
    if not waiting:
        await remove_review_files(context.data_dir, delivery.invoice_id)
    if reviewed:
        async with context.pool.connection() as conn, conn.transaction():
            await merge_flow_data(conn, delivery.invoice_id, {REVIEWED_KEY: False})
    return delivery, mode_name


def _held(
    mailer: MailerSink, reading: Reading, extraction: Extraction, today: date
) -> tuple[str, ...] | None:
    """Return the doubtful field names of a mail held for review, or None when it is not held (D203).

    ``review`` always holds, with no names; ``auto`` never does. ``if_doubtful`` holds
    when the summary's review set (D203) is not empty or a required field is missing,
    and names those fields.
    """
    if mailer.send == "auto":
        return None
    if mailer.send == "review":
        return ()
    names = review_fields(reading, extraction, today)
    names += tuple(name for name in reading.missing_required() if name not in names)
    return names or None


def _was_reviewed(flow_data: Mapping[str, Any], mail: OutgoingMail) -> bool:
    """Return whether the owner pressed [Enviar] on exactly this mail (D194)."""
    return flow_data.get(REVIEWED_KEY) is True and flow_data.get(DIGEST_KEY) == mail_digest(mail)


async def _resend(
    context: DeliverContext,
    invoice: _Invoice,
    sinks: _Sinks,
    prepared: _Values,
    run: Callable[[DeliverContext, _Invoice, _Delivery, _Values], Awaitable[None]],
) -> _Delivery:
    """Rebuild the invoice's delivery and run the step's resend action (R-7.8, R-7.14)."""
    delivery = await _prepare(context, invoice, sinks, prepared)
    await run(context, invoice, delivery, prepared)
    return delivery


async def _resend_laptop(
    context: DeliverContext, invoice: _Invoice, delivery: _Delivery, prepared: _Values
) -> None:
    """Write a new laptop job for the mail the sender chose to resend (R-7.14, D109).

    A ``resending`` marker already in the flow data means a previous attempt died
    mid-write: the marker is cleared and BR-D11 asks the sender again instead of
    writing a second job (D89). An invoice whose newest laptop job has no receipt
    yet already has a claim, so no marker and no job are written; BR-D12 tells the
    sender how long it has waited instead (R-7.7). Otherwise the marker is committed
    before the upload, so a retry after a crash takes that same path, and one
    transaction stores the ``mail_jobs`` row and clears the marker. The receipt
    answers; nothing is sent. A laptop mode that is missing or does not complete by
    receipt (the dry run when sending is off) writes nothing and answers BR-D11 as a
    flow without a laptop mode does (R-7.10, R-7.14).

    shortcut: a crash after the upload and before ``add_mail_job`` leaves an orphan
    job file whose receipt is ignored, because its job id is unknown.
    """
    mode = context.modes.get(LAPTOP_OUTLOOK_MODE)
    if mode is None or mode.completion != "receipt":
        # No laptop flows: re-offering [Por la laptop] here would loop the sender.
        async with context.pool.connection() as conn, conn.transaction():
            await send_not_confirmed(conn, invoice.job.invoice_id, frozenset())
        return
    if invoice.flow_data.get(RESENDING_KEY) is True:
        async with context.pool.connection() as conn, conn.transaction():
            await merge_flow_data(conn, invoice.job.invoice_id, {RESENDING_KEY: False})
            await send_not_confirmed(conn, invoice.job.invoice_id, context.laptop_flows)
        return
    async with context.pool.connection() as conn, conn.transaction():
        hours = await waiting_job_age_hours(conn, invoice.job.invoice_id)
        if hours is not None:
            await _notify(
                conn,
                invoice.head,
                invoice.job.invoice_id,
                _Reply(
                    RULE_STALE,
                    MESSAGES[RULE_STALE].format(
                        serie=prepared.values["serie"],
                        numero=prepared.values["numero"],
                        hours=hours,
                    ),
                    [],
                ),
            )
            return
        await merge_flow_data(conn, invoice.job.invoice_id, {RESENDING_KEY: True})
    try:
        job_id = await mode.deliver(delivery.invoice_id, delivery.mail, delivery.folder_path)
    except AuthRequired:
        # The token is fetched before the upload, so no job was written: clear the marker so
        # the next [Por la laptop] writes it instead of treating it as a crashed write.
        async with context.pool.connection() as conn, conn.transaction():
            await merge_flow_data(conn, delivery.invoice_id, {RESENDING_KEY: False})
        raise
    if job_id is None:
        raise RuntimeError("a receipt mail mode returned no job id")
    async with context.pool.connection() as conn, conn.transaction():
        await add_mail_job(conn, job_id, delivery.invoice_id)
        await merge_flow_data(conn, delivery.invoice_id, {RESENDING_KEY: False})


async def _resend_eml(
    context: DeliverContext, _invoice: _Invoice, delivery: _Delivery, prepared: _Values
) -> None:
    """Rewrite the draft, queue it as a document, then answer BR-D07 (R-7.8, D109, D194).

    The button is [Ya lo envié] only when prepared.

    Rewriting the ``.eml`` is harmless, so this step keeps no marker and changes no
    invoice or batch state; a ``prepared`` batch keeps its button, a ``sent`` one has
    nothing left to confirm.
    """
    mode = _named_mode(context.modes, DRY_RUN_MODE)
    eml_path = await mode.deliver(delivery.invoice_id, delivery.mail, delivery.folder_path)
    if eml_path is None:
        raise RuntimeError("an assisted mail mode returned no .eml path")
    if not Path(eml_path).is_relative_to(context.data_dir):
        raise RuntimeError("an assisted mail mode wrote outside the data directory")
    text, button = _draft_reply(delivery)
    size = (await asyncio.to_thread(Path(eml_path).stat)).st_size
    draft = _draft_document(Path(eml_path), prepared, size)
    async with context.pool.connection() as conn, conn.transaction():
        awaiting = await get_batch_state(conn, delivery.invoice_id) == PREPARED_BATCH_STATE
        await _queue_draft(conn, delivery, draft)
        await _notify(
            conn,
            delivery.head,
            delivery.invoice_id,
            _Reply(RULE_BATCHED, text, [button] if awaiting else [], _folder_links(delivery)),
        )


async def _handled[T](
    context: DeliverContext, invoice: _Invoice, work: Awaitable[T], sink_name: str
) -> T | None:
    """Await ``work``, parking or manually filing the invoice on its two known failures.

    A sign-in failure leaves the invoice ``PENDING_AUTH`` with BR-D01 and a bad Excel
    template leaves it ``MANUAL_REQUIRED`` with BR-D02. Those handlers act only on a
    ``DELIVERING`` invoice, so a resend merely stops (R-7.9, R-7.10).
    """
    try:
        return await work
    except AuthRequired as required:
        await _pending_auth(context, invoice, required.account)
    except TemplateInvalid:
        await _template_invalid(context, invoice, sink_name)
    return None


async def _load(context: DeliverContext, job: Job, states: Collection[str]) -> _Invoice | None:
    """Read the invoice's delivery inputs, or return None when its state is not in ``states``."""
    async with context.pool.connection() as conn:
        head = await load_invoice_head(conn, job.invoice_id)
        if head is None or head.state not in states:
            return None
        reading = await load_reading(conn, job.invoice_id)
        flow_name = await load_flow(conn, job.invoice_id)
        flow_data = await load_flow_data(conn, job.invoice_id)
        pdf_path = await get_file_path(conn, job.invoice_id, "pdf")
        xml_path = await get_file_path(conn, job.invoice_id, "xml")
        today = await owner_today(conn, context.cfg.owner.timezone)
    if reading is None or flow_name is None:
        raise RuntimeError(f"invoice {job.invoice_id} lacks its reading or flow")
    flow = context.cfg.flows.get(flow_name)
    if flow is None:
        raise RuntimeError(f"invoice {job.invoice_id} has unknown flow {flow_name!r}")
    if pdf_path is None:
        raise RuntimeError(f"invoice {job.invoice_id} has no PDF to deliver")
    pdf = await _read_bytes(Path(pdf_path))
    xml = None if xml_path is None else await _read_bytes(Path(xml_path))
    xml_reading = None if xml is None else parse_invoice_xml(xml)
    return _Invoice(
        job=job,
        head=head,
        reading=reading,
        flow_name=flow_name,
        flow=flow,
        flow_data=flow_data,
        pdf=pdf,
        xml=xml,
        xml_reading=xml_reading,
        today=today,
    )


def _sinks(flow: Flow) -> _Sinks | None:
    """Return the flow's folder, Excel and mailer sinks, or None without a folder sink."""
    folder: OneDriveFolderSink | None = None
    excel: ExcelTemplateSink | None = None
    mailer: MailerSink | None = None
    for sink in flow.sinks:
        if isinstance(sink, OneDriveFolderSink):
            folder = sink
        elif isinstance(sink, ExcelTemplateSink):
            excel = sink
        elif isinstance(sink, MailerSink):
            mailer = sink
    if folder is None:
        return None
    if excel is None or mailer is None:
        raise RuntimeError("a flow with a folder sink needs an excel_template and a mailer sink")
    return _Sinks(folder=folder, excel=excel, mailer=mailer)


async def _prepare_values(context: DeliverContext, invoice: _Invoice) -> _Values:
    """Resolve the comercio and the step-6 names, saving the comercio in flow data (R-7.1, R-7.4, BR-R09).

    The sender's chosen brand wins over the serie when it is one of the store's (BR-R09).
    """
    profile = context.cfg.profiles.get(invoice.head.profile_id)
    if profile is None:
        raise RuntimeError(f"invoice {invoice.job.invoice_id} has unknown profile")
    values = invoice_values(invoice.reading, invoice.xml_reading, None)
    fecha = cast(date | None, values["fecha"])
    if fecha is None:
        raise RuntimeError(f"invoice {invoice.job.invoice_id} has no issue date")
    serie = cast(str | None, values["serie"])
    numero = cast(str | None, values["numero"])
    if serie is None or numero is None:
        raise RuntimeError(f"invoice {invoice.job.invoice_id} lacks its serie or numero")
    ruc_emisor = cast(str | None, values["ruc_emisor"])
    razon_social = cast(str | None, values["razon_social"]) or ""
    store = None if ruc_emisor is None else context.directory.stores.get(ruc_emisor)
    if store is None:
        logger.debug(
            "the issuer has no store; the slug stands in for the store name",
            extra={"step": DELIVER_STEP, "invoice_id": invoice.job.invoice_id},
        )
    overrides = _overrides(invoice.flow_data)
    comercio = _comercio(store, invoice.flow_data, serie, razon_social)
    if comercio is None:
        raise RuntimeError(f"invoice {invoice.job.invoice_id} has no comercio")
    async with context.pool.connection() as conn, conn.transaction():
        await merge_flow_data(conn, invoice.job.invoice_id, {"comercio": comercio})
    return _Values(
        fecha=fecha,
        overrides=overrides,
        values=_values(values, comercio, overrides, profile.legal_name),
    )


async def _prepare(context: DeliverContext, invoice: _Invoice, sinks: _Sinks, prepared: _Values) -> _Delivery:
    """File the folder, upload the Excel and build the mail (R-7.1, R-7.2, R-7.4, R-7.6)."""

    async def claim(listed_max: int) -> int:
        async with context.pool.connection() as conn, conn.transaction():
            return await claim_seq(
                conn,
                invoice.flow_name,
                prepared.fecha.year,
                prepared.fecha.month,
                listed_max,
                invoice.job.invoice_id,
            )

    folder = await file_folder(
        sinks.folder,
        context.drive,
        DeliveryContext(
            invoice_id=invoice.job.invoice_id,
            values=prepared.values,
            pdf=invoice.pdf,
            xml=invoice.xml,
            claim_seq=claim,
            saved=invoice.flow_data,
        ),
    )
    saved_url = invoice.flow_data.get(FOLDER_URL_KEY)
    folder_url = (
        saved_url
        if isinstance(saved_url, str) and saved_url
        else await _read_folder_url(context.links, folder.path, invoice.job.invoice_id)
    )
    async with context.pool.connection() as conn, conn.transaction():
        await merge_flow_data(
            conn,
            invoice.job.invoice_id,
            {
                "folder": folder.folder,
                "seq": folder.seq,
                "folder_path": folder.path,
                FOLDER_URL_KEY: folder_url,
            },
        )
    excel = await asyncio.to_thread(build_excel, sinks.excel, prepared.values, prepared.overrides)
    await context.drive.upload(f"{folder.path}/{sinks.excel.output}", excel)
    mail = build_mail(
        sinks.mailer,
        prepared.values,
        _attachments(sinks.excel.output, excel, sinks.folder, prepared.values, invoice.pdf, invoice.xml),
    )
    async with context.pool.connection() as conn, conn.transaction():
        await merge_flow_data(
            conn,
            invoice.job.invoice_id,
            {
                MAIL_TO_KEY: list(mail.to),
                MAIL_INVOICE_KEY: [prepared.values["serie"], prepared.values["numero"]],
            },
        )
    return _Delivery(
        invoice_id=invoice.job.invoice_id,
        head=invoice.head,
        flow_name=invoice.flow_name,
        folder=folder.folder,
        folder_path=folder.path,
        folder_url=folder_url,
        seq=folder.seq,
        mail=mail,
    )


async def _read_folder_url(links: FolderLinks, path: str, invoice_id: int) -> str:
    """Return the folder's browser link, or an empty one when Graph has no usable link (D194).

    A missing folder or a ``webUrl`` that is absent or not HTTPS is final and only costs the
    link, so the filed delivery goes on and ``safe_links`` drops the empty URL. A sign-in
    failure and a transient error still propagate.
    """
    try:
        return await links.web_url(path)
    except GraphError as error:
        logger.warning(
            "the folder link could not be read; the message goes without it",
            extra={"step": DELIVER_STEP, "invoice_id": invoice_id, "status": error.status},
        )
        return ""


def _mode(modes: Mapping[str, MailMode], mailer: MailerSink) -> tuple[str, MailMode]:
    """Return the flow's first configured mail mode (R-7.7)."""
    name = mailer.modes[0]
    return name, _named_mode(modes, name)


def _named_mode(modes: Mapping[str, MailMode], name: str) -> MailMode:
    """Return the configured mode called ``name``, or raise when the build lacks it."""
    mode = modes.get(name)
    if mode is None:
        raise RuntimeError(f"mail mode {name!r} is not configured")
    return mode


def _draft_document(eml: Path, prepared: _Values, size: int) -> Document:
    """Describe the written draft as the document the chat receives (D194)."""
    name = f"{prepared.values['serie']}-{prepared.values['numero']}.eml"
    return Document(path=str(eml.resolve()), filename=name, mime=EML_MIME, size=size)


async def _queue_draft(conn: AsyncConnection[Any], delivery: _Delivery, draft: Document) -> None:
    """Queue the draft before BR-D07, in the caller's transaction, so its buttons come last (D194)."""
    await queue_document(
        conn,
        Addressee(delivery.head.origin_channel, delivery.head.profile_id),
        draft,
        caption=draft.filename,
        invoice_id=delivery.invoice_id,
        rule_id=RULE_BATCHED,
    )


async def _assisted(context: DeliverContext, delivery: _Delivery, prepared: _Values, mode: MailMode) -> None:
    """Write the draft and record the ``prepared`` batch in one transaction (R-7.5, R-7.7, R-8.1).

    The same transaction queues the draft as a document, then BR-D07 with [Ya lo envié] (D194).
    """
    eml_path = await mode.deliver(delivery.invoice_id, delivery.mail, delivery.folder_path)
    if eml_path is None:
        raise RuntimeError("an assisted mail mode returned no .eml path")
    eml = Path(eml_path)
    if not eml.is_relative_to(context.data_dir):
        raise RuntimeError("an assisted mail mode wrote outside the data directory")
    data = await _read_bytes(eml)
    text, button = _draft_reply(delivery)
    draft = _draft_document(eml, prepared, len(data))
    async with context.pool.connection() as conn, conn.transaction():
        if not await _still_delivering(conn, delivery.invoice_id):
            return
        batch_id = await open_batch(conn, delivery.invoice_id, delivery.flow_name, PREPARED_BATCH_STATE)
        await set_batch_state(conn, batch_id, PREPARED_BATCH_STATE)
        await add_file(
            conn,
            delivery.invoice_id,
            FileRecord(
                kind="eml",
                local_path=str(eml),
                remote_ref=None,
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
            ),
        )
        await set_state(conn, delivery.invoice_id, BATCHED, rule_id=RULE_BATCHED)
        await _queue_draft(conn, delivery, draft)
        await _notify(
            conn,
            delivery.head,
            delivery.invoice_id,
            _Reply(RULE_BATCHED, text, [button], _folder_links(delivery)),
        )
        await _close_fixed_gap(conn, context, delivery.invoice_id)


async def _review(
    context: DeliverContext, delivery: _Delivery, prepared: _Values, doubtful: Sequence[str]
) -> None:
    """Post the built mail for the owner to check, sending nothing (D194, D203, D88, D89).

    The attachments are written first; a retry rewrites them, which is harmless. One
    transaction then opens the ``review`` batch (reusing an earlier ``prepared``,
    ``failed`` or ``review`` one), moves the invoice to ``BATCHED`` with BR-D19, stores
    the mail's digest, clears ``reviewed`` and queues the attachments as documents, then
    BR-D19 with [Enviar], [Cancelar] and the folder link. A retry after that commit finds
    the invoice ``BATCHED`` and the handler does nothing. A batch left ``sending`` is never
    posted or sent: it fails with BR-D03 and a reprocess starts over (D89).
    """
    invoice_id = delivery.invoice_id
    documents = await write_review_files(context.data_dir, invoice_id, delivery.mail.attachments)
    buttons = [
        Button(id=button_id(invoice_id, SEND_ACTION), title=BUTTON_SEND),
        Button(id=button_id(invoice_id, HOLD_ACTION), title=BUTTON_HOLD),
    ]
    async with context.pool.connection() as conn, conn.transaction():
        if not await _still_delivering(conn, invoice_id):
            return
        if await get_batch_state(conn, invoice_id) == SENDING_BATCH_STATE:
            await _crashed_send(conn, delivery, prepared, context.cfg.profiles)
            return
        batch_id = await open_batch(conn, invoice_id, delivery.flow_name, REVIEW_BATCH_STATE)
        await set_batch_state(conn, batch_id, REVIEW_BATCH_STATE)
        await merge_flow_data(conn, invoice_id, {DIGEST_KEY: mail_digest(delivery.mail), REVIEWED_KEY: False})
        await set_state(conn, invoice_id, BATCHED, rule_id=RULE_REVIEW)
        addressee = Addressee(delivery.head.origin_channel, delivery.head.profile_id)
        await queue_review_documents(conn, addressee, invoice_id, documents)
        text = review_text(prepared.values, delivery.mail, doubtful)
        await _notify(
            conn, delivery.head, invoice_id, _Reply(RULE_REVIEW, text, buttons, _folder_links(delivery))
        )
        await _close_fixed_gap(conn, context, invoice_id)


def _draft_reply(delivery: _Delivery) -> tuple[str, Button]:
    """Return BR-D07's text and its [Ya lo envié] button for a ready draft (R-8.1)."""
    text = MESSAGES[RULE_BATCHED].format(folder=delivery.folder, modes_desc=modes_desc([DRY_RUN_MODE]))
    button = Button(id=button_id(delivery.invoice_id, SENT_ACTION), title=BUTTON_SENT)
    return text, button


async def _automatic(
    context: DeliverContext,
    delivery: _Delivery,
    prepared: _Values,
    mode: MailMode,
) -> None:
    """Send the mail once and close the delivery, or report a crashed send (R-7.5, D89).

    The first transaction records the batch as ``sending`` before the external call; an
    existing batch may be ``failed`` or ``prepared``, so its state is always set again. A
    batch already ``sending`` means a previous attempt died mid-send, so it is never sent
    again: the batch fails and BR-D03 asks the owner to reprocess it (D89). A transient Graph
    error during the send propagates and leaves the batch ``sending``, so the retry treats it
    as a crashed send: the batch fails, BR-D03 reports it, and the mail is never resent (D89).
    A batch the send set ``sent`` also enqueues the ``folder_status`` job (R-7.11), in the
    same transaction as that state change.
    Before the send (filing, Excel upload) a transient error is simply retried.
    """
    batch_id = await _begin_send(context, delivery, prepared)
    if batch_id is None:
        return

    try:
        await mode.deliver(delivery.invoice_id, delivery.mail, delivery.folder_path)
    except GraphError:
        await _send_failed(context, delivery, prepared, batch_id)
        return
    except AuthRequired:
        # The token is fetched before anything is sent, so the mail did not go out: free the
        # batch so the delivery resumed after re-auth sends it instead of treating it as crashed.
        async with context.pool.connection() as conn, conn.transaction():
            await set_batch_state(conn, batch_id, FAILED_BATCH_STATE)
        raise

    async with context.pool.connection() as conn, conn.transaction():
        await set_batch_state(conn, batch_id, SENT_BATCH_STATE)
        await enqueue(conn, delivery.invoice_id, FOLDER_STATUS_STEP)
        if not await _still_delivering(conn, delivery.invoice_id):
            return
        await set_state(conn, delivery.invoice_id, DONE, rule_id=mode.done_rule)
        await _notify(
            conn,
            delivery.head,
            delivery.invoice_id,
            _Reply(
                mode.done_rule,
                await _done_reply_text(conn, delivery, mode.done_rule),
                _done_buttons(delivery.invoice_id, mode.done_rule),
                _folder_links(delivery),
            ),
        )
        await _close_fixed_gap(conn, context, delivery.invoice_id)
        photos = await take_photo_paths(conn, delivery.invoice_id)

    for path in photos:
        await asyncio.to_thread(path.unlink, missing_ok=True)


async def _done_reply_text(conn: AsyncConnection[Any], delivery: _Delivery, rule_id: str) -> str:
    """Return the completion text: BR-D10 names the invoice and the To address (D195), the rest the folder."""
    if rule_id == RULE_DONE:
        return await done_text(conn, delivery.invoice_id)
    return MESSAGES[rule_id].format(folder=delivery.folder)


async def _handed_off(
    context: DeliverContext,
    delivery: _Delivery,
    prepared: _Values,
    mode: MailMode,
) -> None:
    """Write the laptop job and leave the batch ``prepared`` until its receipt (R-7.14, D108).

    The batch is committed as ``sending`` before the job file is written, so a retry after a
    crash finds it ``sending``, fails it and never writes a second job the laptop would send
    twice (D89). A final Graph error on the upload fails the batch and leaves the invoice
    ``BATCHED`` with BR-D03, as the automatic mode does. One transaction then records the
    batch ``prepared``, stores the job id in ``mail_jobs`` and moves the invoice to
    ``BATCHED``; no message is queued, because the receipt completes the batch later
    (R-7.14). The photo stays until the mail is sent.
    """
    batch_id = await _begin_send(context, delivery, prepared)
    if batch_id is None:
        return

    try:
        job_id = await mode.deliver(delivery.invoice_id, delivery.mail, delivery.folder_path)
    except GraphError:
        await _send_failed(context, delivery, prepared, batch_id)
        return
    except AuthRequired:
        # The token is fetched before the upload, so no job was written: free the batch so the
        # delivery resumed after re-auth writes it instead of treating it as crashed.
        async with context.pool.connection() as conn, conn.transaction():
            await set_batch_state(conn, batch_id, FAILED_BATCH_STATE)
        raise
    if job_id is None:
        raise RuntimeError("a receipt mail mode returned no job id")

    async with context.pool.connection() as conn, conn.transaction():
        await set_batch_state(conn, batch_id, PREPARED_BATCH_STATE)
        await add_mail_job(conn, job_id, delivery.invoice_id)
        if not await _still_delivering(conn, delivery.invoice_id):
            return
        await set_state(conn, delivery.invoice_id, BATCHED, rule_id=RULE_BATCHED)
        await _close_fixed_gap(conn, context, delivery.invoice_id)


async def _begin_send(context: DeliverContext, delivery: _Delivery, prepared: _Values) -> int | None:
    """Commit the batch as ``sending`` before the external call and return its id (D89).

    The invoice's row lock serializes this check against any other job of the same invoice,
    such as a leased-out job and the one [Enviar] queued, so only one of them can pass it.
    Returns None when the invoice left ``DELIVERING`` meanwhile, and when a previous attempt
    left the batch ``sending``: that one is failed with BR-D03 and never sent again.
    """
    async with context.pool.connection() as conn, conn.transaction():
        await lock_invoice(conn, delivery.invoice_id)
        if not await _still_delivering(conn, delivery.invoice_id):
            return None
        if await get_batch_state(conn, delivery.invoice_id) == SENDING_BATCH_STATE:
            await _crashed_send(conn, delivery, prepared, context.cfg.profiles)
            return None
        batch_id = await open_batch(conn, delivery.invoice_id, delivery.flow_name, SENDING_BATCH_STATE)
        await set_batch_state(conn, batch_id, SENDING_BATCH_STATE)
        return batch_id


async def _close_fixed_gap(conn: AsyncConnection[Any], context: DeliverContext, invoice_id: int) -> None:
    """Close the invoice's fixed template gap now that delivery succeeded (R-11.9)."""
    await close_if_fixed(conn, invoice_id, TEMPLATE_INVALID, context.cfg.gaps.github.repo)


def _done_buttons(invoice_id: int, done_rule: str) -> list[Button]:
    """Return BR-D13's [No llegó] button, or none for any other done rule (R-7.8)."""
    if done_rule != RULE_INBOX:
        return []
    return [Button(id=button_id(invoice_id, NOT_ARRIVED_ACTION), title=BUTTON_NOT_ARRIVED)]


type _CompletionHandler = Callable[[DeliverContext, _Delivery, _Values, MailMode], Awaitable[None]]

#: The handler each completion uses (D108).
_COMPLETIONS: Mapping[Completion, _CompletionHandler] = {
    "accepted": _automatic,
    "receipt": _handed_off,
    "tap": _assisted,
}


async def _crashed_send(
    conn: AsyncConnection[Any],
    delivery: _Delivery,
    prepared: _Values,
    profiles: Mapping[str, Profile],
) -> None:
    """Fail the batch a previous attempt left ``sending`` and ask the owner to reprocess (D89)."""
    if not await _still_delivering(conn, delivery.invoice_id):
        return
    batch_id = await open_batch(conn, delivery.invoice_id, delivery.flow_name, SENDING_BATCH_STATE)
    await set_batch_state(conn, batch_id, FAILED_BATCH_STATE)
    await merge_flow_data(conn, delivery.invoice_id, {REVIEWED_KEY: False})
    await set_state(conn, delivery.invoice_id, BATCHED, rule_id=RULE_SEND_FAILED)
    await _notify(
        conn,
        delivery.head,
        delivery.invoice_id,
        _Reply(
            RULE_SEND_FAILED,
            _send_failed_text(prepared, delivery.invoice_id),
            [retry_button(delivery.invoice_id)],
        ),
    )
    await queue_admin_alert(
        conn,
        profiles,
        RULE_SEND_FAILED_ADMIN,
        _send_failed_admin_text(prepared, delivery.invoice_id),
    )


async def _send_failed(
    context: DeliverContext,
    delivery: _Delivery,
    prepared: _Values,
    batch_id: int,
) -> None:
    """Fail the batch and leave the invoice ``BATCHED`` so a reprocess retries it (BR-D03)."""
    async with context.pool.connection() as conn, conn.transaction():
        if not await _still_delivering(conn, delivery.invoice_id):
            return
        await set_batch_state(conn, batch_id, FAILED_BATCH_STATE)
        await merge_flow_data(conn, delivery.invoice_id, {REVIEWED_KEY: False})
        await set_state(conn, delivery.invoice_id, BATCHED, rule_id=RULE_SEND_FAILED)
        text = _send_failed_text(prepared, delivery.invoice_id)
        await _notify(
            conn,
            delivery.head,
            delivery.invoice_id,
            _Reply(RULE_SEND_FAILED, text, [retry_button(delivery.invoice_id)]),
        )
        await queue_admin_alert(
            conn,
            context.cfg.profiles,
            RULE_SEND_FAILED_ADMIN,
            _send_failed_admin_text(prepared, delivery.invoice_id),
        )


def _send_failed_text(prepared: _Values, invoice_id: int) -> str:
    """Return BR-D03's text for the invoice's serie, numero and id (BR-D03)."""
    return MESSAGES[RULE_SEND_FAILED].format(
        serie=prepared.values["serie"],
        numero=prepared.values["numero"],
        id=invoice_id,
    )


def _send_failed_admin_text(prepared: _Values, invoice_id: int) -> str:
    """Return BR-D03's admin alert for the invoice's id, serie and numero (BR-D03)."""
    return MESSAGES[RULE_SEND_FAILED_ADMIN].format(
        id=invoice_id,
        serie=prepared.values["serie"],
        numero=prepared.values["numero"],
    )


async def _pending_auth(context: DeliverContext, invoice: _Invoice, account: str) -> None:
    """Park the invoice until the account signs in and alert its sender and admins (BR-D01)."""
    async with context.pool.connection() as conn, conn.transaction():
        await park_for_auth(
            conn,
            invoice.head,
            invoice.job.invoice_id,
            AUTH_PROVIDER_SLUG,
            account,
            context.cfg.profiles,
        )


async def _template_invalid(context: DeliverContext, invoice: _Invoice, sink_name: str) -> None:
    """Move the invoice to ``MANUAL_REQUIRED`` with BR-D02 and raise the template gap (R-7.10, R-11.2)."""
    async with context.pool.connection() as conn, conn.transaction():
        if not await _still_delivering(conn, invoice.job.invoice_id):
            return
        await set_state(conn, invoice.job.invoice_id, MANUAL_REQUIRED, rule_id=RULE_TEMPLATE_INVALID)
        await _notify(
            conn,
            invoice.head,
            invoice.job.invoice_id,
            _Reply(RULE_TEMPLATE_INVALID, MESSAGES[RULE_TEMPLATE_INVALID], []),
        )
        await raise_gap(
            conn,
            GapContext(profiles=context.cfg.profiles, gaps_cfg=context.cfg.gaps),
            _template_sighting(invoice, sink_name),
        )


def _template_sighting(invoice: _Invoice, sink_name: str) -> GapSighting:
    """Build one TEMPLATE_INVALID sighting from the flow and failing sink (BR-D02, R-11.12, D157)."""
    ruc_emisor = invoice.reading.value("ruc_emisor")
    sink = next((entry for entry in invoice.flow.sinks if entry.kind == sink_name), None)
    return GapSighting(
        kind=TEMPLATE_INVALID,
        parts=(invoice.flow_name, sink_name),
        subject=KIND_SUBJECT[TEMPLATE_INVALID].format(flow=invoice.flow_name, sink=sink_name),
        invoice_id=invoice.job.invoice_id,
        fields=IssueFields(
            kind=TEMPLATE_INVALID,
            fingerprint=fingerprint(TEMPLATE_INVALID, invoice.flow_name, sink_name),
            ruc_emisor=None if ruc_emisor is None else int(ruc_emisor),
            razon_social=invoice.reading.value("razon_social"),
            tipo=invoice.reading.value("tipo"),
            reasons=(),
            portal_key=None,
            hint_url=None,
        ),
        raised_with=None if sink is None else config_digest(sink),
    )


async def _still_delivering(conn: AsyncConnection[Any], invoice_id: int) -> bool:
    """Return True when the invoice is still ``DELIVERING`` inside the caller's transaction."""
    head = await load_invoice_head(conn, invoice_id)
    return head is not None and head.state == DELIVERING


@dataclass(frozen=True)
class _Reply:
    """One chat message: its rule, text, buttons and links (D194)."""

    rule_id: str
    text: str
    buttons: Sequence[Button]
    links: Sequence[Link] = ()


async def _notify(
    conn: AsyncConnection[Any],
    head: InvoiceHead,
    invoice_id: int,
    reply: _Reply,
) -> None:
    """Queue ``reply`` for the invoice's sender in the caller's transaction (D194)."""
    message = outbound(
        Addressee(head.origin_channel, head.profile_id),
        reply.text,
        rule_id=reply.rule_id,
        buttons=reply.buttons,
        links=reply.links,
    )
    await queue_outbound(conn, message, invoice_id=invoice_id)


def _folder_links(delivery: _Delivery) -> list[Link]:
    """Return the invoice folder's link for a delivery message (D194)."""
    return safe_links([(LINK_TITLE_FOLDER, delivery.folder_url)])


def _values(
    base: Mapping[str, object],
    comercio: str,
    overrides: Mapping[str, str],
    legal_name: str,
) -> dict[str, object]:
    """Return the step-6 names the folder, cell, subject and body templates render (R-7.1, R-7.4)."""
    return {
        **base,
        "comercio": comercio,
        "motivo": overrides.get("motivo"),
        "descripcion": overrides.get("descripcion"),
        "profile": {"legal_name": legal_name},
    }


def _attachments(
    excel_name: str,
    excel: bytes,
    folder: OneDriveFolderSink,
    values: Mapping[str, object],
    pdf: bytes,
    xml: bytes | None,
) -> dict[str, Attachment]:
    """Return the mail's attachments by the names the mailer sink can attach (R-7.6)."""
    files = {
        "excel_template": Attachment(excel_name, excel, XLSX_MIME),
        "pdf": Attachment(render(folder.files.pdf, values), pdf, PDF_MIME),
    }
    if xml is not None:
        files["xml"] = Attachment(render(folder.files.xml, values), xml, XML_MIME)
    return files


def _comercio(store: Store | None, flow_data: Mapping[str, Any], serie: str, razon_social: str) -> str | None:
    """Return the sender's chosen brand when it is the store's, else the serie's comercio (BR-R09)."""
    raw_answers = flow_data.get("answers")
    answers: Mapping[str, Any] = raw_answers if isinstance(raw_answers, dict) else {}
    chosen = answers.get("comercio")
    if isinstance(chosen, str) and store is not None and any(b.comercio == chosen for b in store.brands):
        return chosen
    return comercio_for(store, serie, razon_social)


def _overrides(flow_data: Mapping[str, Any]) -> dict[str, str]:
    """Return the sender's stored overrides as text, or nothing when absent (R-7.4)."""
    raw = flow_data.get("overrides")
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


async def _read_bytes(path: Path) -> bytes:
    """Read ``path`` off the event loop; PDFs and XMLs are capped at the boundary (R-9.2)."""
    return await asyncio.to_thread(path.read_bytes)
