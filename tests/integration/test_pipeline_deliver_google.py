"""Integration tests for the flow A deliver handler (R-6.1 to R-6.3, R-8.1, R-9.4, R-9.7,
BR-D01, BR-D16, BR-D17).

Drive and Sheets are one stateful ``httpx.MockTransport`` that really remembers folders, files
and rows, so a retried delivery reuses and updates instead of duplicating (R-6.3). Postgres is
real.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.auth.msal_cache import AuthRequired
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config
from invoicebot.extract.fields import Field, Reading
from invoicebot.pipeline.deliver_google import GoogleDeliverContext, make_google_deliver_handler
from invoicebot.pipeline.steps import DELIVER_STEP
from invoicebot.queue.jobs import Job
from invoicebot.queue.worker import Handler
from invoicebot.rules.messages_es import MESSAGES
from invoicebot.sinks.google_api import GoogleTransient
from invoicebot.sinks.google_drive import GoogleDrive
from invoicebot.sinks.google_sheets import GoogleSheets
from invoicebot.states import CANCELLED, StateMoved
from invoicebot.store.invoices import (
    FileRecord,
    add_file,
    create_invoice,
    save_reading,
    set_flow,
    set_state,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
FLOW = "personal_google"
PROFILE = "yo"
ACCOUNT = "owner"
SPREADSHEET_ID = "SHEET_ID"
TAB = "Compras"
SHEET_ID = 123
TIMEOUT_S = 5.0
MAX_ROWS = 100
# From tests/synthetic_rucs.txt; no real RUC appears here.
STORE_RUC = "20000000001"
RAZON_SOCIAL = "Restaurante La Lena S.A.C."
SERIE = "F001"
NUMERO = "15160"
TIPO = "01"
TOTAL = "51.61"
FECHA = date(2026, 11, 5)
FILE_NAME = f"{FECHA:%Y-%m-%d}_{STORE_RUC}_{SERIE}-{NUMERO}"
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
PHOTO_BYTES = b"\xff\xd8\xff synthetic photo\n"


class FakeGoogle:
    """A stateful Drive + Sheets backend served through one ``MockTransport`` handler (R-6.3)."""

    def __init__(self, *, fail: int | None = None) -> None:
        self.fail = fail
        #: When set, the next request matching the predicate fails once with this status.
        self.fail_once: tuple[Callable[[httpx.Request], bool], int] | None = None
        self.folder_ids: dict[tuple[str, str], str] = {}
        self.files: dict[tuple[str, str], tuple[str, str]] = {}
        self.created_folders: list[tuple[str, str, str]] = []
        self.uploaded: list[tuple[str, str, str, str]] = []
        self.keys: dict[str, int] = {}
        self.rows: dict[int, list[str]] = {}
        self.appended: list[list[str]] = []
        self.updated: list[tuple[int, list[str]]] = []
        self.sheet_id = SHEET_ID
        self._next_folder = 1
        self._next_file = 1
        self._next_row = 2

    @property
    def last_folder_id(self) -> str:
        """Return the id of the last created folder, the one the completion links (R-6.1)."""
        return self.created_folders[-1][2]

    @property
    def last_row_number(self) -> int:
        """Return the row number the last append landed on (R-6.2)."""
        return self._next_row - 1

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Route one request to the fake Drive or Sheets backend."""
        if self.fail_once is not None:
            predicate, status = self.fail_once
            if predicate(request):
                self.fail_once = None
                return httpx.Response(status)
        if self.fail is not None:
            return httpx.Response(self.fail)
        if request.url.host == "www.googleapis.com":
            return self._drive(request)
        return self._sheets(request)

    def _drive(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/drive/v3/files":
            parent, name = self._parent_and_name(request.url.params["q"])
            found = self.folder_ids.get((parent, name)) or self.files.get((parent, name))
            if found is None:
                return httpx.Response(200, json={"files": []})
            file_id, link = self._entry(parent, name, found)
            return httpx.Response(200, json={"files": [{"id": file_id, "webViewLink": link}]})
        if request.method == "POST" and path == "/drive/v3/files":
            body = json.loads(request.content)
            parent, name = body["parents"][0], body["name"]
            folder_id = f"folder_{self._next_folder:012d}"
            self._next_folder += 1
            self.folder_ids[(parent, name)] = folder_id
            self.created_folders.append((parent, name, folder_id))
            return httpx.Response(200, json={"id": folder_id})
        if request.method == "POST" and path == "/upload/drive/v3/files":
            metadata = self._metadata(request)
            parent, name = metadata["parents"][0], metadata["name"]
            existing = self.files.get((parent, name))
            if existing is not None:
                return httpx.Response(200, json={"id": existing[0], "webViewLink": existing[1]})
            file_id = f"file_{self._next_file:012d}"
            self._next_file += 1
            link = f"https://drive.google.com/file/d/{file_id}/view"
            self.files[(parent, name)] = (file_id, link)
            self.uploaded.append((parent, name, file_id, link))
            return httpx.Response(200, json={"id": file_id, "webViewLink": link})
        return httpx.Response(404)

    def _sheets(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == f"/v4/spreadsheets/{SPREADSHEET_ID}":
            return httpx.Response(
                200, json={"sheets": [{"properties": {"sheetId": self.sheet_id, "title": TAB}}]}
            )
        if path.startswith(f"/v4/spreadsheets/{SPREADSHEET_ID}/values/"):
            if request.method == "GET":
                values = [["Clave"], *[[key] for key in self.keys]]
                return httpx.Response(200, json={"values": values})
            if request.method == "POST" and path.endswith(":append"):
                row = json.loads(request.content)["values"][0]
                row_number = self._next_row
                self._next_row += 1
                self.keys[row[0]] = row_number
                self.rows[row_number] = row
                self.appended.append(row)
                tab = self._tab(path)
                return httpx.Response(
                    200, json={"updates": {"updatedRange": f"{tab}!A{row_number}:K{row_number}"}}
                )
            if request.method == "PUT":
                row = json.loads(request.content)["values"][0]
                row_number = self.keys[row[0]]
                self.rows[row_number] = row
                self.updated.append((row_number, row))
                return httpx.Response(200, json={})
        return httpx.Response(404)

    @staticmethod
    def _parent_and_name(query: str) -> tuple[str, str]:
        """Return the parent id and file name from a Drive ``q`` literal."""
        parent = re.search(r"'([^']*)' in parents", query)
        name = re.search(r"name = '([^']*)'", query)
        assert parent is not None
        assert name is not None
        return parent.group(1), name.group(1)

    @staticmethod
    def _metadata(request: httpx.Request) -> dict[str, Any]:
        """Return the JSON metadata part of a multipart upload."""
        content_type = request.headers["Content-Type"]
        boundary = content_type.split("boundary=", 1)[1].strip('"')
        parts = request.content.split(b"--" + boundary.encode())
        head = parts[1].split(b"\r\n\r\n", 1)[1]
        return json.loads(head.split(b"\r\n", 1)[0])

    @staticmethod
    def _tab(path: str) -> str:
        """Return the tab name from a Sheets values path."""
        return path.split("/values/", 1)[1].split("!", 1)[0].strip("'")

    def _entry(self, parent: str, name: str, found: str | tuple[str, str]) -> tuple[str, str]:
        """Return ``(id, link)`` for a found folder or file entry."""
        if isinstance(found, tuple):
            return found
        return found, f"https://drive.google.com/drive/folders/{found}"


class _Tokens:
    """A TokenSource that always hands out the same synthetic bearer token."""

    async def token(self) -> str:
        return "synthetic-bearer-value"


@dataclass(frozen=True)
class _Seeded:
    """The invoice and the local files a test seeded."""

    invoice_id: int
    photo: Path | None
    pdf: Path | None
    xml: Path | None


def _field(value: str) -> Field:
    return Field(value=value, source="qr", confidence=1.0)


def _reading() -> Reading:
    """Build a complete synthetic reading for a flow A invoice."""
    return Reading(
        fields={
            "ruc_emisor": _field(STORE_RUC),
            "tipo": _field(TIPO),
            "serie": _field(SERIE),
            "numero": _field(NUMERO),
            "razon_social": _field(RAZON_SOCIAL),
            "total": _field(TOTAL),
            "fecha_emision": _field(FECHA.isoformat()),
        },
        document_url=None,
    )


def _config(profile_id: str) -> Config:
    """Return example.yaml with ``profile_id`` as the only profile."""
    cfg = load_config(EXAMPLE)
    return cfg.model_copy(update={"profiles": {profile_id: cfg.profiles[PROFILE]}})


def _config_with_admins(cfg: Config, profile_id: str, admin_ids: list[str]) -> Config:
    """Return cfg whose sender is not an admin and which has one admin profile per id (BR-D01)."""
    sender = cfg.profiles[profile_id].model_copy(update={"admin": False})
    profiles = {profile_id: sender}
    for admin_id in admin_ids:
        profiles[admin_id] = sender.model_copy(update={"admin": True})
    return cfg.model_copy(update={"profiles": profiles})


def _job(invoice_id: int) -> Job:
    """Build the job the handler receives."""
    return Job(
        id=1,
        invoice_id=invoice_id,
        step=DELIVER_STEP,
        attempts=0,
        crashes=0,
        lease_until=datetime.now(UTC),
    )


async def _delivering_invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    profile_id: str,
    *,
    state: str = "DELIVERING",
    pdf: bool = True,
) -> _Seeded:
    """Create a flow A invoice with a reading, a photo, an XML and (unless ``pdf=False``) a PDF."""
    photo_path = tmp_path / f"{uuid4().hex}.jpg"
    pdf_path = tmp_path / f"{uuid4().hex}.pdf" if pdf else None
    xml_path = tmp_path / f"{uuid4().hex}.xml"
    photo_path.write_bytes(PHOTO_BYTES)
    if pdf_path is not None:
        pdf_path.write_bytes(PDF_BYTES)
    xml_path.write_bytes(XML_BYTES)
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await save_reading(conn, invoice_id, _reading())
        await set_flow(conn, invoice_id, FLOW)
        await set_state(conn, invoice_id, state)
        for kind, path in (("photo", photo_path), ("pdf", pdf_path), ("xml", xml_path)):
            if path is None:
                continue
            data = path.read_bytes()
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
    return _Seeded(invoice_id=invoice_id, photo=photo_path, pdf=pdf_path, xml=xml_path)


@contextlib.asynccontextmanager
async def _handler(
    pool: AsyncConnectionPool[AsyncConnection[Any]], cfg: Config, fake: FakeGoogle
) -> AsyncIterator[Handler]:
    """Build the flow A deliver handler on one real Drive and Sheets client over the fake."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as http:
        drive = GoogleDrive(http, _Tokens(), TIMEOUT_S, account=ACCOUNT)
        sheets = GoogleSheets(http, _Tokens(), TIMEOUT_S, account=ACCOUNT, max_rows=MAX_ROWS)
        yield make_google_deliver_handler(GoogleDeliverContext(pool, cfg, drive, sheets, cfg.profiles))


async def _invoice_state(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str, str | None]:
    cur = await conn.execute("SELECT state, rule_id FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0]), row[1]


async def _invoice_error(conn: AsyncConnection[Any], invoice_id: int) -> str | None:
    cur = await conn.execute("SELECT error FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return row[0]


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


async def test_delivering_invoice_files_drive_and_appends_its_row(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-6.1, R-6.2, BR-D16: a DELIVERING invoice ends DONE with files and one row and both links."""
    profile_id = f"google-deliver-{uuid4().hex}"
    cfg = _config(profile_id)
    seeded = await _delivering_invoice(db_pool, tmp_path, profile_id)
    fake = FakeGoogle()

    async with _handler(db_pool, cfg, fake) as handler:
        await handler(_job(seeded.invoice_id))

    key = f"{profile_id}|{STORE_RUC}|{TIPO}|{SERIE}|{NUMERO}"
    folder_link = f"https://drive.google.com/drive/folders/{fake.last_folder_id}"
    row_link = (
        f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit"
        f"#gid={SHEET_ID}&range=A{fake.last_row_number}"
    )
    expected = MESSAGES["BR-D16"].format(
        serie=SERIE,
        numero=NUMERO,
        razon_social=RAZON_SOCIAL,
        total=TOTAL,
        link_row=row_link,
        link_folder=folder_link,
    )

    async with db_pool.connection() as conn:
        state = await _invoice_state(conn, seeded.invoice_id)
        outbox = await _outbox(conn, profile_id)

    assert state == ("DONE", "BR-D16")
    assert [uploaded[1] for uploaded in fake.uploaded] == [
        f"{FILE_NAME}.jpg",
        f"{FILE_NAME}.pdf",
        f"{FILE_NAME}.xml",
    ]
    assert len(fake.appended) == 1
    assert fake.appended[0][0] == key
    assert outbox == [(expected, "BR-D16", [])]


async def test_a_retry_reuses_drive_files_and_updates_the_row(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-6.3: a retried delivery reuses the Drive files and updates the one row it appended."""
    profile_id = f"google-retry-{uuid4().hex}"
    cfg = _config(profile_id)
    seeded = await _delivering_invoice(db_pool, tmp_path, profile_id)
    fake = FakeGoogle()
    fake.fail_once = (
        lambda request: request.method == "GET" and request.url.path == f"/v4/spreadsheets/{SPREADSHEET_ID}",
        503,
    )

    async with _handler(db_pool, cfg, fake) as handler:
        with pytest.raises(GoogleTransient, match="status 503"):
            await handler(_job(seeded.invoice_id))
        await handler(_job(seeded.invoice_id))

    async with db_pool.connection() as conn:
        state = await _invoice_state(conn, seeded.invoice_id)

    assert state == ("DONE", "BR-D16")
    assert [uploaded[1] for uploaded in fake.uploaded] == [
        f"{FILE_NAME}.jpg",
        f"{FILE_NAME}.pdf",
        f"{FILE_NAME}.xml",
    ]
    assert len(fake.appended) == 1
    assert len(fake.updated) == 1


async def test_no_pdf_reports_br_d17(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-8.1, BR-D17: an invoice with no PDF still files and reports the missing-PDF message."""
    profile_id = f"google-no-pdf-{uuid4().hex}"
    cfg = _config(profile_id)
    seeded = await _delivering_invoice(db_pool, tmp_path, profile_id, pdf=False)
    fake = FakeGoogle()

    async with _handler(db_pool, cfg, fake) as handler:
        await handler(_job(seeded.invoice_id))

    folder_link = f"https://drive.google.com/drive/folders/{fake.last_folder_id}"
    row_link = (
        f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit"
        f"#gid={SHEET_ID}&range=A{fake.last_row_number}"
    )
    expected = MESSAGES["BR-D17"].format(
        serie=SERIE,
        numero=NUMERO,
        razon_social=RAZON_SOCIAL,
        total=TOTAL,
        link_row=row_link,
        link_folder=folder_link,
    )

    async with db_pool.connection() as conn:
        state = await _invoice_state(conn, seeded.invoice_id)
        outbox = await _outbox(conn, profile_id)

    assert state == ("DONE", "BR-D17")
    assert outbox == [(expected, "BR-D17", [])]


async def test_auth_required_parks_the_invoice_and_alerts_every_admin(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """BR-D01: a 401 leaves PENDING_AUTH and alerts the sender and each admin to auth_google."""
    profile_id = f"google-auth-{uuid4().hex}"
    admin_ids = [f"admin-a-{uuid4().hex}", f"admin-b-{uuid4().hex}"]
    cfg = _config_with_admins(_config(profile_id), profile_id, admin_ids)
    seeded = await _delivering_invoice(db_pool, tmp_path, profile_id)
    fake = FakeGoogle(fail=401)

    async with _handler(db_pool, cfg, fake) as handler:
        await handler(_job(seeded.invoice_id))

    async with db_pool.connection() as conn:
        state = await _invoice_state(conn, seeded.invoice_id)
        error = await _invoice_error(conn, seeded.invoice_id)
        user = await _outbox(conn, profile_id)
        admins = [await _outbox(conn, admin_id) for admin_id in admin_ids]

    assert state == ("PENDING_AUTH", "BR-D01")
    assert error == "google:owner"
    assert user == [(MESSAGES["BR-D01"].format(provider="Google"), "BR-D01", [])]
    assert admins == [
        [
            (
                MESSAGES["BR-D01_admin"].format(provider="google", account=ACCOUNT),
                "BR-D01_admin",
                [],
            )
        ]
        for _ in admin_ids
    ]


async def test_a_cancel_during_the_google_call_is_not_overwritten_by_the_park(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """BR-D01, R-9.4: a cancel before the park keeps CANCELLED and queues no BR-D01."""
    profile_id = f"google-cancel-{uuid4().hex}"
    cfg = _config(profile_id)
    seeded = await _delivering_invoice(db_pool, tmp_path, profile_id)
    fake = FakeGoogle()
    calls = 0

    async def cancel_then_require_auth(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            async with db_pool.connection() as conn, conn.transaction():
                await set_state(conn, seeded.invoice_id, CANCELLED, rule_id="BR-D06")
            raise AuthRequired(ACCOUNT)
        return fake.handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(cancel_then_require_auth)) as http:
        drive = GoogleDrive(http, _Tokens(), TIMEOUT_S, account=ACCOUNT)
        sheets = GoogleSheets(http, _Tokens(), TIMEOUT_S, account=ACCOUNT, max_rows=MAX_ROWS)
        handler = make_google_deliver_handler(GoogleDeliverContext(db_pool, cfg, drive, sheets, cfg.profiles))
        with pytest.raises(StateMoved):
            await handler(_job(seeded.invoice_id))

    async with db_pool.connection() as conn:
        state = await _invoice_state(conn, seeded.invoice_id)
        outbox = await _outbox(conn, profile_id)

    assert state == ("CANCELLED", "BR-D06")
    assert [rule for _, rule, _ in outbox] == []


async def test_a_503_raises_and_leaves_the_invoice_delivering(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-6.4: a transient Google failure propagates and the invoice stays DELIVERING."""
    profile_id = f"google-503-{uuid4().hex}"
    cfg = _config(profile_id)
    seeded = await _delivering_invoice(db_pool, tmp_path, profile_id)
    fake = FakeGoogle(fail=503)

    async with _handler(db_pool, cfg, fake) as handler:
        with pytest.raises(GoogleTransient, match="status 503"):
            await handler(_job(seeded.invoice_id))

    async with db_pool.connection() as conn:
        state = await _invoice_state(conn, seeded.invoice_id)
        outbox = await _outbox(conn, profile_id)

    assert state == ("DELIVERING", None)
    assert outbox == []


async def test_done_deletes_the_local_photo_pdf_and_xml(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-9.7: after DONE commits, the local photo, PDF and XML files are gone."""
    profile_id = f"google-cleanup-{uuid4().hex}"
    cfg = _config(profile_id)
    seeded = await _delivering_invoice(db_pool, tmp_path, profile_id)
    fake = FakeGoogle()

    async with _handler(db_pool, cfg, fake) as handler:
        await handler(_job(seeded.invoice_id))

    assert seeded.photo is not None
    assert seeded.pdf is not None
    assert seeded.xml is not None
    assert not seeded.photo.exists()
    assert not seeded.pdf.exists()
    assert not seeded.xml.exists()


async def test_invoice_outside_delivering_is_untouched(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-9.4: an invoice already DONE is not filed, appended or messaged again."""
    profile_id = f"google-done-{uuid4().hex}"
    cfg = _config(profile_id)
    seeded = await _delivering_invoice(db_pool, tmp_path, profile_id, state="DONE")
    fake = FakeGoogle()

    async with _handler(db_pool, cfg, fake) as handler:
        await handler(_job(seeded.invoice_id))

    async with db_pool.connection() as conn:
        state = await _invoice_state(conn, seeded.invoice_id)
        outbox = await _outbox(conn, profile_id)

    assert state == ("DONE", None)
    assert outbox == []
    assert fake.uploaded == []
    assert fake.appended == []
