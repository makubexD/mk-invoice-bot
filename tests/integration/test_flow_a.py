"""Flow A end to end: a photo becomes a filed invoice in Google Drive and Sheets (S9, D132).

The photo, extraction, routing, fetching and delivery all run through the real pipeline and
real Postgres. The fetcher, Drive and Sheets are the test's fakes; the delivery goes through
:func:`invoicebot.pipeline.deliver_dispatch.make_deliver_dispatch`, the handler the app
registers for the ``deliver`` step. ``read_conversation`` drives the photo to its summary and
leaves the invoice in ``FETCHING_DOCS``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.auth.msal_cache import AuthRequired
from invoicebot.channels.fake import FakeChannel
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, StoreDirectory
from invoicebot.fetchers.base import FetchedFile, FetchInvoice, FetchReason, FetchResult
from invoicebot.pipeline.deliver_dispatch import make_deliver_dispatch
from invoicebot.pipeline.deliver_google import GoogleDeliverContext, make_google_deliver_handler
from invoicebot.pipeline.fetch import FETCH_STEP, make_fetch_handler
from invoicebot.pipeline.steps import DELIVER_STEP
from invoicebot.queue.jobs import Job, enqueue
from invoicebot.queue.worker import Handler
from invoicebot.sinks.google_drive import GoogleDrive
from invoicebot.sinks.google_sheets import ROW_LINK_TEMPLATE, GoogleSheets
from invoicebot.store.invoices import resume_pending_auth

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
FLOW = "personal_google"
ACCOUNT = "owner"
SPREADSHEET_ID = "SHEET_ID"
TAB = "Compras"
SHEET_ID = 123
TIMEOUT_S = 5.0
MAX_ROWS = 100
ROW_NUMBER = 2
#: The owner's company RUC from tests/synthetic_rucs.txt; flow A's buyer (D132).
OWNER_RUC = "20000000001"
RAZON_SOCIAL = "Restaurante La Lena S.A.C."
PDF_BYTES = b"%PDF-1.4 synthetic invoice\n"
BEARER = "synthetic-bearer-value"
_ACK_ID = re.compile(r"#(\d+)")


def _invoice_id(fake: FakeChannel) -> int:
    """Return the invoice id the BR-S09 acknowledgement names."""
    match = _ACK_ID.search(fake.texts()[0])
    assert match is not None
    return int(match.group(1))


def _config() -> Config:
    """Return the example config, whose ``personal_google`` flow is flow A (D132)."""
    return load_config(EXAMPLE)


def _job(job_id: int, invoice_id: int, step: str) -> Job:
    """Build the claimed job a handler receives."""
    return Job(
        id=job_id, invoice_id=invoice_id, step=step, attempts=0, crashes=0, lease_until=datetime.now(UTC)
    )


async def _newest_job(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int, step: str) -> Job:
    """Return the invoice's newest job of ``step``."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT max(id) FROM jobs WHERE invoice_id = %s AND step = %s", (invoice_id, step)
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] is not None
    return _job(int(row[0]), invoice_id, step)


def _written(out_dir: Path, kind: Literal["pdf", "xml"], data: bytes) -> FetchedFile:
    """Write one synthetic file into the fetcher's output directory and describe it (R-5.1)."""
    path = out_dir / f"invoice.{kind}"
    path.write_bytes(data)
    return FetchedFile(kind=kind, path=path, size=len(data), sha256=hashlib.sha256(data).hexdigest())


def _matching_xml(payload: str) -> bytes:
    """Return a synthetic UBL invoice matching the photo's QR match fields (R-5.6)."""
    issuer, tipo, serie, numero, igv, total, fecha = payload.split("|")[:7]
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2">
  <ID>{serie}-{numero}</ID>
  <IssueDate>{fecha}</IssueDate>
  <InvoiceTypeCode>{tipo}</InvoiceTypeCode>
  <AccountingSupplierParty>
    <Party>
      <PartyIdentification><ID>{issuer}</ID></PartyIdentification>
      <PartyLegalEntity><RegistrationName>{RAZON_SOCIAL}</RegistrationName></PartyLegalEntity>
    </Party>
  </AccountingSupplierParty>
  <TaxTotal><TaxAmount>{igv}</TaxAmount></TaxTotal>
  <LegalMonetaryTotal>
    <LineExtensionAmount>{total}</LineExtensionAmount>
    <PayableAmount>{total}</PayableAmount>
  </LegalMonetaryTotal>
