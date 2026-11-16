"""Integration tests for the deliver job handler (R-7.1 to R-7.8, R-8.1, R-9.4, BR-D03, BR-D07,
BR-D10, D88, D89).

The folder is an in-memory fake :class:`Drive`; the assisted mode is the real :class:`DryRun`
writing under ``tmp_path`` and the automatic mode is a fake that records its sends. Postgres is
real.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.auth.msal_cache import AuthRequired
from invoicebot.channels.whatsapp.payloads import button_id
from invoicebot.config.loader import load_config
from invoicebot.config.models import (
    Brand,
    Config,
    ExcelTemplateSink,
    MailerSink,
    OneDriveFolderSink,
    Store,
    StoreDirectory,
)
from invoicebot.extract.fields import Field, Reading
from invoicebot.pipeline.deliver import (
    DELIVER_STEP,
    DeliverContext,
    make_deliver_handler,
    make_resend_handler,
)
from invoicebot.pipeline.steps import RESEND_EML_STEP, RESEND_LAPTOP_STEP
from invoicebot.queue.jobs import Job
from invoicebot.queue.worker import Handler
from invoicebot.rules.messages_es import BUTTON_VIA_LAPTOP, MESSAGES
from invoicebot.sinks.base import OutgoingMail
from invoicebot.sinks.laptop_job import LaptopJob, job_hmac
from invoicebot.sinks.mailer import DryRun, LaptopOutlook
from invoicebot.sinks.onedrive import GraphError, GraphTransient
from invoicebot.store.flow_b import (
    get_batch_state,
    load_flow_data,
    merge_flow_data,
    open_batch,
    set_batch_state,
)
from invoicebot.store.invoices import (
    FileRecord,
    add_file,
    create_invoice,
    get_file_path,
    save_reading,
    set_flow,
    set_state,
)
from invoicebot.store.mail_jobs import add_mail_job
from xlsx_factory import build_template

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
FLOW = "work_microsoft"
FLOW_WITHOUT_FOLDER = "personal_google"
PROFILE = "yo"
# From tests/synthetic_rucs.txt; no real RUC appears here.
STORE_RUC = "20000000001"
COMERCIO = "LaLena"
SECOND_COMERCIO = "Local Dos"
SERIE = "F001"
NUMERO = "15160"
FECHA = date(2026, 11, 5)
FOLDER_PREFIX = "2026-11-Support_"
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


class FakeDrive:
    """An in-memory Drive: folders hold their entry names and uploads keep their bytes."""

    def __init__(self, folders: dict[str, list[str]] | None = None) -> None:
        self.folders = {path: list(names) for path, names in (folders or {}).items()}
        self.listings: list[str] = []
        self.created: list[tuple[str, str]] = []
        self.uploads: dict[str, bytes] = {}
        self.moves: list[tuple[str, str, str]] = []

    async def list_names(self, path: str) -> list[str]:
        """Return the entries of a folder; a path that does not exist has none."""
        self.listings.append(path)
        return list(self.folders.get(path, []))

    async def create_folder(self, parent: str, name: str) -> None:
        """Record the creation and list the new folder under its parent."""
        self.created.append((parent, name))
        self.folders.setdefault(parent, []).append(name)

    async def upload(self, path: str, data: bytes) -> None:
        """Keep the file's bytes at its absolute path."""
        self.uploads[path] = data

    async def move(self, path: str, new_parent: str, new_name: str) -> None:
        """Record the move; the fake does not change its folders."""
        self.moves.append((path, new_parent, new_name))


class CrashingMode:
    """An assisted mode that fails after the folder is filed, standing in for a crash."""

    completion = "tap"
    done_rule = "BR-D08"

    async def deliver(self, invoice_id: int, mail: OutgoingMail, _folder_path: str) -> str | None:
        """Raise before writing any draft."""
        raise RuntimeError("synthetic crash before the mail step")


class FakeGraphMode:
    """An automatic mode that records each send instead of reaching Graph (D89)."""

    completion = "accepted"
    done_rule = "BR-D10"

    def __init__(self) -> None:
        self.calls: list[tuple[int, OutgoingMail]] = []

    async def deliver(self, invoice_id: int, mail: OutgoingMail, _folder_path: str) -> None:
        """Record the send; an automatic mode sends once per call (D89)."""
        self.calls.append((invoice_id, mail))


class FakeInboxRuleMode(FakeGraphMode):
    """An automatic mode whose done rule is BR-D13: Graph accepted the inbox_rule send (R-7.8)."""

    done_rule = "BR-D13"


class RaisingMode:
    """An automatic mode whose send raises the error the test installed (BR-D01, R-7.3, BR-D03)."""

    completion = "accepted"
    done_rule = "BR-D10"

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def deliver(self, invoice_id: int, mail: OutgoingMail, _folder_path: str) -> None:
        """Raise the installed error instead of sending."""
        raise self._error


