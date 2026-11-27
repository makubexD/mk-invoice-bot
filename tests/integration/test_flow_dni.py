"""The owner's DNI boleta end to end: QR, BR-R10, a Sheets-only flow and BR-D18 (D143, D144).

The photo, extraction, routing, validation and fetching run through the real pipeline and real
Postgres. SUNAT validation is off (``secrets is None``), exactly as in ``test_flow_a.py``. The
``personal_dni`` flow has no fetchers and no required documents, so fetching moves the invoice
straight to ``DELIVERING`` (D144); the delivery then writes only a Sheets row and completes with
BR-D18. Sheets is one stateful ``httpx.MockTransport``; no Drive client is built.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import cv2
import httpx
import numpy as np
import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import Attachment, InboundMessage
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, StoreDirectory
from invoicebot.pipeline.deliver_dispatch import make_deliver_dispatch
from invoicebot.pipeline.deliver_google import GoogleDeliverContext, make_google_deliver_handler
from invoicebot.pipeline.extract import ROUTE_STEP, ExtractInputs, make_extract_handler
from invoicebot.pipeline.fetch import FETCH_STEP, make_fetch_handler
from invoicebot.pipeline.gaps import GapContext
from invoicebot.pipeline.handlers import EXTRACT_STEP
from invoicebot.pipeline.intake import handle_inbound
from invoicebot.pipeline.route import make_route_handler
from invoicebot.pipeline.steps import DELIVER_STEP
from invoicebot.pipeline.summary_post import post_summary
from invoicebot.pipeline.validate import VALIDATE_STEP, make_validate_handler
from invoicebot.queue.jobs import Job, enqueue
from invoicebot.queue.worker import Handler
from invoicebot.routing.router import compile_rules
from invoicebot.sinks.google_sheets import ROW_LINK_TEMPLATE, GoogleSheets
from invoicebot.store.invoices import FileRecord, add_file

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
PARSING_RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "parsing_rules.yaml"
#: The owner profile in example.yaml; the test copies it under a unique id (D93).
PROFILE = "yo"
FLOW = "personal_dni"
ACCOUNT = "owner"
#: example.yaml's personal_dni sheet, tab and columns (D143).
SPREADSHEET_ID = "DNI_SHEET_ID_PLACEHOLDER"
TAB = "Boletas"
SHEET_ID = 123
TIMEOUT_S = 5.0
MAX_ROWS = 100
ROW_NUMBER = 2
SHEETS_HOST = "sheets.googleapis.com"
#: From tests/synthetic_rucs.txt; no real RUC may appear here.
ISSUER_RUC = "20000000001"
#: The owner's synthetic DNI (D143).
OWNER_DNI = "40000000"
TIPO_BOLETA = "03"
TIPO_FACTURA = "01"
SERIE_BOLETA = "B001"
SERIE_FACTURA = "F001"
IGV = "7.61"
TOTAL = "49.90"
BEARER = "synthetic-bearer-value"
CANVAS_PX = 800
WHITE = 255
QR_SCALE = 6
QR_OFFSET_PX = 100


def _config() -> tuple[Config, str]:
    """Return the example config with the owner profile under a unique id, and that id (D93)."""
    cfg = load_config(EXAMPLE)
    profile_id = f"dni-{uuid4().hex}"
    return cfg.model_copy(update={"profiles": {profile_id: cfg.profiles[PROFILE]}}), profile_id


def _payload(fecha: str, *, tipo: str, serie: str, numero: str) -> str:
    """Return a SUNAT QR payload for ``tipo`` sold to the owner's DNI (BR-R10)."""
    return f"{ISSUER_RUC}|{tipo}|{serie}|{numero}|{IGV}|{TOTAL}|{fecha}|1|{OWNER_DNI}"


