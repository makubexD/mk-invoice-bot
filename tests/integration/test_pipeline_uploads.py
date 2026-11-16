"""Integration tests for the attach-upload job handler (S7.5, R-1.5, R-5.6, BR-D09, D66, D101).

Postgres is real and a fake ``MediaSource`` serves each channel's uploads.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import Attachment, AttachmentKind, ChannelName, DownloadedMedia, InboundMessage
from invoicebot.channels.whatsapp.client import WhatsAppError
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, MediaLimits, MediaMaxBytes
from invoicebot.extract.fields import Field, Reading
from invoicebot.pipeline.steps import ATTACH_STEP
from invoicebot.pipeline.uploads import make_attach_handler
from invoicebot.queue.jobs import Job
from invoicebot.rules.messages_es import MESSAGES, UPLOAD_XML_INVALID, UPLOAD_XML_MISMATCH
from invoicebot.store.invoices import (
    FileKind,
    FileRecord,
    add_file,
    create_invoice,
    get_file_path,
    save_reading,
    set_flow,
    set_state,
)
from invoicebot.store.messages import link_message_to_invoice, store_inbound

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
FLOW = "work_microsoft"
# From tests/synthetic_rucs.txt; no real RUC appears here.
STORE_RUC = "20000000001"
SERIE = "F001"
NUMERO = "15160"
MEGABYTE = 1024 * 1024
PDF_BYTES = b"%PDF-1.4 synthetic invoice\n"
XML_BYTES = b"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2">
  <ID>F001-15160</ID>
  <IssueDate>2026-11-05</IssueDate>
  <InvoiceTypeCode>01</InvoiceTypeCode>
  <AccountingSupplierParty>
    <Party>
      <PartyIdentification><ID>20000000001</ID></PartyIdentification>
      <PartyLegalEntity><RegistrationName>Restaurante La Lena S.A.C.</RegistrationName></PartyLegalEntity>
    </Party>
  </AccountingSupplierParty>
  <TaxTotal><TaxAmount>7.61</TaxAmount></TaxTotal>
  <LegalMonetaryTotal>
    <LineExtensionAmount>40.00</LineExtensionAmount>
    <ChargeTotalAmount>4.00</ChargeTotalAmount>
    <PayableAmount>51.61</PayableAmount>
  </LegalMonetaryTotal>
</Invoice>
"""
NEWER_XML_BYTES = XML_BYTES.replace(b"</Invoice>", b"<!-- newer upload -->\n</Invoice>")
MISMATCH_XML_BYTES = XML_BYTES.replace(b"F001-15160", b"F001-99999")
DOCTYPE_XML_BYTES = b'<?xml version="1.0"?>\n<!DOCTYPE Invoice>\n<Invoice/>'
MIME: dict[AttachmentKind, str] = {
    "pdf": "application/pdf",
    "xml": "application/xml",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "photo": "image/png",
    "captcha": "image/png",
}


def _config() -> Config:
    """Return the example config with the work flow requiring the PDF and the XML."""
    cfg = load_config(EXAMPLE)
    flow = cfg.flows[FLOW].model_copy(
        update={"required_docs": ["xml", "pdf"], "manual_uploads": ["pdf", "xml"]}
    )
    return cfg.model_copy(update={"flows": {**cfg.flows, FLOW: flow}})


def _limits() -> MediaLimits:
    """Limits with synthetic byte caps larger than every test body."""
    return MediaLimits(
        max_megapixels=40,
        max_bytes=MediaMaxBytes(
            image=8 * MEGABYTE,
            pdf=16 * MEGABYTE,
            xml=2 * MEGABYTE,
            zip=4 * MEGABYTE,
        ),
        zip_max_entries=20,
        zip_max_ratio=50,
    )


def _field(value: str) -> Field:
    return Field(value=value, source="qr", confidence=1.0)