</Invoice>
""".encode()


class _MatchingFetcher:
    """A ``Fetcher`` that returns the PDF and XML matching the photo's QR (R-5.1, R-5.6)."""

    def __init__(self, payload: str) -> None:
        self._payload = payload

    async def fetch(self, invoice: FetchInvoice, out_dir: Path) -> FetchResult:
        del invoice
        files = (
            _written(out_dir, "pdf", PDF_BYTES),
            _written(out_dir, "xml", _matching_xml(self._payload)),
        )
        return FetchResult(FetchReason.OK, files, None)


class _Tokens:
    """A Google token source the test can revoke, like an account that lost its grant (BR-D01)."""

    def __init__(self, *, revoked: bool = False) -> None:
        self.revoked = revoked

    async def token(self) -> str:
        if self.revoked:
            raise AuthRequired(ACCOUNT)
        return BEARER


def _upload_name(request: httpx.Request) -> str:
    """Return the file name from the JSON metadata part of a multipart upload (R-6.1)."""
    boundary = request.headers["Content-Type"].split("boundary=", 1)[1].strip('"')
    part = request.content.split(b"--" + boundary.encode())[1]
    metadata = part.split(b"\r\n\r\n", 1)[1].split(b"\r\n", 1)[0]
    return str(json.loads(metadata)["name"])


class _Google:
    """A Drive and Sheets backend for one delivery, served by ``httpx.MockTransport`` (R-6.1, R-6.2)."""

    def __init__(self) -> None:
        #: The names of the files Drive received, in upload order.
        self.uploaded: list[str] = []
        #: The rows Sheets appended, in order.
        self.rows: list[list[str]] = []
        self._next_folder = 1

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Route one request to the fake Drive or the fake Sheets."""
        if request.url.host == "www.googleapis.com":
            return self._drive(request)
        return self._sheets(request)

    def _drive(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/drive/v3/files":
            return httpx.Response(200, json={"files": []})
        if request.method == "POST" and path == "/drive/v3/files":
            folder_id = f"folder_{self._next_folder:012d}"
            self._next_folder += 1
            return httpx.Response(200, json={"id": folder_id})
        if request.method == "POST" and path == "/upload/drive/v3/files":
            self.uploaded.append(_upload_name(request))
            file_id = f"file_{len(self.uploaded):012d}"
            return httpx.Response(
                200,
                json={"id": file_id, "webViewLink": f"https://drive.google.com/file/d/{file_id}/view"},
            )
        return httpx.Response(404)

    def _sheets(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == f"/v4/spreadsheets/{SPREADSHEET_ID}":
            return httpx.Response(200, json={"sheets": [{"properties": {"sheetId": SHEET_ID, "title": TAB}}]})
        if request.method == "GET" and "/values/" in path:
            return httpx.Response(200, json={"values": [["Clave"], *[[row[0]] for row in self.rows]]})
        if request.method == "POST" and path.endswith(":append"):
            self.rows.append(json.loads(request.content)["values"][0])
            row = len(self.rows) + 1
            return httpx.Response(200, json={"updates": {"updatedRange": f"{TAB}!A{row}:K{row}"}})
        return httpx.Response(404)


@asynccontextmanager
async def _dispatch(
    pool: AsyncConnectionPool[AsyncConnection[Any]], cfg: Config, google: _Google, tokens: _Tokens
) -> AsyncIterator[Handler]:
    """Build the app's ``deliver`` dispatcher with flow A's Google deliverer over the fake."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(google.handler)) as http:
        drive = GoogleDrive(http, tokens, TIMEOUT_S, account=ACCOUNT)
        sheets = GoogleSheets(http, tokens, TIMEOUT_S, account=ACCOUNT, max_rows=MAX_ROWS)
        deliver = make_google_deliver_handler(GoogleDeliverContext(pool, cfg, drive, sheets, cfg.profiles))
        yield make_deliver_dispatch(pool, {FLOW: deliver})


