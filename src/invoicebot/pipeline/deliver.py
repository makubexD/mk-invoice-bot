"""Job handler that files an invoice in its folder and prepares or sends its mail (S11.2).

:func:`make_deliver_handler` builds the handler of the ``deliver`` job step. It acts only on
an invoice in ``DELIVERING`` whose flow has a OneDrive folder sink: it picks the folder and
sequence number, fills the pinned Excel and builds the mail from the step-6 values. The
flow's first mail mode then takes over. The assisted mode writes the ``.eml`` and, in one
transaction, opens a ``prepared`` batch, records the draft file and moves the invoice to
``BATCHED`` with BR-D07 and its [Ya lo envié] button (R-8.1). The automatic mode is not wired
yet.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection

from invoicebot.channels.base import Button, OutboundMessage
from invoicebot.channels.whatsapp.payloads import button_id
from invoicebot.config.models import (
    Config,
    ExcelTemplateSink,
    Flow,
    MailerSink,
    OneDriveFolderSink,
    StoreDirectory,
)
from invoicebot.extract.fields import Reading
from invoicebot.extract.ubl import parse_invoice_xml
from invoicebot.queue import outbox
from invoicebot.queue.jobs import Job
from invoicebot.queue.worker import Handler
from invoicebot.rules.messages_es import MESSAGES, SENT_BUTTON_TITLE, modes_desc
from invoicebot.sinks.base import Attachment, DeliveryContext, Drive, OutgoingMail
from invoicebot.sinks.excel_template import build_excel
from invoicebot.sinks.expr import render
from invoicebot.sinks.folders import comercio_for
from invoicebot.sinks.mailer import MailMode, build_mail
from invoicebot.sinks.onedrive_folder import file_folder
from invoicebot.store.db import Pool
from invoicebot.store.flow_b import claim_seq, load_flow_data, merge_flow_data, open_batch
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

#: The job step this handler runs.
DELIVER_STEP = "deliver"
#: The only state this handler acts on; any other leaves the invoice as it is (R-9.4).
DELIVERING_STATE = "DELIVERING"
#: Where the assisted mode leaves the invoice after the draft is ready (R-7.7).
BATCHED_STATE = "BATCHED"
#: BR-D07: the rule that reports a filed invoice whose mail waits for the owner.
RULE_BATCHED = "BR-D07"
#: The assisted mode's name, used in BR-D07's ``{modes_desc}`` (R-7.7).
DRY_RUN_MODE = "dry_run"
#: The button action that confirms the owner sent the draft (R-7.8, R-8.1).
SENT_ACTION = "sent"

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
    before the mail is built, so a retry after a crash reuses them (D88).
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
        delivery = await _prepare(context, invoice, sinks, prepared)
        mode_name, mode = _mode(context.modes, sinks.mailer)
        if mode.automatic:
            # shortcut: part B adds the automatic graph path (batch sending/sent, D89).
            raise NotImplementedError(f"mail mode {mode_name!r} arrives with part B")
        await _assisted(context, delivery, mode)
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
    """Resolve the comercio and the step-6 names, saving the comercio in flow data (R-7.1, R-7.4)."""
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
    comercio = comercio_for(store, serie, razon_social)
    if comercio is None:
        raise RuntimeError(f"invoice {invoice.job.invoice_id} has no comercio")
    overrides = _overrides(invoice.flow_data)
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
            xml=invoice.xml or b"",
            claim_seq=claim,
            saved=invoice.flow_data,
        ),
    )
    async with context.pool.connection() as conn, conn.transaction():
        await merge_flow_data(conn, invoice.job.invoice_id, {"folder": folder.folder, "seq": folder.seq})
    excel = build_excel(sinks.excel, prepared.values, prepared.overrides)
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


async def _assisted(context: _Context, delivery: _Delivery, mode: MailMode) -> None:
    """Write the draft and record the ``prepared`` batch in one transaction (R-7.5, R-7.7, R-8.1)."""
    eml_path = await mode.deliver(delivery.invoice_id, delivery.mail)
    if eml_path is None:
        raise RuntimeError("an assisted mail mode returned no .eml path")
    eml = Path(eml_path)
    if not eml.is_relative_to(context.data_dir):
        raise RuntimeError("an assisted mail mode wrote outside the data directory")
    data = await _read_bytes(eml)
    text = MESSAGES[RULE_BATCHED].format(folder=delivery.folder, modes_desc=modes_desc([DRY_RUN_MODE]))
    button = Button(id=button_id(delivery.invoice_id, SENT_ACTION), title=SENT_BUTTON_TITLE)
    async with context.pool.connection() as conn, conn.transaction():
        if not await _still_delivering(conn, delivery.invoice_id):
            return
        await open_batch(conn, delivery.invoice_id, delivery.flow_name, "prepared")
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
        await _notify(conn, delivery.head, RULE_BATCHED, text, [button])


async def _still_delivering(conn: AsyncConnection[Any], invoice_id: int) -> bool:
    """Return True when the invoice is still ``DELIVERING`` inside the caller's transaction."""
    head = await load_invoice_head(conn, invoice_id)
    return head is not None and head.state == DELIVERING_STATE


async def _notify(
    conn: AsyncConnection[Any],
    head: InvoiceHead,
    rule_id: str,
    text: str,
    buttons: Sequence[Button],
) -> None:
    """Queue ``text`` for the invoice's sender in the caller's transaction."""
    message = OutboundMessage(
        channel=head.origin_channel,
        recipient=head.profile_id,
        profile_id=head.profile_id,
        rule_id=rule_id,
        text=text,
        buttons=list(buttons),
    )
    await outbox.add(conn, head.origin_channel, head.profile_id, message.model_dump(mode="json"))


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


def _overrides(flow_data: Mapping[str, Any]) -> dict[str, str]:
    """Return the sender's stored overrides as text, or nothing when absent (R-7.4)."""
    raw = flow_data.get("overrides")
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


async def _read_bytes(path: Path) -> bytes:
    """Read ``path`` off the event loop; PDFs and XMLs are capped at the boundary (R-9.2)."""
    return await asyncio.to_thread(path.read_bytes)