def _reading() -> Reading:
    """Build the synthetic reading a stuck invoice keeps."""
    return Reading(
        fields={
            "ruc_emisor": _field(STORE_RUC),
            "tipo": _field("01"),
            "serie": _field(SERIE),
            "numero": _field(NUMERO),
            "total": _field("51.61"),
            "fecha_emision": _field("2026-11-05"),
        },
        document_url=None,
    )


class _FakeMediaSource:
    """A channel's media source over the synthetic bodies the test serves (R-1.5)."""

    def __init__(
        self,
        media: dict[str, tuple[str, bytes]],
        counts: dict[str, int] | None = None,
        statuses: dict[str, int] | None = None,
    ) -> None:
        self.media = media
        self.counts = counts
        self.statuses = statuses
        self.refs: list[str] = []

    async def download(
        self, media_ref: str, kind: AttachmentKind, out_dir: Path, limits: MediaLimits
    ) -> DownloadedMedia:
        """Write the synthetic body for ``media_ref`` and return its path, hash and size."""
        self.refs.append(media_ref)
        if self.counts is not None:
            self.counts[media_ref] = self.counts.get(media_ref, 0) + 1
        if self.statuses is not None and media_ref in self.statuses:
            raise WhatsAppError(status=self.statuses[media_ref])
        mime, body = self.media[media_ref]
        directory = out_dir / media_ref
        directory.mkdir(parents=True)
        path = directory / "file"
        path.write_bytes(body)
        return DownloadedMedia(
            path=path,
            sha256=hashlib.sha256(body).hexdigest(),
            size=len(body),
            mime=mime,
        )


async def _invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    state: str = "MANUAL_REQUIRED",
    channel: ChannelName = "whatsapp",
) -> int:
    """Create a routed invoice with a stored reading in ``state`` and return its id."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, channel)
        await save_reading(conn, invoice_id, _reading())
        await set_flow(conn, invoice_id, FLOW)
        rule_id = "BR-F07" if state == "MANUAL_REQUIRED" else None
        await set_state(conn, invoice_id, state, rule_id=rule_id)
    return invoice_id


@dataclass(frozen=True)
class _Upload:
    """One synthetic inbound upload."""

    kind: AttachmentKind
    media_ref: str
    body: bytes
    received_at: datetime


async def _link_upload(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    invoice_id: int,
    profile_id: str,
    upload: _Upload,
    channel: ChannelName = "whatsapp",
) -> None:
    """Store one inbound upload and link it to the invoice, as intake does (R-1.5)."""
    msg = InboundMessage(
        channel=channel,
        external_id=f"ext-{uuid4().hex}",
        profile_id=profile_id,
        sender_role="owner",
        attachments=[
            Attachment(
                kind=upload.kind,
                media_ref=upload.media_ref,
                mime=MIME[upload.kind],
                size=len(upload.body),
            )
        ],
        received_at=upload.received_at,
    )
    async with pool.connection() as conn, conn.transaction():
        assert await store_inbound(conn, msg)
        await link_message_to_invoice(conn, msg.channel, msg.external_id, invoice_id)


async def _store_doc(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    invoice_id: int,
    kind: FileKind,
) -> None:
    """Store a file of ``kind`` as an earlier upload did, so the invoice has every required doc."""
    async with pool.connection() as conn, conn.transaction():
        await add_file(
            conn,
            invoice_id,
            FileRecord(
                kind=kind,
                local_path=str(tmp_path / f"synthetic-{kind}"),
                remote_ref=f"ref-{uuid4().hex}",
                sha256="0" * 64,
                size=1,
            ),
        )


def _job(invoice_id: int, job_id: int = 1) -> Job:
    """Build the attach job the handler receives for ``invoice_id``."""
    return Job(
        id=job_id,
        invoice_id=invoice_id,
        step=ATTACH_STEP,
        attempts=0,
        crashes=0,
        lease_until=datetime.now(UTC),
    )


@dataclass(frozen=True)
class _RunOptions:
    """What one handler run overrides: the config, the fake source's extras and its channels."""

    cfg: Config | None = None
    counts: dict[str, int] | None = None
    statuses: dict[str, int] | None = None
    channels: tuple[ChannelName, ...] = ("whatsapp",)