async def _fetch_documents(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    cfg: Config,
    invoice_id: int,
    payload: str,
    data_dir: Path,
) -> None:
    """Run the one registered fetcher on the invoice, moving it to ``DELIVERING`` (R-5.1)."""
    handler = make_fetch_handler(
        pool,
        cfg,
        StoreDirectory(stores={}, portals={}),
        {FLOW: {"qr_url": _MatchingFetcher(payload)}},
        data_dir,
    )
    await handler(await _newest_job(pool, invoice_id, FETCH_STEP))


async def _state(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int) -> tuple[str, str | None]:
    """Return the invoice's ``(state, rule_id)``."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT state, rule_id FROM invoices WHERE id = %s", (invoice_id,))
        row = await cur.fetchone()
    assert row is not None
    return str(row[0]), None if row[1] is None else str(row[1])


async def _completion_text(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int) -> str:
    """Return the BR-D16 completion message queued for the invoice."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT payload_json->>'text'
            FROM outbox
            WHERE invoice_id = %s AND payload_json->>'rule_id' = 'BR-D16'
            """,
            (invoice_id,),
        )
        rows = await cur.fetchall()
    assert len(rows) == 1
    return str(rows[0][0])


async def test_a_photo_is_fetched_and_filed_in_drive_and_sheets(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    read_conversation: Any,
) -> None:
    """R-6.1, R-6.4, BR-D16: a routed photo ends DONE with its files in Drive and its row linked."""
    fake_channel, payload = await read_conversation(True, buyer_ruc=OWNER_RUC, flow=FLOW)
    invoice_id = _invoice_id(fake_channel)
    cfg = _config()
    await _fetch_documents(db_pool, cfg, invoice_id, payload, tmp_path)
    assert await _state(db_pool, invoice_id) == ("DELIVERING", None)

    google = _Google()
    async with _dispatch(db_pool, cfg, google, _Tokens()) as dispatch:
        await dispatch(await _newest_job(db_pool, invoice_id, DELIVER_STEP))

    assert await _state(db_pool, invoice_id) == ("DONE", "BR-D16")
    assert [name.rsplit(".", 1)[-1] for name in google.uploaded] == ["jpg", "pdf", "xml"]
    assert len(google.rows) == 1
    row_link = ROW_LINK_TEMPLATE.format(
        spreadsheet_id=SPREADSHEET_ID, sheet_id=SHEET_ID, row_number=ROW_NUMBER
    )
    assert row_link in await _completion_text(db_pool, invoice_id)


async def test_a_revoked_google_token_parks_and_resume_finishes_the_delivery(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    read_conversation: Any,
) -> None:
    """BR-D01: a revoked token parks the invoice, and resume plus the deliver job finish it DONE."""
    fake_channel, payload = await read_conversation(True, buyer_ruc=OWNER_RUC, flow=FLOW)
    invoice_id = _invoice_id(fake_channel)
    cfg = _config()
    await _fetch_documents(db_pool, cfg, invoice_id, payload, tmp_path)
    assert await _state(db_pool, invoice_id) == ("DELIVERING", None)

    tokens = _Tokens(revoked=True)
    google = _Google()
    async with _dispatch(db_pool, cfg, google, tokens) as dispatch:
        await dispatch(await _newest_job(db_pool, invoice_id, DELIVER_STEP))
        assert await _state(db_pool, invoice_id) == ("PENDING_AUTH", "BR-D01")

        async with db_pool.connection() as conn, conn.transaction():
            resumed = await resume_pending_auth(conn)
            await enqueue(conn, invoice_id, DELIVER_STEP)
        assert invoice_id in resumed

        tokens.revoked = False
        await dispatch(await _newest_job(db_pool, invoice_id, DELIVER_STEP))

    assert await _state(db_pool, invoice_id) == ("DONE", "BR-D16")
