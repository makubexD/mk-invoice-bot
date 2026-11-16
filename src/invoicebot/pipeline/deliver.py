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
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

from psycopg import AsyncConnection

from invoicebot.auth.msal_cache import AuthRequired
from invoicebot.channels.base import Button
from invoicebot.channels.whatsapp.payloads import button_id
from invoicebot.config.models import (
    Config,
    ExcelTemplateSink,
    Flow,
    MailerSink,
    OneDriveFolderSink,
    Profile,
    Store,
    StoreDirectory,
)
from invoicebot.extract.fields import Reading
from invoicebot.extract.ubl import parse_invoice_xml
from invoicebot.pipeline.actions import NOT_ARRIVED_ACTION, SENT_ACTION
from invoicebot.pipeline.outgoing import Addressee, queue_admin_alert, queue_message
from invoicebot.pipeline.steps import DELIVER_STEP as DELIVER_STEP
from invoicebot.pipeline.steps import FOLDER_STATUS_STEP
from invoicebot.queue.jobs import Job, enqueue
from invoicebot.queue.worker import Handler
from invoicebot.rules.messages_es import BUTTON_NOT_ARRIVED, BUTTON_SENT, MESSAGES, modes_desc
from invoicebot.sinks.base import Attachment, DeliveryContext, Drive, OutgoingMail
from invoicebot.sinks.excel_template import TemplateInvalid, build_excel
from invoicebot.sinks.expr import render
from invoicebot.sinks.folders import comercio_for
from invoicebot.sinks.mailer import Completion, MailMode, build_mail
from invoicebot.sinks.onedrive import GraphError
from invoicebot.sinks.onedrive_folder import file_folder
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
    set_state,
)

logger = logging.getLogger(__name__)

#: The only state this handler acts on; any other leaves the invoice as it is (R-9.4).
DELIVERING_STATE = "DELIVERING"
#: Where the assisted mode leaves the invoice after the draft is ready (R-7.7).
BATCHED_STATE = "BATCHED"
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
#: Where the automatic mode leaves the invoice once the mail is sent (R-7.7).
DONE_STATE = "DONE"
#: BR-D03: the rule that reports a mail that could not be sent.
RULE_SEND_FAILED = "BR-D03"
#: BR-D13: the rule that accepts the ``inbox_rule`` send and offers [No llegó] (R-7.8).
RULE_INBOX = "BR-D13"
#: BR-D03's admin-only alert, with the command the admin runs to reprocess (BR-D03).
RULE_SEND_FAILED_ADMIN = "BR-D03_admin"
#: BR-D01: the rule that parks an invoice until the account signs in (R-7.9).
RULE_AUTH = "BR-D01"
#: BR-D01's admin-only alert, with the script the admin runs (BR-D01).
RULE_AUTH_ADMIN = "BR-D01_admin"
#: BR-D02: the rule that reports an Excel template that cannot be filled (R-7.10).
RULE_TEMPLATE_INVALID = "BR-D02"
#: Where a missing token leaves the invoice until the account signs in (BR-D01).
PENDING_AUTH_STATE = "PENDING_AUTH"
#: Where a broken Excel template leaves the invoice (BR-D02).
MANUAL_REQUIRED_STATE = "MANUAL_REQUIRED"
#: The provider named in the sender's BR-D01 text.
AUTH_PROVIDER_NAME = "Microsoft"
#: The provider's slug in BR-D01's admin text, which names ``scripts/auth_<slug>.py``.
AUTH_PROVIDER_SLUG = "microsoft"
#: The assisted mode's name, used in BR-D07's ``{modes_desc}`` (R-7.7).
DRY_RUN_MODE = "dry_run"

PDF_MIME = "application/pdf"
XML_MIME = "application/xml"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@dataclass(frozen=True)
class _Context:
    """The handler's dependencies, shared by every delivery branch."""

    pool: Pool
    cfg: Config
    directory: StoreDirectory
    drive: Drive
    modes: Mapping[str, MailMode]
    data_dir: Path


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
    seq: int
    mail: OutgoingMail