def _write_photo(path: Path, payload: str) -> Path:
    """Write a white PNG photo carrying ``payload`` as a QR code (R-2.2)."""
    canvas = np.full((CANVAS_PX, CANVAS_PX), WHITE, dtype=np.uint8)
    code = cv2.QRCodeEncoder.create().encode(payload)
    code = cv2.resize(code, None, fx=QR_SCALE, fy=QR_SCALE, interpolation=cv2.INTER_NEAREST)
    height, width = code.shape
    canvas[QR_OFFSET_PX : QR_OFFSET_PX + height, QR_OFFSET_PX : QR_OFFSET_PX + width] = code
    ok, encoded = cv2.imencode(".png", canvas)
    assert ok
    path.write_bytes(encoded.tobytes())
    return path


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


async def _state(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int) -> tuple[str, str | None]:
    """Return the invoice's ``(state, rule_id)``."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT state, rule_id FROM invoices WHERE id = %s", (invoice_id,))
        row = await cur.fetchone()
    assert row is not None
    return str(row[0]), None if row[1] is None else str(row[1])


async def _outbox(
    pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int, rule_id: str
) -> list[str]:
    """Return the texts of the invoice's queued messages with ``rule_id``."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT payload_json->>'text'
            FROM outbox
            WHERE invoice_id = %s AND payload_json->>'rule_id' = %s
            ORDER BY id
            """,
            (invoice_id, rule_id),
        )
        rows = await cur.fetchall()
    return [str(row[0]) for row in rows]


async def _outbox_links(
    pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int, rule_id: str
) -> list[Any]:
    """Return the links of the invoice's queued messages with ``rule_id`` (D194)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT payload_json->'links'
            FROM outbox
            WHERE invoice_id = %s AND payload_json->>'rule_id' = %s
            ORDER BY id
            """,
            (invoice_id, rule_id),
        )
        rows = await cur.fetchall()
    return [row[0] for row in rows]


async def _fetch_attempts(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int) -> int:
    """Return how many fetch attempts the invoice recorded (R-5.7)."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM fetch_attempts WHERE invoice_id = %s", (invoice_id,))
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _read_photo(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    cfg: Config,
    profile_id: str,
    payload: str,
    tmp_path: Path,
) -> int:
    """Send one photo through intake and extraction, leaving it in ``ROUTING`` (S3.11)."""
    msg = InboundMessage(
        channel="whatsapp",
        external_id=f"ext-{uuid4().hex}",
        profile_id=profile_id,
        sender_role="owner",
        attachments=[Attachment(kind="photo", media_ref=f"ref-{uuid4().hex}", mime="image/png", size=1024)],
        received_at=datetime.now(UTC),
    )
    async with pool.connection() as conn:
        intake = await handle_inbound(
            conn,
            msg,
            max_queued_per_sender=cfg.limits.max_queued_per_sender,
            media_max_bytes=cfg.limits.media.max_bytes,
            timezone=cfg.owner.timezone,
        )
    assert intake.invoice_id is not None
    invoice_id = intake.invoice_id
    photo = _write_photo(tmp_path / f"{invoice_id}.png", payload)
    data = photo.read_bytes()
    record = FileRecord(
        kind="photo",
        local_path=str(photo),
        remote_ref=msg.attachments[0].media_ref,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
    )
    async with pool.connection() as conn, conn.transaction():
        await add_file(conn, invoice_id, record)
        extract_job = await enqueue(conn, invoice_id, EXTRACT_STEP)
    await make_extract_handler(
        pool,
        ExtractInputs(
            cfg.extraction,
            cfg.limits.media,
            PARSING_RULES_PATH,
            frozenset({cfg.owner.ruc, cfg.work.ruc}),
        ),
        GapContext(profiles=cfg.profiles, gaps_cfg=cfg.gaps),
        helper_prefix=(),
    )(_job(extract_job, invoice_id, EXTRACT_STEP))
    return invoice_id


async def _route(pool: AsyncConnectionPool[AsyncConnection[Any]], cfg: Config, invoice_id: int) -> None:
    """Run the route handler on the invoice's route job (S4.2)."""
    handler = make_route_handler(pool, cfg, StoreDirectory(stores={}, portals={}), compile_rules(cfg.routing))
    await handler(await _newest_job(pool, invoice_id, ROUTE_STEP))


