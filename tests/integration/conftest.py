"""Integration-test fixtures: a migrated test database, an open pool and a read conversation."""

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import cv2
import numpy as np
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from psycopg import AsyncConnection, conninfo, sql
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import Attachment, InboundMessage
from invoicebot.channels.fake import FakeChannel
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config as AppConfig
from invoicebot.config.models import RoutingRule
from invoicebot.pipeline.extract import ROUTE_STEP, make_extract_handler
from invoicebot.pipeline.handlers import EXTRACT_STEP
from invoicebot.pipeline.intake import handle_inbound
from invoicebot.pipeline.route import make_route_handler
from invoicebot.queue.jobs import Job, enqueue
from invoicebot.queue.outbox import OutboxSender
from invoicebot.routing.router import compile_rules
from invoicebot.store.db import create_pool
from invoicebot.store.invoices import FileRecord, add_file

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEST_DATABASE_URL = "postgresql://invoicebot:invoicebot@localhost:5432/invoicebot_test"
MAINTENANCE_DB = "postgres"


def _test_database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


def _maintenance_dsn(url: str) -> tuple[str, str]:
    """Split a test URL into the maintenance-server DSN and the database name."""
    params = conninfo.conninfo_to_dict(url)
    dbname = params.get("dbname")
    if not isinstance(dbname, str) or not dbname:
        raise RuntimeError("TEST_DATABASE_URL must name a database")
    if not dbname.endswith("_test"):
        # The fixture drops this database, so refuse anything that is not a test one.
        raise RuntimeError(f"refusing to reset database {dbname!r}: its name must end with '_test'")
    return conninfo.make_conninfo(**{**params, "dbname": MAINTENANCE_DB}), dbname


async def _reset_database(url: str) -> None:
    """Drop and recreate the test database, failing if Postgres is unreachable."""
    maintenance_dsn, dbname = _maintenance_dsn(url)
    conn = await AsyncConnection.connect(maintenance_dsn, autocommit=True)
    async with conn:
        await conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname)))
        await conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))


def _upgrade(url: str) -> None:
    """Run `alembic upgrade head` against the test database."""
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")
    finally:
        if previous is None:
            del os.environ["DATABASE_URL"]
        else:
            os.environ["DATABASE_URL"] = previous


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def db_pool() -> AsyncIterator[AsyncConnectionPool[AsyncConnection[Any]]]:
    """Reset, migrate and yield an open pool for the test database."""
    url = _test_database_url()
    await _reset_database(url)
    await asyncio.to_thread(_upgrade, url)
    pool = create_pool(url)
    await pool.open(wait=True)
    try:
        yield pool
    finally:
        await pool.close()


# --- A read conversation on the fake channel (S3.11) -------------------------------------

#: From tests/synthetic_rucs.txt; no real RUC may appear here.
ISSUER_RUC = "20000000001"
WORK_RUC = "51900000000"
WORK_FLOW = "work_microsoft"
SERIE = "F001"
NUMERO = "15160"
TOTAL = "49.90"
EXAMPLE_CONFIG = REPO_ROOT / "config" / "example.yaml"
PARSING_RULES_PATH = REPO_ROOT / "config" / "parsing_rules.yaml"
_WHITE = 255
_CANVAS_PX = 800
_QR_SCALE = 6
_QR_OFFSET_PX = 100
_MAX_QUEUED = 10


def _qr_payload(fecha: date) -> str:
    """Return a SUNAT QR payload for a factura from ISSUER_RUC to WORK_RUC."""
    return f"{ISSUER_RUC}|01|{SERIE}|{NUMERO}|7.61|{TOTAL}|{fecha.isoformat()}|6|{WORK_RUC}"


