"""The S8.7 end-to-end resume: a new portal unsticks an invoice and its gap closes (R-11.9, D157)."""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
import yaml
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.config.loader import load_config, load_directory
from invoicebot.config.models import Config
from invoicebot.extract.fields import Field, Reading
from invoicebot.fetchers.qr_url import QrUrlFetcher, allowed_hosts
from invoicebot.gaps.kinds import fingerprint
from invoicebot.pipeline.fetch import FETCH_STEP, make_fetch_handler
from invoicebot.pipeline.reevaluate import reevaluate
from invoicebot.queue.jobs import Job
from invoicebot.rules.messages_es import MESSAGES
from invoicebot.states import DELIVERING, FETCHING_DOCS, MANUAL_REQUIRED
from invoicebot.store.fetch_attempts import record_attempt
from invoicebot.store.invoices import create_invoice, save_reading, set_flow, set_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
EXAMPLE = CONFIG_DIR / "example.yaml"
# From tests/synthetic_rucs.txt; no real RUC may appear here.
ISSUER_RUC = "20000000001"
PORTAL_KEY = "synthetic_portal"
FLOW = "work_microsoft"
SERIE = "F001"
NUMERO = "15160"
FECHA = "2026-11-13"
TOTAL = "49.90"
DOCUMENT_URL = "https://portal.example/invoice"
ISSUE_URL = "https://github.com/owner/mk-invoice-bot-ops/issues/7"
GAP_FINGERPRINT = fingerprint("PORTAL_MISSING", ISSUER_RUC)
#: A final reason per fetcher, as a flow that ran to exhaustion leaves its attempts (R-5.4).
EXHAUSTED_REASON = {
    "qr_url": "technical",
    "portal_registry": "no_portal",
    "email_inbox": "not_found",
    "manual": "missing_docs",
}

XML_BYTES = f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>{SERIE}-{NUMERO}</cbc:ID>
  <cbc:IssueDate>{FECHA}</cbc:IssueDate>
  <cbc:InvoiceTypeCode>01</cbc:InvoiceTypeCode>
  <cac:AccountingSupplierParty>
    <cac:Party>
      <cac:PartyIdentification>
        <cbc:ID schemeID="6">{ISSUER_RUC}</cbc:ID>
      </cac:PartyIdentification>
      <cac:PartyLegalEntity>
        <cbc:RegistrationName>COMERCIO SINTETICO S.A.C.</cbc:RegistrationName>
      </cac:PartyLegalEntity>
    </cac:Party>
  </cac:AccountingSupplierParty>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="PEN">42.29</cbc:LineExtensionAmount>
    <cbc:PayableAmount currencyID="PEN">{TOTAL}</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
</Invoice>
""".encode()


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _clean_gap_state(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> AsyncIterator[None]:
    """Start each test from empty gaps, links and outbox rows."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM invoice_gaps")
        await conn.execute("DELETE FROM gaps")
        await conn.execute("DELETE FROM outbox")
    yield


def _copy_directory_fixture(tmp_path: Path) -> Path:
    """Copy the ``stores``/``portals`` fixture ``load_directory`` reads into ``tmp_path`` (D47)."""
    config_dir = tmp_path / "config"
    shutil.copytree(CONFIG_DIR / "stores", config_dir / "stores")
    shutil.copytree(CONFIG_DIR / "portals", config_dir / "portals")
    shutil.copy(CONFIG_DIR / "sunat_sol.yaml", config_dir / "sunat_sol.yaml")
    return config_dir