async def _run(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    invoice_id: int,
    media: dict[str, tuple[str, bytes]],
    job_id: int = 1,
    options: _RunOptions | None = None,
) -> None:
    """Run the attach handler once, serving ``media`` from the fake sources."""
    opts = options or _RunOptions()
    sources = {channel: _FakeMediaSource(media, opts.counts, opts.statuses) for channel in opts.channels}
    handler = make_attach_handler(pool, opts.cfg or _config(), sources, tmp_path, _limits())
    await handler(_job(invoice_id, job_id))


async def _state(conn: AsyncConnection[Any], invoice_id: int) -> str:
    cur = await conn.execute("SELECT state FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0])


async def _files(conn: AsyncConnection[Any], invoice_id: int) -> list[tuple[str, str, str | None]]:
    """Return ``(kind, local_path, remote_ref)`` of the invoice's files, oldest first."""
    cur = await conn.execute(
        "SELECT kind, local_path, remote_ref FROM files WHERE invoice_id = %s ORDER BY id",
        (invoice_id,),
    )
    return [
        (str(kind), str(path), None if ref is None else str(ref)) for kind, path, ref in await cur.fetchall()
    ]


async def _outbox(conn: AsyncConnection[Any], profile_id: str) -> list[tuple[str, str | None]]:
    cur = await conn.execute(
        """
        SELECT payload_json->>'text', payload_json->>'rule_id'
        FROM outbox
        WHERE "to" = %s
        ORDER BY id
        """,
        (profile_id,),
    )
    return [(str(text), None if rule is None else str(rule)) for text, rule in await cur.fetchall()]


async def _deliver_jobs(conn: AsyncConnection[Any], invoice_id: int) -> int:
    cur = await conn.execute(
        "SELECT count(*) FROM jobs WHERE invoice_id = %s AND step = 'deliver'",
        (invoice_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def test_pdf_then_xml_in_two_messages_missing_then_delivering(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-1.5, BR-D09, D66: a PDF says what is missing; the following XML moves the invoice on."""
    profile_id = f"uploads-{uuid4().hex}"
    invoice_id = await _invoice(db_pool, profile_id)
    pdf_ref = f"ref-{uuid4().hex}"
    await _link_upload(
        db_pool,
        invoice_id,
        profile_id,
        _Upload("pdf", pdf_ref, PDF_BYTES, datetime(2026, 11, 14, 10, tzinfo=UTC)),
    )

    await _run(db_pool, tmp_path, invoice_id, {pdf_ref: (MIME["pdf"], PDF_BYTES)})

    async with db_pool.connection() as conn:
        state = await _state(conn, invoice_id)
        files = await _files(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert state == "MANUAL_REQUIRED"
    assert [kind for kind, _path, _ref in files] == ["pdf"]
    assert outbox == [(f"Recibí el PDF. Aún falta el XML para completar {SERIE}-{NUMERO}.", "BR-D09")]

    xml_ref = f"ref-{uuid4().hex}"
    await _link_upload(
        db_pool,
        invoice_id,
        profile_id,
        _Upload("xml", xml_ref, XML_BYTES, datetime(2026, 11, 14, 10, 5, tzinfo=UTC)),
    )

    await _run(db_pool, tmp_path, invoice_id, {xml_ref: (MIME["xml"], XML_BYTES)}, job_id=2)

    async with db_pool.connection() as conn:
        state = await _state(conn, invoice_id)
        files = await _files(conn, invoice_id)
        deliver_jobs = await _deliver_jobs(conn, invoice_id)
    assert state == "DELIVERING"
    assert [kind for kind, _path, _ref in files] == ["pdf", "xml"]
    assert files[1][2] == xml_ref
    assert deliver_jobs == 1


async def test_mismatching_xml_replies_br_f06_and_stores_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-5.6, BR-F06, D101: an XML for another invoice is removed and answered, none stored."""
    profile_id = f"uploads-{uuid4().hex}"
    invoice_id = await _invoice(db_pool, profile_id)
    media_ref = f"ref-{uuid4().hex}"
    await _link_upload(
        db_pool,
        invoice_id,
        profile_id,
        _Upload("xml", media_ref, MISMATCH_XML_BYTES, datetime(2026, 11, 14, 10, tzinfo=UTC)),
    )

    await _run(db_pool, tmp_path, invoice_id, {media_ref: (MIME["xml"], MISMATCH_XML_BYTES)})

    async with db_pool.connection() as conn:
        state = await _state(conn, invoice_id)
        files = await _files(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert state == "MANUAL_REQUIRED"
    assert files == []
    assert outbox == [(UPLOAD_XML_MISMATCH.format(serie=SERIE, numero=NUMERO), "BR-F06")]


async def test_xml_with_a_doctype_replies_br_f09(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-5.6, BR-F09, D101: an XML with a DOCTYPE is removed and answered, none stored."""
    profile_id = f"uploads-{uuid4().hex}"
    invoice_id = await _invoice(db_pool, profile_id)
    media_ref = f"ref-{uuid4().hex}"
    await _link_upload(
        db_pool,
        invoice_id,
        profile_id,
        _Upload("xml", media_ref, DOCTYPE_XML_BYTES, datetime(2026, 11, 14, 10, tzinfo=UTC)),
    )

    await _run(db_pool, tmp_path, invoice_id, {media_ref: (MIME["xml"], DOCTYPE_XML_BYTES)})

    async with db_pool.connection() as conn:
        state = await _state(conn, invoice_id)
        files = await _files(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert state == "MANUAL_REQUIRED"
    assert files == []
    assert outbox == [(UPLOAD_XML_INVALID, "BR-F09")]


async def test_html_uploaded_as_a_pdf_replies_br_d15_and_stores_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D103, BR-D15: an HTML body uploaded as a pdf is removed and answered once, none stored."""
    profile_id = f"uploads-{uuid4().hex}"
    invoice_id = await _invoice(db_pool, profile_id)
    media_ref = f"ref-{uuid4().hex}"
    html_bytes = b"<!doctype html><html><body>synthetic</body></html>"
    await _link_upload(
        db_pool,
        invoice_id,
        profile_id,
        _Upload("pdf", media_ref, html_bytes, datetime(2026, 11, 14, 10, tzinfo=UTC)),
    )

    await _run(db_pool, tmp_path, invoice_id, {media_ref: (MIME["pdf"], html_bytes)})

    async with db_pool.connection() as conn:
        state = await _state(conn, invoice_id)
        files = await _files(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert state == "MANUAL_REQUIRED"
    assert files == []
    assert outbox == [(MESSAGES["BR-D15"], "BR-D15")]


async def test_xlsx_attachment_is_ignored(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-1.5, D66: a kind the flow does not accept is never downloaded or stored."""
    profile_id = f"uploads-{uuid4().hex}"
    invoice_id = await _invoice(db_pool, profile_id)
    await _link_upload(
        db_pool,
        invoice_id,
        profile_id,
        _Upload(
            "xlsx",
            f"ref-{uuid4().hex}",
            b"synthetic xlsx bytes",
            datetime(2026, 11, 14, 10, tzinfo=UTC),
        ),
    )

    await _run(db_pool, tmp_path, invoice_id, {})

    async with db_pool.connection() as conn:
        state = await _state(conn, invoice_id)
        files = await _files(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
        deliver_jobs = await _deliver_jobs(conn, invoice_id)
    assert state == "MANUAL_REQUIRED"
    assert files == []
    assert outbox == []
    assert deliver_jobs == 0


async def test_running_the_same_job_twice_stores_the_file_once(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-1.5, D101: a redelivered job finds no unattached upload and writes nothing new."""
    profile_id = f"uploads-{uuid4().hex}"
    invoice_id = await _invoice(db_pool, profile_id)
    media_ref = f"ref-{uuid4().hex}"
    await _link_upload(
        db_pool,
        invoice_id,
        profile_id,
        _Upload("pdf", media_ref, PDF_BYTES, datetime(2026, 11, 14, 10, tzinfo=UTC)),
    )
    media = {media_ref: (MIME["pdf"], PDF_BYTES)}

    await _run(db_pool, tmp_path, invoice_id, media)
    await _run(db_pool, tmp_path, invoice_id, media)

    async with db_pool.connection() as conn:
        files = await _files(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert [kind for kind, _path, _ref in files] == ["pdf"]
    assert len(outbox) == 1


async def test_invoice_not_manual_required_is_left_alone(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-1.5, D101: an invoice past MANUAL_REQUIRED gets no files, no job and no reply."""
    profile_id = f"uploads-{uuid4().hex}"
    invoice_id = await _invoice(db_pool, profile_id, state="DELIVERING")
    media_ref = f"ref-{uuid4().hex}"
    await _link_upload(
        db_pool,
        invoice_id,
        profile_id,
        _Upload("pdf", media_ref, PDF_BYTES, datetime(2026, 11, 14, 10, tzinfo=UTC)),
    )

    await _run(db_pool, tmp_path, invoice_id, {media_ref: (MIME["pdf"], PDF_BYTES)})

    async with db_pool.connection() as conn:
        state = await _state(conn, invoice_id)
        files = await _files(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
        deliver_jobs = await _deliver_jobs(conn, invoice_id)
    assert state == "DELIVERING"
    assert files == []
    assert outbox == []
    assert deliver_jobs == 0


async def test_two_xml_uploads_leave_the_newest_for_delivery(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-1.5, D101: with two XML uploads, delivery reads the newest one."""
    profile_id = f"uploads-{uuid4().hex}"
    invoice_id = await _invoice(db_pool, profile_id)
    older_ref = f"ref-{uuid4().hex}"
    newer_ref = f"ref-{uuid4().hex}"
    await _link_upload(
        db_pool,
        invoice_id,
        profile_id,
        _Upload("xml", older_ref, XML_BYTES, datetime(2026, 11, 14, 10, tzinfo=UTC)),
    )
    await _link_upload(
        db_pool,
        invoice_id,
        profile_id,
        _Upload("xml", newer_ref, NEWER_XML_BYTES, datetime(2026, 11, 14, 10, 5, tzinfo=UTC)),
    )

    await _run(
        db_pool,
        tmp_path,
        invoice_id,
        {
            older_ref: (MIME["xml"], XML_BYTES),
            newer_ref: (MIME["xml"], NEWER_XML_BYTES),
        },
    )

    async with db_pool.connection() as conn:
        rows = await _files(conn, invoice_id)
        delivered = await get_file_path(conn, invoice_id, "xml")
    assert [ref for _kind, _path, ref in rows] == [older_ref, newer_ref]
    assert rows[0][1] != rows[1][1]
    newer_bytes = await asyncio.to_thread(Path(rows[1][1]).read_bytes)
    assert hashlib.sha256(newer_bytes).hexdigest() == hashlib.sha256(NEWER_XML_BYTES).hexdigest()
    assert delivered == rows[1][1]


async def test_rejected_xml_with_every_doc_present_stays_manual_required(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D103: a rejected XML on a complete invoice answers once and queues no deliver job."""
    profile_id = f"uploads-{uuid4().hex}"
    invoice_id = await _invoice(db_pool, profile_id)
    await _store_doc(db_pool, tmp_path, invoice_id, "xml")
    await _store_doc(db_pool, tmp_path, invoice_id, "pdf")
    media_ref = f"ref-{uuid4().hex}"
    await _link_upload(
        db_pool,
        invoice_id,
        profile_id,
        _Upload("xml", media_ref, DOCTYPE_XML_BYTES, datetime(2026, 11, 14, 10, tzinfo=UTC)),
    )

    await _run(db_pool, tmp_path, invoice_id, {media_ref: (MIME["xml"], DOCTYPE_XML_BYTES)})

    async with db_pool.connection() as conn:
        state = await _state(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
        deliver_jobs = await _deliver_jobs(conn, invoice_id)
    assert state == "MANUAL_REQUIRED"
    assert outbox == [(UPLOAD_XML_INVALID, "BR-F09")]
    assert deliver_jobs == 0


async def test_upload_kind_outside_the_flow_stores_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D103: a kind the flow does not accept on a complete invoice sends nothing and moves nowhere."""
    profile_id = f"uploads-{uuid4().hex}"
    invoice_id = await _invoice(db_pool, profile_id)
    await _store_doc(db_pool, tmp_path, invoice_id, "xml")
    await _store_doc(db_pool, tmp_path, invoice_id, "pdf")
    media_ref = f"ref-{uuid4().hex}"
    await _link_upload(
        db_pool,
        invoice_id,
        profile_id,
        _Upload("xml", media_ref, XML_BYTES, datetime(2026, 11, 14, 10, tzinfo=UTC)),
    )
    base = _config()
    flow = base.flows[FLOW].model_copy(update={"manual_uploads": ["pdf"]})
    cfg = base.model_copy(update={"flows": {**base.flows, FLOW: flow}})

    await _run(
        db_pool,
        tmp_path,
        invoice_id,
        {media_ref: (MIME["xml"], XML_BYTES)},
        options=_RunOptions(cfg=cfg),
    )

    async with db_pool.connection() as conn:
        state = await _state(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
        deliver_jobs = await _deliver_jobs(conn, invoice_id)
    assert state == "MANUAL_REQUIRED"
    assert outbox == []
    assert deliver_jobs == 0


async def test_second_attach_job_does_not_repeat_an_xml_rejection(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D103: a second attach job answers BR-F09 once and downloads the rejected XML once."""
    profile_id = f"uploads-{uuid4().hex}"
    invoice_id = await _invoice(db_pool, profile_id)
    media_ref = f"ref-{uuid4().hex}"
    await _link_upload(
        db_pool,
        invoice_id,
        profile_id,
        _Upload("xml", media_ref, DOCTYPE_XML_BYTES, datetime(2026, 11, 14, 10, tzinfo=UTC)),
    )
    media = {media_ref: (MIME["xml"], DOCTYPE_XML_BYTES)}
    counts: dict[str, int] = {}

    await _run(db_pool, tmp_path, invoice_id, media, options=_RunOptions(counts=counts))
    await _run(db_pool, tmp_path, invoice_id, media, job_id=2, options=_RunOptions(counts=counts))

    async with db_pool.connection() as conn:
        outbox = await _outbox(conn, profile_id)
    assert outbox == [(UPLOAD_XML_INVALID, "BR-F09")]
    # The first job downloads it once; the second job finds the rejection and downloads nothing.
    assert counts[media_ref] == 1


async def test_media_info_404_is_skipped_and_a_valid_pdf_is_stored(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-1.5: one broken media ref does not stop the run storing a valid PDF beside it."""
    profile_id = f"uploads-{uuid4().hex}"
    invoice_id = await _invoice(db_pool, profile_id)
    broken_ref = f"ref-{uuid4().hex}"
    pdf_ref = f"ref-{uuid4().hex}"
    await _link_upload(
        db_pool,
        invoice_id,
        profile_id,
        _Upload("xml", broken_ref, XML_BYTES, datetime(2026, 11, 14, 10, tzinfo=UTC)),
    )
    await _link_upload(
        db_pool,
        invoice_id,
        profile_id,
        _Upload("pdf", pdf_ref, PDF_BYTES, datetime(2026, 11, 14, 10, 5, tzinfo=UTC)),
    )

    await _run(
        db_pool,
        tmp_path,
        invoice_id,
        {pdf_ref: (MIME["pdf"], PDF_BYTES)},
        options=_RunOptions(statuses={broken_ref: 404}),
    )

    async with db_pool.connection() as conn:
        state = await _state(conn, invoice_id)
        files = await _files(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert state == "MANUAL_REQUIRED"
    assert [(kind, ref) for kind, _path, ref in files] == [("pdf", pdf_ref)]
    assert outbox == [(f"Recibí el PDF. Aún falta el XML para completar {SERIE}-{NUMERO}.", "BR-D09")]


async def test_media_info_503_fails_the_job_so_the_queue_retries(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-1.11: a Meta error a retry can fix fails the job instead of silently dropping the upload."""
    profile_id = f"uploads-{uuid4().hex}"
    invoice_id = await _invoice(db_pool, profile_id)
    pdf_ref = f"ref-{uuid4().hex}"
    await _link_upload(
        db_pool,
        invoice_id,
        profile_id,
        _Upload("pdf", pdf_ref, PDF_BYTES, datetime(2026, 11, 14, 10, tzinfo=UTC)),
    )

    with pytest.raises(WhatsAppError):
        await _run(db_pool, tmp_path, invoice_id, {}, options=_RunOptions(statuses={pdf_ref: 503}))

    async with db_pool.connection() as conn:
        state = await _state(conn, invoice_id)
        files = await _files(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert state == "MANUAL_REQUIRED"
    assert files == []
    assert outbox == []


async def test_upload_is_downloaded_through_its_channel_source(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-1.5: an upload is downloaded through the source registered for its channel."""
    profile_id = f"uploads-{uuid4().hex}"
    invoice_id = await _invoice(db_pool, profile_id)
    media_ref = f"ref-{uuid4().hex}"
    await _link_upload(
        db_pool,
        invoice_id,
        profile_id,
        _Upload("pdf", media_ref, PDF_BYTES, datetime(2026, 11, 14, 10, tzinfo=UTC)),
    )
    whatsapp = _FakeMediaSource({media_ref: (MIME["pdf"], PDF_BYTES)})
    telegram = _FakeMediaSource({media_ref: (MIME["pdf"], b"synthetic telegram bytes")})

    handler = make_attach_handler(
        db_pool,
        _config(),
        {"whatsapp": whatsapp, "telegram": telegram},
        tmp_path,
        _limits(),
    )
    await handler(_job(invoice_id))

    assert whatsapp.refs == [media_ref]
    assert telegram.refs == []


async def test_upload_from_a_channel_with_no_source_fails_the_job(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-1.5, BR-S03: a channel with no media source raises, so the queue retries then parks."""
    profile_id = f"uploads-{uuid4().hex}"
    invoice_id = await _invoice(db_pool, profile_id, channel="telegram")
    media_ref = f"ref-{uuid4().hex}"
    await _link_upload(
        db_pool,
        invoice_id,
        profile_id,
        _Upload("pdf", media_ref, PDF_BYTES, datetime(2026, 11, 14, 10, tzinfo=UTC)),
        channel="telegram",
    )

    with pytest.raises(LookupError, match="telegram"):
        await _run(db_pool, tmp_path, invoice_id, {media_ref: (MIME["pdf"], PDF_BYTES)})

    async with db_pool.connection() as conn:
        state = await _state(conn, invoice_id)
        files = await _files(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert state == "MANUAL_REQUIRED"
    assert files == []
    assert outbox == []