def _write_photo(path: Path, payload: str | None) -> Path:
    """Write a white PNG photo, with ``payload`` as a QR code unless it is None."""
    canvas = np.full((_CANVAS_PX, _CANVAS_PX), _WHITE, dtype=np.uint8)
    if payload is not None:
        code = cv2.QRCodeEncoder.create().encode(payload)
        code = cv2.resize(code, None, fx=_QR_SCALE, fy=_QR_SCALE, interpolation=cv2.INTER_NEAREST)
        height, width = code.shape
        canvas[_QR_OFFSET_PX : _QR_OFFSET_PX + height, _QR_OFFSET_PX : _QR_OFFSET_PX + width] = code
    ok, encoded = cv2.imencode(".png", canvas)
    assert ok
    path.write_bytes(encoded.tobytes())
    return path


def _read_config(profile_id: str) -> AppConfig:
    """Return the example config with ``profile_id`` as the full profile and WORK_RUC routed."""
    cfg = load_config(EXAMPLE_CONFIG)
    routing = cfg.routing.model_copy(
        update={"rules": [RoutingRule(id="BR-R02", when={"num_doc_adq": [WORK_RUC]}, flow=WORK_FLOW)]}
    )
    return cfg.model_copy(update={"profiles": {profile_id: cfg.profiles["yo"]}, "routing": routing})


#: Runs one conversation, with or without a QR, and returns the fake channel and the QR text.
type ReadConversation = Callable[[bool], Awaitable[tuple[FakeChannel, str]]]


@pytest.fixture
def read_conversation(db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path) -> ReadConversation:
    """Return a runner for one photo conversation: intake, extract, route, outbox.

    The download handler needs the WhatsApp client, so the runner stands in for it:
    after intake it writes the photo, adds the ``files`` row and the ``extract`` job,
    then runs the extract and route handlers on their jobs and sends the outbox to a
    fake channel. The outbox is cleared first, so the fake sees only this conversation.
    """

    async def run(with_qr: bool) -> tuple[FakeChannel, str]:
        payload = _qr_payload(datetime.now(UTC).date())
        profile_id = f"profile-{uuid4().hex}"
        cfg = _read_config(profile_id)
        msg = InboundMessage(
            channel="whatsapp",
            external_id=f"ext-{uuid4().hex}",
            profile_id=profile_id,
            sender_role="owner",
            attachments=[
                Attachment(kind="photo", media_ref=f"ref-{uuid4().hex}", mime="image/png", size=1024)
            ],
            received_at=datetime.now(UTC),
        )
        async with db_pool.connection() as conn:
            await conn.execute("DELETE FROM outbox")
            intake = await handle_inbound(conn, msg, max_queued_per_sender=_MAX_QUEUED)
        assert intake.invoice_id is not None
        invoice_id = intake.invoice_id
        photo = _write_photo(tmp_path / f"{invoice_id}.png", payload if with_qr else None)
        data = photo.read_bytes()
        record = FileRecord(
            kind="photo",
            local_path=str(photo),
            remote_ref=msg.attachments[0].media_ref,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
        )
        async with db_pool.connection() as conn, conn.transaction():
            await add_file(conn, invoice_id, record)
            extract_job = await enqueue(conn, invoice_id, EXTRACT_STEP)

        await make_extract_handler(db_pool, cfg.extraction, cfg.limits.media, PARSING_RULES_PATH)(
            _job(extract_job, invoice_id, EXTRACT_STEP)
        )
        async with db_pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id FROM jobs WHERE invoice_id = %s AND step = %s", (invoice_id, ROUTE_STEP)
            )
            route_jobs = [int(row[0]) for row in await cur.fetchall()]
        rules = compile_rules(cfg.routing)
        for job_id in route_jobs:
            await make_route_handler(db_pool, cfg, rules)(_job(job_id, invoice_id, ROUTE_STEP))

        fake = FakeChannel()
        await OutboxSender(db_pool, {"whatsapp": fake.transport()}).send_pending()
        return fake, payload

    return run


def _job(job_id: int, invoice_id: int, step: str) -> Job:
    """Build the claimed job a handler receives."""
    return Job(
        id=job_id, invoice_id=invoice_id, step=step, attempts=0, crashes=0, lease_until=datetime.now(UTC)
    )