def _write_yaml(path: Path, document: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _add_active_store(config_dir: Path) -> None:
    """Add the synthetic issuer's store and its active portal to the temp directory (R-5.8)."""
    _write_yaml(
        config_dir / "stores" / f"{ISSUER_RUC}.yaml",
        {
            "ruc": ISSUER_RUC,
            "razon_social": "Comercio Sintético",
            "tipo": "01",
            "brands": [{"comercio": "Tienda Sintética"}],
            "portal": PORTAL_KEY,
        },
    )
    _write_yaml(
        config_dir / "portals" / f"{PORTAL_KEY}.yaml",
        {"url": DOCUMENT_URL, "adapter": "generic_form", "status": "active"},
    )


def _config() -> Config:
    """Return the example config, as startup loads it (R-9.6)."""
    return load_config(EXAMPLE)


def _fetch_config() -> Config:
    """Return the example config with the work flow fetching only the XML through its QR URL."""
    cfg = _config()
    flow = cfg.flows[FLOW].model_copy(update={"fetchers": ["qr_url"], "required_docs": ["xml"]})
    return cfg.model_copy(update={"flows": {**cfg.flows, FLOW: flow}})


def _field(value: str) -> Field:
    return Field(value=value, source="qr", confidence=1.0)


def _reading() -> Reading:
    """Build the stored reading the resumed invoice carries into the fetch step."""
    return Reading(
        fields={
            "ruc_emisor": _field(ISSUER_RUC),
            "tipo": _field("01"),
            "serie": _field(SERIE),
            "numero": _field(NUMERO),
            "total": _field(TOTAL),
            "fecha_emision": _field(FECHA),
        },
        document_url=DOCUMENT_URL,
    )


def _portal_reply(request: httpx.Request) -> httpx.Response:
    """Answer the portal's one document URL with the invoice's XML, and fail on anything else."""
    if str(request.url) != DOCUMENT_URL:
        pytest.fail(f"unexpected portal request: {request.method} {request.url}")
    return httpx.Response(200, content=XML_BYTES)


async def _invoice_state(conn: AsyncConnection[Any], invoice_id: int) -> str | None:
    cur = await conn.execute("SELECT state FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    return None if row is None else str(row[0])


async def _gap_state(conn: AsyncConnection[Any], gap_fingerprint: str) -> str | None:
    cur = await conn.execute("SELECT state FROM gaps WHERE fingerprint = %s", (gap_fingerprint,))
    row = await cur.fetchone()
    return None if row is None else str(row[0])


async def _sender_outbox(conn: AsyncConnection[Any], profile_id: str) -> list[tuple[str, str, str]]:
    """Return each sender row's rule id, text and channel, oldest first."""
    cur = await conn.execute(
        """
        SELECT payload_json->>'rule_id', payload_json->>'text', channel
        FROM outbox
        WHERE "to" = %s
        ORDER BY id
        """,
        (profile_id,),
    )
    return [(str(rule_id), str(text), str(channel)) for rule_id, text, channel in await cur.fetchall()]


async def _github_rows(conn: AsyncConnection[Any]) -> list[tuple[str, str | None]]:
    """Return each github row's op and issue_url, oldest first."""
    cur = await conn.execute(
        """
        SELECT payload_json->>'op', payload_json->>'issue_url'
        FROM outbox
        WHERE channel = 'github'
        ORDER BY id
        """,
    )
    return [(str(op), issue_url) for op, issue_url in await cur.fetchall()]


async def _fetch_job(conn: AsyncConnection[Any], invoice_id: int) -> Job:
    """Return the fetch job ``reevaluate`` enqueued for the resumed invoice."""
    cur = await conn.execute(
        "SELECT id FROM jobs WHERE invoice_id = %s AND step = %s ORDER BY id DESC LIMIT 1",
        (invoice_id, FETCH_STEP),
    )
    row = await cur.fetchone()
    assert row is not None
    return Job(
        id=int(row[0]),
        invoice_id=invoice_id,
        step=FETCH_STEP,
        attempts=0,
        crashes=0,
        lease_until=datetime.now(UTC),
    )


async def test_an_active_portal_resumes_the_invoice_and_its_fetch_closes_the_gap(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """R-11.9: adding an active portal resumes the invoice, and its fetch closes the gap and its issue."""
    profile_id = f"resume-{uuid4().hex}"
    config_dir = _copy_directory_fixture(tmp_path)
    assert ISSUER_RUC not in load_directory(config_dir).stores
    _add_active_store(config_dir)
    directory = load_directory(config_dir)

    async with db_pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await save_reading(conn, invoice_id, _reading())
        await set_flow(conn, invoice_id, FLOW)
        await set_state(conn, invoice_id, MANUAL_REQUIRED, rule_id="BR-F07")
        await conn.execute(
            """
            INSERT INTO gaps (fingerprint, kind, subject, state, raised_at, issue_url)
            VALUES (%s, %s, %s, 'reported', now(), %s)
            """,
            (GAP_FINGERPRINT, "PORTAL_MISSING", f"RUC {ISSUER_RUC}", ISSUE_URL),
        )
        await conn.execute(
            "INSERT INTO invoice_gaps (invoice_id, fingerprint) VALUES (%s, %s)",
            (invoice_id, GAP_FINGERPRINT),
        )

    await reevaluate(db_pool, _config(), directory)

    async with db_pool.connection() as conn:
        assert await _invoice_state(conn, invoice_id) == FETCHING_DOCS
        assert await _sender_outbox(conn, profile_id) == [
            ("BR-F12", MESSAGES["BR-F12"].format(id=invoice_id), "whatsapp")
        ]
        job = await _fetch_job(conn, invoice_id)

    cfg = _fetch_config()
    async with httpx.AsyncClient(transport=httpx.MockTransport(_portal_reply)) as http:
        fetcher = QrUrlFetcher(
            http,
            allowed_hosts(directory),
            cfg.limits.media.max_bytes,
            cfg.retrieval.portal_timeout_s,
        )
        handler = make_fetch_handler(
            db_pool,
            cfg,
            directory,
            {FLOW: {"qr_url": fetcher}},
            tmp_path / "data",
        )
        await handler(job)

    async with db_pool.connection() as conn:
        assert await _invoice_state(conn, invoice_id) == DELIVERING
        assert await _gap_state(conn, GAP_FINGERPRINT) == "closed"
        assert await _github_rows(conn) == [("close_issue", ISSUE_URL)]


async def test_an_active_portal_resumes_an_invoice_whose_fetchers_were_used_up(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """R-11.9, D157, D166: an active portal resumes an invoice whose fetchers were used up."""
    profile_id = f"resume-{uuid4().hex}"
    config_dir = _copy_directory_fixture(tmp_path)
    assert ISSUER_RUC not in load_directory(config_dir).stores

    async with db_pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await save_reading(conn, invoice_id, _reading())
        await set_flow(conn, invoice_id, FLOW)
        await set_state(conn, invoice_id, MANUAL_REQUIRED, rule_id="BR-F07")
        await conn.execute(
            """
            INSERT INTO gaps (fingerprint, kind, subject, state, raised_at, issue_url)
            VALUES (%s, %s, %s, 'reported', now(), %s)
            """,
            (GAP_FINGERPRINT, "PORTAL_MISSING", f"RUC {ISSUER_RUC}", ISSUE_URL),
        )
        await conn.execute(
            "INSERT INTO invoice_gaps (invoice_id, fingerprint) VALUES (%s, %s)",
            (invoice_id, GAP_FINGERPRINT),
        )

    for fetcher in _config().flows[FLOW].fetchers:
        # Two attempts use the fetcher up today and after D167's one retry is spent.
        for _ in range(2):
            async with db_pool.connection() as conn, conn.transaction():
                await record_attempt(conn, invoice_id, fetcher, None, EXHAUSTED_REASON[fetcher], 1)

    _add_active_store(config_dir)
    directory = load_directory(config_dir)

    await reevaluate(db_pool, _config(), directory)

    async with db_pool.connection() as conn:
        assert await _invoice_state(conn, invoice_id) == FETCHING_DOCS
        assert await _sender_outbox(conn, profile_id) == [
            ("BR-F12", MESSAGES["BR-F12"].format(id=invoice_id), "whatsapp")
        ]
        job = await _fetch_job(conn, invoice_id)

    cfg = _fetch_config()
    async with httpx.AsyncClient(transport=httpx.MockTransport(_portal_reply)) as http:
        fetcher = QrUrlFetcher(
            http,
            allowed_hosts(directory),
            cfg.limits.media.max_bytes,
            cfg.retrieval.portal_timeout_s,
        )
        handler = make_fetch_handler(
            db_pool,
            cfg,
            directory,
            {FLOW: {"qr_url": fetcher}},
            tmp_path / "data",
        )
        await handler(job)

    async with db_pool.connection() as conn:
        assert await _invoice_state(conn, invoice_id) == DELIVERING
        assert await _gap_state(conn, GAP_FINGERPRINT) == "closed"
        assert await _github_rows(conn) == [("close_issue", ISSUE_URL)]