def make_deliver_handler(
    pool: Pool,
    cfg: Config,
    directory: StoreDirectory,
    drive: Drive,
    modes: Mapping[str, MailMode],
    data_dir: Path,
) -> Handler:
    """Build the handler that files an invoice and hands its mail to the flow's first mode.

    A flow without a OneDrive folder sink is left alone (flow A is ``flow-a``'s). An invoice
    outside ``DELIVERING`` is left untouched (R-9.4). The folder and sequence claim are saved
    before the mail is built, so a retry after a crash reuses them (D88). An automatic mode
    commits its batch as ``sending`` before it sends, so a retry never sends twice (D89).
    """
    context = _Context(pool, cfg, directory, drive, modes, data_dir)

    async def handle(job: Job) -> None:
        started = time.perf_counter()
        invoice = await _load(context, job)
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
        try:
            delivery = await _prepare(context, invoice, sinks, prepared)
            mode_name, mode = _mode(context.modes, sinks.mailer)
            await _COMPLETIONS[mode.completion](context, delivery, prepared, mode)
        except AuthRequired as required:
            await _pending_auth(context, invoice, required.account)
            return
        except TemplateInvalid:
            await _template_invalid(context, invoice)
            return
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


async def _load(context: _Context, job: Job) -> _Invoice | None:
    """Read the invoice's delivery inputs, or return None when it is not ``DELIVERING``."""
    async with context.pool.connection() as conn:
        head = await load_invoice_head(conn, job.invoice_id)
        if head is None or head.state != DELIVERING_STATE:
            return None
        reading = await load_reading(conn, job.invoice_id)
        flow_name = await load_flow(conn, job.invoice_id)
        flow_data = await load_flow_data(conn, job.invoice_id)
        pdf_path = await get_file_path(conn, job.invoice_id, "pdf")
        xml_path = await get_file_path(conn, job.invoice_id, "xml")
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


async def _prepare_values(context: _Context, invoice: _Invoice) -> _Values:
    """Resolve the comercio and the step-6 names, saving the comercio in flow data (R-7.1, R-7.4, BR-R09).

    The sender's chosen brand wins over the serie when it is one of the store's (BR-R09).
    """
    profile = context.cfg.profiles.get(invoice.head.profile_id)
    if profile is None:
        raise RuntimeError(f"invoice {invoice.job.invoice_id} has unknown profile")
    fecha = _fecha(invoice.reading, invoice.xml_reading)
    if fecha is None:
        raise RuntimeError(f"invoice {invoice.job.invoice_id} has no issue date")
    serie = _text(invoice.reading, invoice.xml_reading, "serie")
    numero = _text(invoice.reading, invoice.xml_reading, "numero")
    if serie is None or numero is None:
        raise RuntimeError(f"invoice {invoice.job.invoice_id} lacks its serie or numero")
    ruc_emisor = _text(invoice.reading, invoice.xml_reading, "ruc_emisor")
    razon_social = _text(invoice.reading, invoice.xml_reading, "razon_social") or ""
    store = None if ruc_emisor is None else context.directory.stores.get(ruc_emisor)
    if store is None:
        # shortcut: the PORTAL_MISSING gap does not exist yet, so the slug replaces the store.
        logger.info(
            "shortcut: the issuer has no store; using the slug until the PORTAL_MISSING gap",
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
        values=_values(invoice.reading, invoice.xml_reading, fecha, comercio, overrides, profile.legal_name),
    )


async def _prepare(context: _Context, invoice: _Invoice, sinks: _Sinks, prepared: _Values) -> _Delivery:
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
    async with context.pool.connection() as conn, conn.transaction():
        await merge_flow_data(
            conn,
            invoice.job.invoice_id,
            {"folder": folder.folder, "seq": folder.seq, "folder_path": folder.path},
        )
    excel = await asyncio.to_thread(build_excel, sinks.excel, prepared.values, prepared.overrides)
    await context.drive.upload(f"{folder.path}/{sinks.excel.output}", excel)
    mail = build_mail(
        sinks.mailer,
        prepared.values,
        _attachments(sinks.excel.output, excel, sinks.folder, prepared.values, invoice.pdf, invoice.xml),
    )
    return _Delivery(
        invoice_id=invoice.job.invoice_id,
        head=invoice.head,
        flow_name=invoice.flow_name,
        folder=folder.folder,
        seq=folder.seq,
        mail=mail,
    )