async def _validate(pool: AsyncConnectionPool[AsyncConnection[Any]], cfg: Config, invoice_id: int) -> None:
    """Run the validate handler with SUNAT off, as test_flow_a.py does (D124)."""
    async with httpx.AsyncClient() as http:
        handler = make_validate_handler(pool, http, None, cfg)
        await handler(await _newest_job(pool, invoice_id, VALIDATE_STEP))
    # D206: the fetch step's first attempt posts the summary; stand in for it here
    async with pool.connection() as conn, conn.transaction():
        await post_summary(conn, cfg, invoice_id)


async def _fetch(
    pool: AsyncConnectionPool[AsyncConnection[Any]], cfg: Config, invoice_id: int, tmp_path: Path
) -> None:
    """Run the fetch handler on the invoice's fetch job (R-5.1)."""
    handler = make_fetch_handler(pool, cfg, StoreDirectory(stores={}, portals={}), {}, tmp_path)
    await handler(await _newest_job(pool, invoice_id, FETCH_STEP))


class _Tokens:
    """A Google token source for the fake Sheets client (R-6.1)."""

    async def token(self) -> str:
        return BEARER


class _Google:
    """A Sheets backend for one delivery, served by ``httpx.MockTransport`` (R-6.2)."""

    def __init__(self) -> None:
        #: Every request the delivery made, so a test can prove none went to Drive (D144).
        self.requests: list[httpx.Request] = []
        #: The rows appended, in order.
        self.rows: list[list[str]] = []

    @property
    def drive_requests(self) -> list[httpx.Request]:
        """Return the requests routed to Drive (D144)."""
        return [request for request in self.requests if request.url.host != SHEETS_HOST]

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Route one request to the fake Sheets backend."""
        self.requests.append(request)
        if request.url.host != SHEETS_HOST:
            return httpx.Response(404)
        return self._sheets(request)

    def _sheets(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == f"/v4/spreadsheets/{SPREADSHEET_ID}":
            return httpx.Response(200, json={"sheets": [{"properties": {"sheetId": SHEET_ID, "title": TAB}}]})
        if request.method == "GET" and "/values/" in path:
            return httpx.Response(200, json={"values": [["Clave"], *[[row[0]] for row in self.rows]]})
        if request.method == "POST" and path.endswith(":append"):
            self.rows.append(json.loads(request.content)["values"][0])
            row = len(self.rows) + 1
            return httpx.Response(200, json={"updates": {"updatedRange": f"{TAB}!A{row}:G{row}"}})
        return httpx.Response(404)


@asynccontextmanager
async def _dispatch(
    pool: AsyncConnectionPool[AsyncConnection[Any]], cfg: Config, google: _Google, data_dir: Path
) -> AsyncIterator[Handler]:
    """Build the app's ``deliver`` dispatcher with the Sheets-only DNI flow over the fake."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(google.handler)) as http:
        sheets = GoogleSheets(http, _Tokens(), TIMEOUT_S, account=ACCOUNT, max_rows=MAX_ROWS)
        deliver = make_google_deliver_handler(
            GoogleDeliverContext(pool, cfg, {}, {ACCOUNT: sheets}, cfg.profiles, data_dir)
        )
        yield make_deliver_dispatch(pool, {FLOW: deliver})