class ObservingGraphMode:
    """An automatic mode that records the batch state it sees just before sending (D89)."""

    completion = "accepted"
    done_rule = "BR-D10"

    def __init__(self, pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
        self._pool = pool
        self.seen_state: str | None = None

    async def deliver(self, invoice_id: int, mail: OutgoingMail, _folder_path: str) -> None:
        """Read the batch's state; it must be ``sending`` before the send (D89)."""
        async with self._pool.connection() as conn:
            self.seen_state = await get_batch_state(conn, invoice_id)


class FailingJobUploadDrive(FakeDrive):
    """A drive whose laptop job upload fails with a final Graph error (BR-D03)."""

    async def upload(self, path: str, data: bytes) -> None:
        """Raise a 403 for the job file; every other upload succeeds."""
        if "/_outbox/" in path:
            raise GraphError(403)
        await super().upload(path, data)


class AuthRequiredDrive(FakeDrive):
    """A drive that reports the account must sign in before it lists a folder (BR-D01)."""

    async def list_names(self, path: str) -> list[str]:
        """Raise ``AuthRequired`` instead of listing the drive."""
        raise AuthRequired("personal")


class CancellingGraphMode:
    """An automatic mode that moves the invoice out of DELIVERING while it sends (R-7.5)."""

    completion = "accepted"
    done_rule = "BR-D10"

    def __init__(self, pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
        self._pool = pool

    async def deliver(self, invoice_id: int, mail: OutgoingMail, _folder_path: str) -> None:
        """Cancel the invoice during the send."""
        async with self._pool.connection() as conn, conn.transaction():
            await set_state(conn, invoice_id, "CANCELLED", rule_id="BR-E05")


def _field(value: str) -> Field:
    return Field(value=value, source="qr", confidence=1.0)


def _reading() -> Reading:
    """Build a complete synthetic reading for a flow B invoice."""
    return Reading(
        fields={
            "ruc_emisor": _field(STORE_RUC),
            "tipo": _field("01"),
            "serie": _field(SERIE),
            "numero": _field(NUMERO),
            "total": _field("51.61"),
            "fecha_emision": _field(FECHA.isoformat()),
        },
        document_url=None,
    )


def _directory() -> StoreDirectory:
    """Return a store directory with one store whose only brand is COMERCIO."""
    store = Store(
        ruc=STORE_RUC,
        razon_social="Comercio Sintetico",
        tipo="01",
        brands=[Brand(comercio=COMERCIO)],
        portal=None,
    )
    return StoreDirectory(stores={STORE_RUC: store}, portals={})


def _two_brand_directory() -> StoreDirectory:
    """Return a store with two brands whose prefixes match neither the tested serie."""
    store = Store(
        ruc=STORE_RUC,
        razon_social="Comercio Sintetico",
        tipo="01",
        brands=[
            Brand(comercio=COMERCIO, serie_prefix=["A"]),
            Brand(comercio=SECOND_COMERCIO, serie_prefix=["B"]),
        ],
        portal=None,
    )
    return StoreDirectory(stores={STORE_RUC: store}, portals={})


def _config(tmp_path: Path, profile_id: str, mail_mode: str = "dry_run") -> Config:
    """Return example.yaml with a synthetic template, one mail mode and one profile."""
    cfg = load_config(EXAMPLE)
    template = build_template()
    template_path = tmp_path / "template.xlsx"
    template_path.write_bytes(template)
    sinks: list[Any] = []
    for sink in cfg.flows[FLOW].sinks:
        if isinstance(sink, ExcelTemplateSink):
            sink = sink.model_copy(
                update={
                    "template": template_path,
                    "template_sha256": hashlib.sha256(template).hexdigest(),
                }
            )
        elif isinstance(sink, MailerSink):
            sink = sink.model_copy(update={"modes": [mail_mode]})
        sinks.append(sink)
    flow = cfg.flows[FLOW].model_copy(update={"sinks": sinks})
    return cfg.model_copy(
        update={
            "flows": {**cfg.flows, FLOW: flow},
            "profiles": {profile_id: cfg.profiles[PROFILE]},
        }
    )


def _config_with_admins(cfg: Config, profile_id: str, admin_ids: list[str]) -> Config:
    """Return cfg whose sender is not an admin and which has one admin profile per id (BR-D01)."""
    sender = cfg.profiles[profile_id].model_copy(update={"admin": False})
    profiles = {profile_id: sender}
    for admin_id in admin_ids:
        profiles[admin_id] = sender.model_copy(update={"admin": True})
    return cfg.model_copy(update={"profiles": profiles})


def _config_with_bad_template(cfg: Config) -> Config:
    """Return cfg whose Excel sink pins a SHA-256 the template cannot match (BR-D02)."""
    sinks: list[Any] = []
    for sink in cfg.flows[FLOW].sinks:
        if isinstance(sink, ExcelTemplateSink):
            sink = sink.model_copy(update={"template_sha256": "0" * 64})
        sinks.append(sink)
    flow = cfg.flows[FLOW].model_copy(update={"sinks": sinks})
    return cfg.model_copy(update={"flows": {**cfg.flows, FLOW: flow}})


def _job(invoice_id: int, step: str = DELIVER_STEP) -> Job:
    """Build the job the handler receives."""
    return Job(
        id=1,
        invoice_id=invoice_id,
        step=step,
        attempts=0,
        crashes=0,
        lease_until=datetime.now(UTC),
    )


def _handler(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    cfg: Config,
    drive: FakeDrive,
    data_dir: Path,
    mode: Any = None,
    mode_name: str = "dry_run",
) -> Handler:
    """Build the deliver handler with the given mode, assisted by default."""
    return make_deliver_handler(
        DeliverContext(
            pool,
            cfg,
            _directory(),
            drive,
            {mode_name: mode or DryRun(data_dir)},
            data_dir,
            laptop_flows=frozenset(),
        )
    )


def _resend_handlers(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    cfg: Config,
    drive: FakeDrive,
    data_dir: Path,
    modes: dict[str, Any],
    laptop_flows: frozenset[str] = frozenset(),
) -> dict[str, Handler]:
    """Build the resend handlers with the given mail modes."""
    context = DeliverContext(pool, cfg, _directory(), drive, modes, data_dir, laptop_flows=laptop_flows)
    return dict(make_resend_handler(context))


def _two_brand_handler(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    cfg: Config,
    drive: FakeDrive,
    data_dir: Path,
) -> Handler:
    """Build the assisted deliver handler against the two-brand store."""
    return make_deliver_handler(
        DeliverContext(
            pool,
            cfg,
            _two_brand_directory(),
            drive,
            {"dry_run": DryRun(data_dir)},
            data_dir,
            laptop_flows=frozenset(),
        )
    )


async def _delivering_invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    profile_id: str,
    *,
    state: str = "DELIVERING",
    flow: str = FLOW,
    flow_data: dict[str, Any] | None = None,
) -> int:
    """Create an invoice with a reading, a PDF and an XML, and return its id."""
    pdf = tmp_path / f"{uuid4().hex}.pdf"
    pdf.write_bytes(PDF_BYTES)
    xml = tmp_path / f"{uuid4().hex}.xml"
    xml.write_bytes(XML_BYTES)
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await save_reading(conn, invoice_id, _reading())
        await set_flow(conn, invoice_id, flow)
        await set_state(conn, invoice_id, state)
        for kind, path, data in (("pdf", pdf, PDF_BYTES), ("xml", xml, XML_BYTES)):
            await add_file(
                conn,
                invoice_id,
                FileRecord(
                    kind=kind,
                    local_path=str(path),
                    remote_ref=None,
                    sha256=hashlib.sha256(data).hexdigest(),
                    size=len(data),
                ),
            )
        if flow_data:
            await merge_flow_data(conn, invoice_id, flow_data)
    return invoice_id


async def _delivered_invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    profile_id: str,
    *,
    state: str = "DONE",
    batch_state: str = "sent",
    flow_data: dict[str, Any] | None = None,
) -> int:
    """Create an invoice the deliver step already filed, with a saved folder and a batch."""
    folder = f"{FOLDER_PREFIX}1_{COMERCIO}"
    saved = {
        "folder": folder,
        "seq": 1,
        "folder_path": f"/Reembolsos/{folder}",
        "year": FECHA.year,
        "month": FECHA.month,
        **(flow_data or {}),
    }
    invoice_id = await _delivering_invoice(pool, tmp_path, profile_id, state=state, flow_data=saved)
    async with pool.connection() as conn, conn.transaction():
        batch_id = await open_batch(conn, invoice_id, FLOW, "prepared")
        await set_batch_state(conn, batch_id, batch_state)
    return invoice_id