def _mode(modes: Mapping[str, MailMode], mailer: MailerSink) -> tuple[str, MailMode]:
    """Return the flow's first configured mail mode (R-7.7)."""
    name = mailer.modes[0]
    mode = modes.get(name)
    if mode is None:
        raise RuntimeError(f"mail mode {name!r} is not configured")
    return name, mode


async def _assisted(context: _Context, delivery: _Delivery, _prepared: _Values, mode: MailMode) -> None:
    """Write the draft and record the ``prepared`` batch in one transaction (R-7.5, R-7.7, R-8.1)."""
    eml_path = await mode.deliver(delivery.invoice_id, delivery.mail)
    if eml_path is None:
        raise RuntimeError("an assisted mail mode returned no .eml path")
    eml = Path(eml_path)
    if not eml.is_relative_to(context.data_dir):
        raise RuntimeError("an assisted mail mode wrote outside the data directory")
    data = await _read_bytes(eml)
    text = MESSAGES[RULE_BATCHED].format(folder=delivery.folder, modes_desc=modes_desc([DRY_RUN_MODE]))
    button = Button(id=button_id(delivery.invoice_id, SENT_ACTION), title=BUTTON_SENT)
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
        await set_state(conn, delivery.invoice_id, BATCHED_STATE, rule_id=RULE_BATCHED)
        await _notify(conn, delivery.head, delivery.invoice_id, RULE_BATCHED, text, [button])