async def test_a_dni_boleta_routes_to_its_sheet_and_ends_done_with_br_d18(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """D194, BR-D18: a QR boleta to the owner's DNI routes by BR-R10, skips fetching and links its row."""
    cfg, profile_id = _config()
    today = datetime.now(ZoneInfo(cfg.owner.timezone)).date()
    numero = "81001"
    payload = _payload(today.isoformat(), tipo=TIPO_BOLETA, serie=SERIE_BOLETA, numero=numero)

    invoice_id = await _read_photo(db_pool, cfg, profile_id, payload, tmp_path)
    assert await _state(db_pool, invoice_id) == ("ROUTING", None)

    await _route(db_pool, cfg, invoice_id)
    assert await _state(db_pool, invoice_id) == ("VALIDATING", "BR-R10")

    await _validate(db_pool, cfg, invoice_id)
    assert await _state(db_pool, invoice_id) == ("FETCHING_DOCS", "BR-R10")

    await _fetch(db_pool, cfg, invoice_id, tmp_path)
    assert await _state(db_pool, invoice_id) == ("DELIVERING", None)

    google = _Google()
    async with _dispatch(db_pool, cfg, google, tmp_path) as dispatch:
        await dispatch(await _newest_job(db_pool, invoice_id, DELIVER_STEP))

    assert await _state(db_pool, invoice_id) == ("DONE", "BR-D18")
    assert google.drive_requests == []
    assert len(google.rows) == 1
    assert google.rows[0][0] == f"{profile_id}|{ISSUER_RUC}|{TIPO_BOLETA}|{SERIE_BOLETA}|{numero}"
    assert google.rows[0][4] == f"{SERIE_BOLETA}-{numero}"
    assert google.rows[0][3] == f"RUC {ISSUER_RUC}"
    assert await _fetch_attempts(db_pool, invoice_id) == 0

    summary = await _outbox(db_pool, invoice_id, "BR-R10")
    assert len(summary) == 1
    assert "➡️ Boletas a mi DNI" in summary[0]
    assert f"{SERIE_BOLETA}-{numero}" in summary[0]

    row_link = ROW_LINK_TEMPLATE.format(
        spreadsheet_id=SPREADSHEET_ID, sheet_id=SHEET_ID, row_number=ROW_NUMBER
    )
    completion = await _outbox(db_pool, invoice_id, "BR-D18")
    assert len(completion) == 1
    assert completion[0].startswith(f"✅ Registrado {SERIE_BOLETA}-{numero}")
    assert f"de RUC {ISSUER_RUC}" in completion[0]
    assert "None" not in completion[0]
    assert "Fila:" not in completion[0]
    assert await _outbox_links(db_pool, invoice_id, "BR-D18") == [
        [{"title": "📄 Abrir fila", "url": row_link}]
    ]


async def test_the_same_dni_boleta_sent_twice_is_cancelled_as_a_duplicate(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """D144: a second identical DNI boleta ends BR-E04 while the first still holds its key."""
    cfg, profile_id = _config()
    today = datetime.now(ZoneInfo(cfg.owner.timezone)).date()
    numero = "81002"
    payload = _payload(today.isoformat(), tipo=TIPO_BOLETA, serie=SERIE_BOLETA, numero=numero)

    first = await _read_photo(db_pool, cfg, profile_id, payload, tmp_path)
    await _route(db_pool, cfg, first)
    assert await _state(db_pool, first) == ("VALIDATING", "BR-R10")

    second = await _read_photo(db_pool, cfg, profile_id, payload, tmp_path)
    await _route(db_pool, cfg, second)

    assert await _state(db_pool, second) == ("CANCELLED", "BR-E04")
    duplicates = await _outbox(db_pool, second, "BR-E04")
    assert len(duplicates) == 1
    assert f"(#{first})" in duplicates[0]


async def test_a_factura_to_the_owners_dni_is_refused_by_br_r08(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """BR-R10: a tipo 01 factura to the owner's DNI matches BR-R10 and is cancelled by BR-R08."""
    cfg, profile_id = _config()
    today = datetime.now(ZoneInfo(cfg.owner.timezone)).date()
    payload = _payload(today.isoformat(), tipo=TIPO_FACTURA, serie=SERIE_FACTURA, numero="81003")

    invoice_id = await _read_photo(db_pool, cfg, profile_id, payload, tmp_path)
    await _route(db_pool, cfg, invoice_id)

    assert await _state(db_pool, invoice_id) == ("CANCELLED", "BR-R08")
    refusal = await _outbox(db_pool, invoice_id, "BR-R08")
    assert len(refusal) == 1
    assert "Boletas a mi DNI" in refusal[0]
    assert "facturas" not in refusal[0]