async def _add_photo(
    pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int, tmp_path: Path
) -> Path:
    """Store a synthetic photo file for the invoice and return its local path."""
    photo = tmp_path / f"{uuid4().hex}.jpg"
    photo.write_bytes(b"\xff\xd8\xff synthetic photo\n")
    async with pool.connection() as conn, conn.transaction():
        await add_file(
            conn,
            invoice_id,
            FileRecord(
                kind="photo",
                local_path=str(photo),
                remote_ref=None,
                sha256=hashlib.sha256(photo.read_bytes()).hexdigest(),
                size=photo.stat().st_size,
            ),
        )
    return photo


async def _invoice_state(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str, str | None]:
    cur = await conn.execute("SELECT state, rule_id FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0]), row[1]


async def _jobs(conn: AsyncConnection[Any], invoice_id: int) -> list[str]:
    """Return the steps of the invoice's queued jobs, in insertion order."""
    cur = await conn.execute("SELECT step FROM jobs WHERE invoice_id = %s ORDER BY id", (invoice_id,))
    return [str(step) for (step,) in await cur.fetchall()]


async def _mail_jobs(conn: AsyncConnection[Any], invoice_id: int) -> list[str]:
    """Return the job ids of the invoice's laptop mail jobs (R-7.14)."""
    cur = await conn.execute(
        "SELECT job_id FROM mail_jobs WHERE invoice_id = %s ORDER BY job_id",
        (invoice_id,),
    )
    return [str(job_id) for (job_id,) in await cur.fetchall()]


async def _eml_files(conn: AsyncConnection[Any], invoice_id: int) -> list[str]:
    cur = await conn.execute(
        "SELECT local_path FROM files WHERE invoice_id = %s AND kind = 'eml' ORDER BY id",
        (invoice_id,),
    )
    return [str(row[0]) for row in await cur.fetchall()]


async def _outbox(conn: AsyncConnection[Any], profile_id: str) -> list[tuple[str, str | None, Any]]:
    cur = await conn.execute(
        """
        SELECT payload_json->>'text', payload_json->>'rule_id', payload_json->'buttons'
        FROM outbox
        WHERE "to" = %s
        ORDER BY id
        """,
        (profile_id,),
    )
    rows = await cur.fetchall()
    return [(str(text), rule_id, buttons) for text, rule_id, buttons in rows]


async def test_dry_run_batches_the_invoice_with_br_d07(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-7.1, R-7.5, R-7.7, R-8.1, BR-D07, D88: the assisted mode files, drafts and batches."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id)
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    drive = FakeDrive()

    await _handler(db_pool, cfg, drive, tmp_path)(_job(invoice_id))

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        emls = await _eml_files(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    folder = f"{FOLDER_PREFIX}{flow_data['seq']}_{COMERCIO}"
    path = f"/Reembolsos/{folder}"
    legal_name = cfg.profiles[profile_id].legal_name
    assert invoice == ("BATCHED", "BR-D07")
    assert batch_state == "prepared"
    assert flow_data["folder"] == folder
    assert isinstance(flow_data["seq"], int)
    assert drive.created == [("/Reembolsos", folder)]
    assert set(drive.uploads) == {
        f"{path}/{legal_name}.pdf",
        f"{path}/{legal_name}.xml",
        f"{path}/.invoicebot.json",
        f"{path}/Formato de reembolsos.xlsx",
    }
    eml = tmp_path / "mail" / f"{invoice_id}.eml"
    assert emls == [str(eml)]
    assert eml.is_file()
    text, rule_id, buttons = outbox[0]
    assert text == MESSAGES["BR-D07"].format(folder=folder, modes_desc="borrador .eml")
    assert rule_id == "BR-D07"
    assert buttons == [{"id": button_id(invoice_id, "sent"), "title": "Ya lo envié"}]


async def test_a_chosen_brand_files_the_invoice_under_it(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """BR-R09: the brand the sender picked files the invoice when no serie prefix matches."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id)
    invoice_id = await _delivering_invoice(
        db_pool, tmp_path, profile_id, flow_data={"answers": {"comercio": SECOND_COMERCIO}}
    )
    drive = FakeDrive()

    await _two_brand_handler(db_pool, cfg, drive, tmp_path)(_job(invoice_id))

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
    folder = f"{FOLDER_PREFIX}{flow_data['seq']}_{SECOND_COMERCIO}"
    assert invoice == ("BATCHED", "BR-D07")
    assert flow_data["comercio"] == SECOND_COMERCIO
    assert drive.created == [("/Reembolsos", folder)]


async def test_no_answer_for_a_multi_brand_store_errors_for_no_comercio(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """BR-R09: with no answer and no serie match the invoice fails for no comercio as before."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id)
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    drive = FakeDrive()

    with pytest.raises(RuntimeError, match="has no comercio"):
        await _two_brand_handler(db_pool, cfg, drive, tmp_path)(_job(invoice_id))

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert invoice == ("DELIVERING", None)
    assert "comercio" not in flow_data
    assert outbox == []
    assert drive.created == []


async def test_an_assisted_draft_prepares_an_existing_failed_batch(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-8.1, BR-D08: an assisted draft turns an existing failed batch back into prepared."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id)
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    async with db_pool.connection() as conn, conn.transaction():
        batch_id = await open_batch(conn, invoice_id, FLOW, "sending")
        await set_batch_state(conn, batch_id, "failed")

    await _handler(db_pool, cfg, FakeDrive(), tmp_path)(_job(invoice_id))

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
    assert invoice == ("BATCHED", "BR-D07")
    assert batch_state == "prepared"


async def test_invoice_outside_delivering_is_left_untouched(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-9.4: an invoice already DONE is not filed, mailed or batched again."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id)
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id, state="DONE")
    drive = FakeDrive()

    await _handler(db_pool, cfg, drive, tmp_path)(_job(invoice_id))

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        emls = await _eml_files(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert invoice == ("DONE", None)
    assert batch_state is None
    assert emls == []
    assert outbox == []
    assert drive.listings == []
    assert drive.created == []
    assert drive.uploads == {}


async def test_retry_after_a_crash_reuses_the_saved_folder(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D88, R-7.1: a retry after a crash before the mail step reuses folder and seq without claiming."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id)
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    drive = FakeDrive()

    with pytest.raises(RuntimeError, match="synthetic crash"):
        await _handler(db_pool, cfg, drive, tmp_path, CrashingMode())(_job(invoice_id))

    async with db_pool.connection() as conn:
        first_state = await _invoice_state(conn, invoice_id)
        first_data = await load_flow_data(conn, invoice_id)
    assert first_state == ("DELIVERING", None)
    assert first_data["folder"].startswith(FOLDER_PREFIX)
    assert isinstance(first_data["seq"], int)
    drive.listings.clear()
    drive.created.clear()

    await _handler(db_pool, cfg, drive, tmp_path)(_job(invoice_id))

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
    assert (flow_data["folder"], flow_data["seq"]) == (first_data["folder"], first_data["seq"])
    assert drive.listings == []
    assert drive.created == [("/Reembolsos", first_data["folder"])]
    assert invoice == ("BATCHED", "BR-D07")
    assert batch_state == "prepared"


async def test_graph_sends_once_and_closes_as_done(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-7.5, R-7.7, R-9.7, BR-D10, D89: the automatic mode sends once, closes and deletes the photo."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id, mail_mode="graph")
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    photo = await _add_photo(db_pool, invoice_id, tmp_path)
    mode = FakeGraphMode()

    await _handler(db_pool, cfg, FakeDrive(), tmp_path, mode, mode_name="graph")(_job(invoice_id))

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
        photo_path = await get_file_path(conn, invoice_id, "photo")
    folder = f"{FOLDER_PREFIX}{flow_data['seq']}_{COMERCIO}"
    assert invoice == ("DONE", "BR-D10")
    assert batch_state == "sent"
    assert [call[0] for call in mode.calls] == [invoice_id]
    assert outbox == [(MESSAGES["BR-D10"].format(folder=folder), "BR-D10", [])]
    assert not photo.exists()
    assert photo_path is None


async def test_laptop_outlook_writes_one_signed_job_and_batches(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-7.14: a laptop delivery writes one signed job, prepares the batch and sends no message."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id, mail_mode="laptop_outlook")
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    root = next(sink.root for sink in cfg.flows[FLOW].sinks if isinstance(sink, OneDriveFolderSink))
    drive = FakeDrive()
    key = b"test-key"

    await _handler(
        db_pool, cfg, drive, tmp_path, LaptopOutlook(drive, root, key), mode_name="laptop_outlook"
    )(_job(invoice_id))

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        jobs = await _mail_jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    job_paths = [path for path in drive.uploads if path.startswith(f"{root}/_outbox/")]
    assert len(job_paths) == 1
    document = json.loads(drive.uploads[job_paths[0]])
    job = LaptopJob(
        job_id=document["job_id"],
        invoice_id=document["invoice_id"],
        folder=document["folder"],
        to=tuple(document["to"]),
        subject=document["subject"],
        body=document["body"],
        attachments=tuple(document["attachments"]),
    )
    assert job_paths == [f"{root}/_outbox/{job.job_id}.json"]
    assert job_hmac(job, key) == document["hmac"]
    assert job.folder == flow_data["folder_path"].lstrip("/")
    assert invoice == ("BATCHED", "BR-D07")
    assert batch_state == "prepared"
    assert jobs == [job.job_id]
    assert outbox == []


async def test_inbox_rule_send_closes_as_done_and_offers_no_llego(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-7.8: an accepted BR-D13 send makes the batch sent, the invoice DONE, one job and [No llegó]."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id, mail_mode="inbox_rule")
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    mode = FakeInboxRuleMode()

    await _handler(db_pool, cfg, FakeDrive(), tmp_path, mode, mode_name="inbox_rule")(_job(invoice_id))

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    folder = f"{FOLDER_PREFIX}{flow_data['seq']}_{COMERCIO}"
    text, rule_id, buttons = outbox[0]
    assert [call[0] for call in mode.calls] == [invoice_id]
    assert invoice == ("DONE", "BR-D13")
    assert batch_state == "sent"
    assert jobs == ["folder_status"]
    assert text == MESSAGES["BR-D13"].format(folder=folder)
    assert rule_id == "BR-D13"
    assert buttons == [{"id": button_id(invoice_id, "not_arrived"), "title": "No llegó"}]


async def test_a_sent_graph_delivery_leaves_one_folder_status_job(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-7.11: a mail Graph accepts leaves exactly one folder_status job."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id, mail_mode="graph")
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    mode = FakeGraphMode()

    await _handler(db_pool, cfg, FakeDrive(), tmp_path, mode, mode_name="graph")(_job(invoice_id))

    async with db_pool.connection() as conn:
        jobs = await _jobs(conn, invoice_id)
    assert [call[0] for call in mode.calls] == [invoice_id]
    assert jobs == ["folder_status"]


async def test_a_failed_send_leaves_no_folder_status_job(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-7.11: a final send failure leaves no folder_status job."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config_with_admins(_config(tmp_path, profile_id, mail_mode="graph"), profile_id, [])
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)

    await _handler(db_pool, cfg, FakeDrive(), tmp_path, RaisingMode(GraphError(400)), mode_name="graph")(
        _job(invoice_id)
    )

    async with db_pool.connection() as conn:
        jobs = await _jobs(conn, invoice_id)
    assert jobs == []


async def test_a_delivered_invoice_records_its_folder_path(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-7.11: delivery stores the folder's absolute path next to its name and seq."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id)
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)

    await _handler(db_pool, cfg, FakeDrive(), tmp_path)(_job(invoice_id))

    async with db_pool.connection() as conn:
        flow_data = await load_flow_data(conn, invoice_id)
    root = next(sink.root for sink in cfg.flows[FLOW].sinks if isinstance(sink, OneDriveFolderSink))
    assert flow_data["folder_path"] == f"{root}/{flow_data['folder']}"


async def test_a_cancelled_invoice_still_marks_the_batch_sent(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-7.5, D89: a mail Graph accepted marks the batch sent even if the invoice left DELIVERING."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id, mail_mode="graph")
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)

    await _handler(db_pool, cfg, FakeDrive(), tmp_path, CancellingGraphMode(db_pool), mode_name="graph")(
        _job(invoice_id)
    )

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert invoice == ("CANCELLED", "BR-E05")
    assert batch_state == "sent"
    assert outbox == []


async def test_rerunning_after_done_sends_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-9.4, D89: rerunning the deliver job on a DONE invoice sends nothing more."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id, mail_mode="graph")
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    drive = FakeDrive()
    mode = FakeGraphMode()
    handler = _handler(db_pool, cfg, drive, tmp_path, mode, mode_name="graph")

    await handler(_job(invoice_id))
    async with db_pool.connection() as conn:
        first_outbox = await _outbox(conn, profile_id)
    created = list(drive.created)

    await handler(_job(invoice_id))

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert invoice == ("DONE", "BR-D10")
    assert len(mode.calls) == 1
    assert outbox == first_outbox
    assert drive.created == created


async def test_a_sending_batch_is_not_resent_and_fails_with_br_d03(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """BR-D03, D89: a batch left sending is never resent; it fails and asks for a reprocess."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config_with_admins(_config(tmp_path, profile_id, mail_mode="graph"), profile_id, [])
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    async with db_pool.connection() as conn, conn.transaction():
        await open_batch(conn, invoice_id, FLOW, "sending")
    mode = FakeGraphMode()

    await _handler(db_pool, cfg, FakeDrive(), tmp_path, mode, mode_name="graph")(_job(invoice_id))

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert mode.calls == []
    assert invoice == ("BATCHED", "BR-D03")
    assert batch_state == "failed"
    assert outbox == [
        (
            MESSAGES["BR-D03"].format(serie=SERIE, numero=NUMERO, id=invoice_id),
            "BR-D03",
            [],
        )
    ]


async def test_send_failure_alerts_every_admin(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """BR-D03: a final send failure alerts the sender and each admin with a channel."""
    profile_id = f"deliver-{uuid4().hex}"
    admin_ids = [f"admin-a-{uuid4().hex}", f"admin-b-{uuid4().hex}"]
    cfg = _config_with_admins(_config(tmp_path, profile_id, mail_mode="graph"), profile_id, admin_ids)
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)

    await _handler(db_pool, cfg, FakeDrive(), tmp_path, RaisingMode(GraphError(400)), mode_name="graph")(
        _job(invoice_id)
    )

    admin_text = MESSAGES["BR-D03_admin"].format(id=invoice_id, serie=SERIE, numero=NUMERO)
    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        sender = await _outbox(conn, profile_id)
        admins = [await _outbox(conn, admin_id) for admin_id in admin_ids]
    assert invoice == ("BATCHED", "BR-D03")
    assert batch_state == "failed"
    assert sender == [(MESSAGES["BR-D03"].format(serie=SERIE, numero=NUMERO, id=invoice_id), "BR-D03", [])]
    assert admins == [[(admin_text, "BR-D03_admin", [])] for _ in admin_ids]


async def test_crashed_send_alerts_every_admin(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """BR-D03, D89: a batch left sending alerts the sender and each admin with a channel."""
    profile_id = f"deliver-{uuid4().hex}"
    admin_ids = [f"admin-a-{uuid4().hex}", f"admin-b-{uuid4().hex}"]
    cfg = _config_with_admins(_config(tmp_path, profile_id, mail_mode="graph"), profile_id, admin_ids)
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    async with db_pool.connection() as conn, conn.transaction():
        await open_batch(conn, invoice_id, FLOW, "sending")
    mode = FakeGraphMode()

    await _handler(db_pool, cfg, FakeDrive(), tmp_path, mode, mode_name="graph")(_job(invoice_id))

    admin_text = MESSAGES["BR-D03_admin"].format(id=invoice_id, serie=SERIE, numero=NUMERO)
    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        sender = await _outbox(conn, profile_id)
        admins = [await _outbox(conn, admin_id) for admin_id in admin_ids]
    assert mode.calls == []
    assert invoice == ("BATCHED", "BR-D03")
    assert batch_state == "failed"
    assert sender == [(MESSAGES["BR-D03"].format(serie=SERIE, numero=NUMERO, id=invoice_id), "BR-D03", [])]
    assert admins == [[(admin_text, "BR-D03_admin", [])] for _ in admin_ids]


async def test_admin_without_channel_is_skipped_on_send_failure(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """BR-D03: an admin without preferred_channel gets no row; the sender and other admins do."""
    profile_id = f"deliver-{uuid4().hex}"
    reachable = f"admin-a-{uuid4().hex}"
    unreachable = f"admin-b-{uuid4().hex}"
    cfg = _config_with_admins(
        _config(tmp_path, profile_id, mail_mode="graph"), profile_id, [reachable, unreachable]
    )
    profiles = dict(cfg.profiles)
    profiles[unreachable] = profiles[unreachable].model_copy(update={"preferred_channel": None})
    cfg = cfg.model_copy(update={"profiles": profiles})
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)

    await _handler(db_pool, cfg, FakeDrive(), tmp_path, RaisingMode(GraphError(400)), mode_name="graph")(
        _job(invoice_id)
    )

    admin_text = MESSAGES["BR-D03_admin"].format(id=invoice_id, serie=SERIE, numero=NUMERO)
    async with db_pool.connection() as conn:
        sender = await _outbox(conn, profile_id)
        skipped = await _outbox(conn, unreachable)
        other_admin = await _outbox(conn, reachable)
    assert sender == [(MESSAGES["BR-D03"].format(serie=SERIE, numero=NUMERO, id=invoice_id), "BR-D03", [])]
    assert skipped == []
    assert other_admin == [(admin_text, "BR-D03_admin", [])]


async def test_auth_required_parks_the_invoice_and_alerts_every_admin(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """BR-D01, R-7.9: a missing token leaves PENDING_AUTH and alerts the sender and each admin."""
    profile_id = f"deliver-{uuid4().hex}"
    admin_ids = [f"admin-a-{uuid4().hex}", f"admin-b-{uuid4().hex}"]
    cfg = _config_with_admins(_config(tmp_path, profile_id), profile_id, admin_ids)
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)

    await _handler(db_pool, cfg, AuthRequiredDrive(), tmp_path)(_job(invoice_id))

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        user = await _outbox(conn, profile_id)
        admins = [await _outbox(conn, admin_id) for admin_id in admin_ids]
    assert invoice == ("PENDING_AUTH", "BR-D01")
    assert user == [(MESSAGES["BR-D01"].format(provider="Microsoft"), "BR-D01", [])]
    assert admins == [
        [(MESSAGES["BR-D01_admin"].format(provider="microsoft", account="personal"), "BR-D01_admin", [])]
        for _ in admin_ids
    ]


async def test_a_bad_template_hash_requires_a_manual_fix(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """BR-D02, R-7.10: a template that does not match its pinned hash leaves MANUAL_REQUIRED."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config_with_bad_template(_config(tmp_path, profile_id))
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)

    await _handler(db_pool, cfg, FakeDrive(), tmp_path)(_job(invoice_id))

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert invoice == ("MANUAL_REQUIRED", "BR-D02")
    assert outbox == [(MESSAGES["BR-D02"], "BR-D02", [])]


async def test_graph_error_fails_the_batch_and_keeps_the_invoice_batched(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """BR-D03, D89: a final send failure fails the batch and leaves the invoice BATCHED."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config_with_admins(_config(tmp_path, profile_id, mail_mode="graph"), profile_id, [])
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)

    await _handler(db_pool, cfg, FakeDrive(), tmp_path, RaisingMode(GraphError(400)), mode_name="graph")(
        _job(invoice_id)
    )

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert invoice == ("BATCHED", "BR-D03")
    assert batch_state == "failed"
    assert outbox == [(MESSAGES["BR-D03"].format(serie=SERIE, numero=NUMERO, id=invoice_id), "BR-D03", [])]


async def test_graph_transient_propagates_and_leaves_the_batch_sending(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-7.3, D89: a transient send failure reaches the queue and the batch stays sending."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id, mail_mode="graph")
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)

    with pytest.raises(GraphTransient, match="status 503"):
        await _handler(
            db_pool, cfg, FakeDrive(), tmp_path, RaisingMode(GraphTransient(503)), mode_name="graph"
        )(_job(invoice_id))

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert invoice == ("DELIVERING", None)
    assert batch_state == "sending"
    assert outbox == []


async def test_auth_required_on_send_parks_the_invoice_and_frees_the_batch(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """BR-D01, D89: an expired token at send time parks the invoice; the batch is failed, not sending."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id, mail_mode="graph")
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)

    await _handler(
        db_pool, cfg, FakeDrive(), tmp_path, RaisingMode(AuthRequired("personal")), mode_name="graph"
    )(_job(invoice_id))

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
    assert invoice == ("PENDING_AUTH", "BR-D01")
    assert batch_state == "failed"


async def test_a_failed_batch_is_marked_sending_before_the_send(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D89: an existing failed batch is set sending before the mode sends it."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id, mail_mode="graph")
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    async with db_pool.connection() as conn, conn.transaction():
        batch_id = await open_batch(conn, invoice_id, FLOW, "sending")
        await set_batch_state(conn, batch_id, "failed")
    mode = ObservingGraphMode(db_pool)

    await _handler(db_pool, cfg, FakeDrive(), tmp_path, mode, mode_name="graph")(_job(invoice_id))

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
    assert mode.seen_state == "sending"
    assert invoice == ("DONE", "BR-D10")
    assert batch_state == "sent"


async def test_flow_without_a_folder_sink_is_a_no_op(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-9.4: a flow whose sinks have no onedrive_folder is left untouched."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id)
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id, flow=FLOW_WITHOUT_FOLDER)
    drive = FakeDrive()

    await _handler(db_pool, cfg, drive, tmp_path)(_job(invoice_id))

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        emls = await _eml_files(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert invoice == ("DELIVERING", None)
    assert batch_state is None
    assert emls == []
    assert outbox == []
    assert drive.listings == []
    assert drive.created == []
    assert drive.uploads == {}


async def test_resend_laptop_writes_one_job_and_leaves_the_batch(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-7.14: [Por la laptop] on a DONE invoice writes one job and one mail_jobs row."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id, mail_mode="laptop_outlook")
    invoice_id = await _delivered_invoice(db_pool, tmp_path, profile_id)
    root = next(sink.root for sink in cfg.flows[FLOW].sinks if isinstance(sink, OneDriveFolderSink))
    drive = FakeDrive()
    key = b"test-key"
    handlers = _resend_handlers(
        db_pool, cfg, drive, tmp_path, {"laptop_outlook": LaptopOutlook(drive, root, key)}
    )

    await handlers[RESEND_LAPTOP_STEP](_job(invoice_id, RESEND_LAPTOP_STEP))

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        jobs = await _mail_jobs(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    job_paths = [path for path in drive.uploads if path.startswith(f"{root}/_outbox/")]
    assert len(job_paths) == 1
    document = json.loads(drive.uploads[job_paths[0]])
    job = LaptopJob(
        job_id=document["job_id"],
        invoice_id=document["invoice_id"],
        folder=document["folder"],
        to=tuple(document["to"]),
        subject=document["subject"],
        body=document["body"],
        attachments=tuple(document["attachments"]),
    )
    assert job_paths == [f"{root}/_outbox/{job.job_id}.json"]
    assert job_hmac(job, key) == document["hmac"]
    assert job.folder == flow_data["folder_path"].lstrip("/")
    assert jobs == [job.job_id]
    assert flow_data["resending"] is False
    assert invoice == ("DONE", None)
    assert batch_state == "sent"
    assert outbox == []


async def test_resend_laptop_after_a_folder_rename_keeps_one_folder(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D109: a resend after folder_status renamed the folder keeps one folder and its folder_path."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id, mail_mode="laptop_outlook")
    root = next(sink.root for sink in cfg.flows[FLOW].sinks if isinstance(sink, OneDriveFolderSink))
    base = f"{FOLDER_PREFIX}1_{COMERCIO}"
    renamed = f"{root}/{base}_Sent"
    invoice_id = await _delivered_invoice(db_pool, tmp_path, profile_id, flow_data={"folder_path": renamed})
    drive = FakeDrive()
    handlers = _resend_handlers(
        db_pool, cfg, drive, tmp_path, {"laptop_outlook": LaptopOutlook(drive, root, b"test-key")}
    )

    await handlers[RESEND_LAPTOP_STEP](_job(invoice_id, RESEND_LAPTOP_STEP))

    async with db_pool.connection() as conn:
        flow_data = await load_flow_data(conn, invoice_id)
    assert flow_data["folder_path"] == renamed
    assert drive.created == [(root, f"{base}_Sent")]
    assert f"{renamed}/PERSONA DE EJEMPLO.pdf" in drive.uploads
    assert f"{renamed}/PERSONA DE EJEMPLO.xml" in drive.uploads
    assert f"{renamed}/.invoicebot.json" in drive.uploads
    assert f"{root}/{base}/PERSONA DE EJEMPLO.pdf" not in drive.uploads


async def test_resend_laptop_with_a_resending_marker_asks_again(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-7.8, R-7.14: a resending marker means BR-D11 asks again, offering [Por la laptop].

    The inbox-first flow lists ``laptop_outlook``, so the retry offers the button even
    though this crashed write left no mail job (D109).
    """
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id, mail_mode="laptop_outlook")
    invoice_id = await _delivered_invoice(db_pool, tmp_path, profile_id, flow_data={"resending": True})
    root = next(sink.root for sink in cfg.flows[FLOW].sinks if isinstance(sink, OneDriveFolderSink))
    drive = FakeDrive()
    handlers = _resend_handlers(
        db_pool,
        cfg,
        drive,
        tmp_path,
        {"laptop_outlook": LaptopOutlook(drive, root, b"test-key")},
        laptop_flows=frozenset({FLOW}),
    )

    await handlers[RESEND_LAPTOP_STEP](_job(invoice_id, RESEND_LAPTOP_STEP))

    async with db_pool.connection() as conn:
        flow_data = await load_flow_data(conn, invoice_id)
        jobs = await _mail_jobs(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert [path for path in drive.uploads if path.startswith(f"{root}/_outbox/")] == []
    assert jobs == []
    assert flow_data["resending"] is False
    assert batch_state == "sent"
    assert outbox == [
        (
            MESSAGES["BR-D11"].format(serie=SERIE, numero=NUMERO),
            "BR-D11",
            [
                {"id": button_id(invoice_id, "via_laptop"), "title": BUTTON_VIA_LAPTOP},
                {"id": button_id(invoice_id, "self_send"), "title": "Lo envío yo"},
                {"id": button_id(invoice_id, "arrived"), "title": "Ya llegó"},
            ],
        )
    ]


async def test_resend_laptop_after_a_sign_in_failure_clears_the_marker(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-7.14: a sign-in failure writes no job, so the marker is cleared for the next press."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id, mail_mode="laptop_outlook")
    invoice_id = await _delivered_invoice(db_pool, tmp_path, profile_id)
    handlers = _resend_handlers(
        db_pool, cfg, FakeDrive(), tmp_path, {"laptop_outlook": RaisingMode(AuthRequired("personal"))}
    )

    await handlers[RESEND_LAPTOP_STEP](_job(invoice_id, RESEND_LAPTOP_STEP))

    async with db_pool.connection() as conn:
        flow_data = await load_flow_data(conn, invoice_id)
        jobs = await _mail_jobs(conn, invoice_id)
    assert flow_data["resending"] is False
    assert jobs == []


async def test_resend_eml_on_a_sent_batch_writes_the_draft_without_the_button(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-7.8: [Lo envío yo] on a sent batch rewrites the .eml and answers BR-D07 with no button."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id)
    invoice_id = await _delivered_invoice(db_pool, tmp_path, profile_id, batch_state="sent")
    handlers = _resend_handlers(db_pool, cfg, FakeDrive(), tmp_path, {"dry_run": DryRun(tmp_path)})

    await handlers[RESEND_EML_STEP](_job(invoice_id, RESEND_EML_STEP))

    eml = tmp_path / "mail" / f"{invoice_id}.eml"
    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        emls = await _eml_files(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert eml.is_file()
    assert emls == []
    assert invoice == ("DONE", None)
    assert batch_state == "sent"
    assert outbox == [
        (MESSAGES["BR-D07"].format(folder=flow_data["folder"], modes_desc="borrador .eml"), "BR-D07", [])
    ]


async def test_resend_laptop_with_a_waiting_job_queues_br_d12(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-7.7: a resend while a job has no receipt writes no file and queues BR-D12."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id, mail_mode="laptop_outlook")
    invoice_id = await _delivered_invoice(db_pool, tmp_path, profile_id)
    async with db_pool.connection() as conn, conn.transaction():
        await add_mail_job(conn, "waiting-job", invoice_id)
        await conn.execute(
            "UPDATE mail_jobs SET created_at = now() - interval '5 hours' WHERE job_id = %s",
            ("waiting-job",),
        )
    root = next(sink.root for sink in cfg.flows[FLOW].sinks if isinstance(sink, OneDriveFolderSink))
    drive = FakeDrive()
    handlers = _resend_handlers(
        db_pool, cfg, drive, tmp_path, {"laptop_outlook": LaptopOutlook(drive, root, b"test-key")}
    )

    await handlers[RESEND_LAPTOP_STEP](_job(invoice_id, RESEND_LAPTOP_STEP))

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        jobs = await _mail_jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert [path for path in drive.uploads if "/_outbox/" in path] == []
    assert jobs == ["waiting-job"]
    assert "resending" not in flow_data
    assert invoice == ("DONE", None)
    assert outbox == [(MESSAGES["BR-D12"].format(serie=SERIE, numero=NUMERO, hours=5), "BR-D12", [])]


async def test_resend_laptop_after_an_error_receipt_writes_a_new_job(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-7.7: a job whose receipt reported an error does not block a new laptop job."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id, mail_mode="laptop_outlook")
    invoice_id = await _delivered_invoice(db_pool, tmp_path, profile_id)
    async with db_pool.connection() as conn, conn.transaction():
        await add_mail_job(conn, "failed-job", invoice_id)
        await conn.execute(
            "UPDATE mail_jobs SET receipt_error = 'synthetic failure' WHERE job_id = %s",
            ("failed-job",),
        )
    root = next(sink.root for sink in cfg.flows[FLOW].sinks if isinstance(sink, OneDriveFolderSink))
    drive = FakeDrive()
    handlers = _resend_handlers(
        db_pool, cfg, drive, tmp_path, {"laptop_outlook": LaptopOutlook(drive, root, b"test-key")}
    )

    await handlers[RESEND_LAPTOP_STEP](_job(invoice_id, RESEND_LAPTOP_STEP))

    async with db_pool.connection() as conn:
        flow_data = await load_flow_data(conn, invoice_id)
        jobs = await _mail_jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert len([path for path in drive.uploads if "/_outbox/" in path]) == 1
    assert len(jobs) == 2
    assert "failed-job" in jobs
    assert flow_data["resending"] is False
    assert outbox == []


async def test_a_graph_error_on_the_job_upload_fails_the_batch_with_br_d03(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-7.14, BR-D03: a 4xx on the job upload fails the send and never crashes the worker."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config_with_admins(_config(tmp_path, profile_id, mail_mode="laptop_outlook"), profile_id, [])
    invoice_id = await _delivering_invoice(db_pool, tmp_path, profile_id)
    root = next(sink.root for sink in cfg.flows[FLOW].sinks if isinstance(sink, OneDriveFolderSink))
    drive = FailingJobUploadDrive()

    await _handler(
        db_pool, cfg, drive, tmp_path, LaptopOutlook(drive, root, b"test-key"), mode_name="laptop_outlook"
    )(_job(invoice_id))

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        jobs = await _mail_jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert [path for path in drive.uploads if "/_outbox/" in path] == []
    assert invoice == ("BATCHED", "BR-D03")
    assert batch_state == "failed"
    assert jobs == []
    assert outbox == [(MESSAGES["BR-D03"].format(serie=SERIE, numero=NUMERO, id=invoice_id), "BR-D03", [])]


async def test_resend_eml_on_a_prepared_batch_offers_ya_lo_envie(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-7.8: [Lo envío yo] on a prepared batch answers BR-D07 with [Ya lo envié]."""
    profile_id = f"deliver-{uuid4().hex}"
    cfg = _config(tmp_path, profile_id)
    invoice_id = await _delivered_invoice(
        db_pool, tmp_path, profile_id, state="BATCHED", batch_state="prepared"
    )
    handlers = _resend_handlers(db_pool, cfg, FakeDrive(), tmp_path, {"dry_run": DryRun(tmp_path)})

    await handlers[RESEND_EML_STEP](_job(invoice_id, RESEND_EML_STEP))

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert invoice == ("BATCHED", None)
    assert batch_state == "prepared"
    assert outbox == [
        (
            MESSAGES["BR-D07"].format(folder=flow_data["folder"], modes_desc="borrador .eml"),
            "BR-D07",
            [{"id": button_id(invoice_id, "sent"), "title": "Ya lo envié"}],
        )
    ]