async def _automatic(
    context: _Context,
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
    async with context.pool.connection() as conn, conn.transaction():
        if await get_batch_state(conn, delivery.invoice_id) == SENDING_BATCH_STATE:
            await _crashed_send(conn, delivery, prepared, context.cfg.profiles)
            return
        batch_id = await open_batch(conn, delivery.invoice_id, delivery.flow_name, SENDING_BATCH_STATE)
        await set_batch_state(conn, batch_id, SENDING_BATCH_STATE)

    try:
        await mode.deliver(delivery.invoice_id, delivery.mail)
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
        await set_state(conn, delivery.invoice_id, DONE_STATE, rule_id=mode.done_rule)
        await _notify(
            conn,
            delivery.head,
            delivery.invoice_id,
            mode.done_rule,
            MESSAGES[mode.done_rule].format(folder=delivery.folder),
            _done_buttons(delivery.invoice_id, mode.done_rule),
        )
        photos = await take_photo_paths(conn, delivery.invoice_id)

    for path in photos:
        await asyncio.to_thread(path.unlink, missing_ok=True)


def _done_buttons(invoice_id: int, done_rule: str) -> list[Button]:
    """Return BR-D13's [No llegó] button, or none for any other done rule (R-7.8)."""
    if done_rule != RULE_INBOX:
        return []
    return [Button(id=button_id(invoice_id, NOT_ARRIVED_ACTION), title=BUTTON_NOT_ARRIVED)]


type _CompletionHandler = Callable[[_Context, _Delivery, _Values, MailMode], Awaitable[None]]

#: The handler each completion uses (D108).
# shortcut: ``receipt`` gets its handler in step 28; no mode reports it yet, so its missing
# key is a programming error and a ``KeyError`` is acceptable.
_COMPLETIONS: Mapping[Completion, _CompletionHandler] = {
    "accepted": _automatic,
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
    await set_state(conn, delivery.invoice_id, BATCHED_STATE, rule_id=RULE_SEND_FAILED)
    await _notify(
        conn,
        delivery.head,
        delivery.invoice_id,
        RULE_SEND_FAILED,
        _send_failed_text(prepared, delivery.invoice_id),
        [],
    )
    await queue_admin_alert(
        conn,
        profiles,
        RULE_SEND_FAILED_ADMIN,
        _send_failed_admin_text(prepared, delivery.invoice_id),
    )


async def _send_failed(
    context: _Context,
    delivery: _Delivery,
    prepared: _Values,
    batch_id: int,
) -> None:
    """Fail the batch and leave the invoice ``BATCHED`` so a reprocess retries it (BR-D03)."""
    async with context.pool.connection() as conn, conn.transaction():
        if not await _still_delivering(conn, delivery.invoice_id):
            return
        await set_batch_state(conn, batch_id, FAILED_BATCH_STATE)
        await set_state(conn, delivery.invoice_id, BATCHED_STATE, rule_id=RULE_SEND_FAILED)
        text = _send_failed_text(prepared, delivery.invoice_id)
        await _notify(conn, delivery.head, delivery.invoice_id, RULE_SEND_FAILED, text, [])
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


async def _pending_auth(context: _Context, invoice: _Invoice, account: str) -> None:
    """Park the invoice until the account signs in and alert its sender and admins (BR-D01)."""
    text = MESSAGES[RULE_AUTH].format(provider=AUTH_PROVIDER_NAME)
    admin_text = MESSAGES[RULE_AUTH_ADMIN].format(provider=AUTH_PROVIDER_SLUG, account=account)
    async with context.pool.connection() as conn, conn.transaction():
        if not await _still_delivering(conn, invoice.job.invoice_id):
            return
        await set_state(conn, invoice.job.invoice_id, PENDING_AUTH_STATE, rule_id=RULE_AUTH)
        await _notify(conn, invoice.head, invoice.job.invoice_id, RULE_AUTH, text, [])
        await queue_admin_alert(conn, context.cfg.profiles, RULE_AUTH_ADMIN, admin_text)


async def _template_invalid(context: _Context, invoice: _Invoice) -> None:
    """Move the invoice to ``MANUAL_REQUIRED`` with BR-D02 (R-7.10)."""
    async with context.pool.connection() as conn, conn.transaction():
        if not await _still_delivering(conn, invoice.job.invoice_id):
            return
        await set_state(conn, invoice.job.invoice_id, MANUAL_REQUIRED_STATE, rule_id=RULE_TEMPLATE_INVALID)
        await _notify(
            conn,
            invoice.head,
            invoice.job.invoice_id,
            RULE_TEMPLATE_INVALID,
            MESSAGES[RULE_TEMPLATE_INVALID],
            [],
        )
    # shortcut: the TEMPLATE_INVALID gap is not filed yet, so a fixed template does not resume it.
    logger.warning(
        "shortcut: the TEMPLATE_INVALID gap is not filed yet",
        extra={"step": DELIVER_STEP, "invoice_id": invoice.job.invoice_id},
    )


async def _still_delivering(conn: AsyncConnection[Any], invoice_id: int) -> bool:
    """Return True when the invoice is still ``DELIVERING`` inside the caller's transaction."""
    head = await load_invoice_head(conn, invoice_id)
    return head is not None and head.state == DELIVERING_STATE


async def _notify(
    conn: AsyncConnection[Any],
    head: InvoiceHead,
    invoice_id: int,
    rule_id: str,
    text: str,
    buttons: Sequence[Button],
) -> None:
    """Queue ``text`` for the invoice's sender in the caller's transaction."""
    await queue_message(
        conn,
        Addressee(head.origin_channel, head.profile_id),
        text,
        invoice_id=invoice_id,
        rule_id=rule_id,
        buttons=buttons,
    )


def _values(
    reading: Reading,
    xml: Reading | None,
    fecha: date,
    comercio: str,
    overrides: Mapping[str, str],
    legal_name: str,
) -> dict[str, object]:
    """Return the step-6 names the folder, cell, subject and body templates render (R-7.1, R-7.4)."""
    amounts = reading if xml is None else xml
    return {
        "fecha": fecha,
        "comercio": comercio,
        "serie": _text(reading, xml, "serie"),
        "numero": _text(reading, xml, "numero"),
        "ruc_emisor": _text(reading, xml, "ruc_emisor"),
        "razon_social": _text(reading, xml, "razon_social"),
        "base": amounts.amount("base"),
        "igv": amounts.amount("igv"),
        "service_charge": amounts.amount("service_charge"),
        "total": amounts.amount("total"),
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


def _text(reading: Reading, xml: Reading | None, name: str) -> str | None:
    """Return ``name`` from the photo's reading, falling back to the XML (R-7.4)."""
    value = reading.value(name)
    return value if value is not None or xml is None else xml.value(name)


def _fecha(reading: Reading, xml: Reading | None) -> date | None:
    """Return the issue date from the reading, falling back to the XML (R-7.1)."""
    fecha = reading.fecha()
    return xml.fecha() if fecha is None and xml is not None else fecha


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
