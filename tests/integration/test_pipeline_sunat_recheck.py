"""Integration tests for the SUNAT recheck step and its admin alert (BR-V01, BR-V02, BR-V04, D125)."""

from __future__ import annotations

import itertools
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.app import _validation_handlers
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, ValidezApi
from invoicebot.extract.fields import Field, Reading
from invoicebot.pipeline.steps import SUNAT_RECHECK_STEP
from invoicebot.pipeline.sunat_recheck import _read, _record, make_sunat_recheck_handler
from invoicebot.queue.jobs import Job
from invoicebot.queue.retry import backoff_minutes
from invoicebot.secrets import Secrets, SecretStore
from invoicebot.store.invoices import (
    create_invoice,
    save_reading,
    set_flow,
    set_state,
    set_sunat_status,
)
from invoicebot.sunat.token import ADAPTER, SECRETS
from invoicebot.sunat.validez import CpStatus
from invoicebot.sunat.verdict import decide

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
# Both values come from tests/synthetic_rucs.txt; no real RUC appears in this file.
OWNER_RUC = "20000000001"
ISSUER_RUC = "51900000000"
WORK_FLOW = "work_microsoft"
FULL_PROFILE = "yo"
FACTURA = "01"
TOTAL = "49.90"
CLIENT_ID = "synthetic-client-id"
CLIENT_CREDENTIAL = "synthetic-client-secret"
ACCESS_VALUE = "synthetic-access-token"
ENDPOINT_TEMPLATE = "https://api-seguridad.example.test/v1/clientesextranet/{client_id}/oauth2/token/"
API_URL = "https://api.example.test/v1/contribuyente/contribuyentes/{ruc}/validarcomprobante"
#: Each invoice gets its own numero, so one test's reading never hides another's.
_NEXT_NUMERO = itertools.count(50000)


def _config(profile_id: str) -> Config:
    """Return the example config for ``profile_id`` with the synthetic owner RUC and a test API."""
    cfg = load_config(EXAMPLE)
    return cfg.model_copy(
        update={
            "profiles": {profile_id: cfg.profiles[FULL_PROFILE]},
            "owner": cfg.owner.model_copy(update={"ruc": OWNER_RUC}),
            "sunat": cfg.sunat.model_copy(
                update={
                    "validez_api": ValidezApi(
                        enabled=True, token_url=ENDPOINT_TEMPLATE, api_url=API_URL, timeout_s=5
                    )
                }
            ),
        }
    )


def _secrets() -> Secrets:
    """The adapter's scoped view over synthetic client credentials."""
    return SecretStore({"sunat_client_id": CLIENT_ID, "sunat_client_secret": CLIENT_CREDENTIAL}).scoped(
        ADAPTER, set(SECRETS)
    )


def _field(value: str) -> Field:
    """Return a QR-read field at full confidence."""
    return Field(value=value, source="qr", confidence=1.0)


def _reading(fecha: date) -> Reading:
    """Build a complete reading from the synthetic issuer RUC with its own numero."""
    return Reading(
        fields={
            "ruc_emisor": _field(ISSUER_RUC),
            "tipo": _field(FACTURA),
            "serie": _field("F001"),
            "numero": _field(str(next(_NEXT_NUMERO))),
            "igv": _field("7.61"),
            "total": _field(TOTAL),
            "fecha_emision": _field(fecha.isoformat()),
        },
        document_url=None,
    )


def _incomplete_reading(fecha: date) -> Reading:
    """Build a reading without its total, which SUNAT cannot validate (R-4.1)."""
    return Reading(
        fields={
            "ruc_emisor": _field(ISSUER_RUC),
            "tipo": _field(FACTURA),
            "serie": _field("F001"),
            "numero": _field(str(next(_NEXT_NUMERO))),
            "fecha_emision": _field(fecha.isoformat()),
        },
        document_url=None,
    )


async def _today(pool: AsyncConnectionPool[AsyncConnection[Any]]) -> date:
    """Return today in Lima from the database clock, as the handler sees it."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT (now() AT TIME ZONE 'America/Lima')::date")
        row = await cur.fetchone()
    assert row is not None
    return cast(date, row[0])


async def _fetching_invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    reading: Reading,
    rechecks: int,
) -> int:
    """Save ``reading`` on an invoice in FETCHING_DOCS carrying ``rechecks`` answers."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await save_reading(conn, invoice_id, reading)
        await set_flow(conn, invoice_id, WORK_FLOW)
        await set_state(conn, invoice_id, "FETCHING_DOCS")
        await set_sunat_status(
            conn, invoice_id, CpStatus(estado_cp=0, estado_ruc="00", cond_domi_ruc="00"), rechecks=rechecks
        )
    return invoice_id


def _job(job_id: int, invoice_id: int) -> Job:
    """Build the leased job row the recheck handler receives."""
    return Job(
        id=job_id,
        invoice_id=invoice_id,
        step=SUNAT_RECHECK_STEP,
        attempts=0,
        crashes=0,
        lease_until=datetime.now(UTC),
    )


def _sunat_handler(
    estado_cp: object = 1,
    *,
    estado_ruc: object = "00",
    cond_domi_ruc: object = "00",
    validez_status: int = 200,
) -> httpx.MockTransport:
    """Answer the token endpoint with a token and the validez endpoint with ``estado_cp``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2/token" in str(request.url):
            return httpx.Response(200, json={"access_token": ACCESS_VALUE, "expires_in": 3600})
        return httpx.Response(
            validez_status,
            json={
                "success": True,
                "data": {
                    "estadoCp": estado_cp,
                    "estadoRuc": estado_ruc,
                    "condDomiRuc": cond_domi_ruc,
                },
            },
        )

    return httpx.MockTransport(handler)


def _unreachable(request: httpx.Request) -> httpx.Response:
    """Fail a test whose handler makes an unexpected HTTP request."""
    pytest.fail(f"unexpected HTTP request: {request.method} {request.url}")


async def _run(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    cfg: Config,
    invoice_id: int,
    *,
    transport: httpx.MockTransport,
    job_id: int = 1,
) -> None:
    """Run the recheck handler on ``invoice_id`` with the given transport."""
    async with httpx.AsyncClient(transport=transport) as http:
        handler = make_sunat_recheck_handler(pool, http, _secrets(), cfg)
        await handler(_job(job_id, invoice_id))


async def _invoice(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str, str | None]:
    """Return ``(state, rule_id)`` of an invoice."""
    cur = await conn.execute("SELECT state, rule_id FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return row[0], row[1]


async def _sunat_json(conn: AsyncConnection[Any], invoice_id: int) -> Any:
    """Return the stored ``sunat_status_json`` of an invoice."""
    cur = await conn.execute("SELECT sunat_status_json FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _jobs(conn: AsyncConnection[Any], invoice_id: int) -> list[str]:
    """Return the steps of the invoice's queued jobs, in insertion order."""
    cur = await conn.execute("SELECT step FROM jobs WHERE invoice_id = %s ORDER BY id", (invoice_id,))
    return [str(step) for (step,) in await cur.fetchall()]


async def _run_after_minutes(conn: AsyncConnection[Any], invoice_id: int) -> float | None:
    """Return how many minutes from now the invoice's recheck job runs, or None when absent."""
    cur = await conn.execute(
        """
        SELECT EXTRACT(EPOCH FROM (run_after - now())) / 60
        FROM jobs
        WHERE invoice_id = %s AND step = %s
        """,
        (invoice_id, SUNAT_RECHECK_STEP),
    )
    row = await cur.fetchone()
    return None if row is None else float(row[0])


async def _outbox(conn: AsyncConnection[Any], profile_id: str) -> list[tuple[str, str | None, Any]]:
    """Return the outbox's ``(text, rule_id, buttons)`` rows for ``profile_id``, oldest first."""
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


async def test_missing_then_authorized_stores_one_and_schedules_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-V01: a recheck answering estadoCp 1 stores rechecks 1 and schedules nothing more."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id)
    reading = _reading(await _today(db_pool))
    invoice_id = await _fetching_invoice(db_pool, profile_id, reading, rechecks=0)

    await _run(db_pool, cfg, invoice_id, transport=_sunat_handler(1))

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        status = await _sunat_json(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert row == ("FETCHING_DOCS", None)
    assert status["estado_cp"] == 1
    assert status["rechecks"] == 1
    assert jobs == []
    assert outbox == []


async def test_three_missing_answers_stop_after_the_third(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-V01: three estadoCp 0 answers stop rechecking after the third."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id)
    invoice_id = await _fetching_invoice(db_pool, profile_id, _reading(await _today(db_pool)), rechecks=0)

    for run in range(3):
        await _run(db_pool, cfg, invoice_id, transport=_sunat_handler(0), job_id=run + 1)

    async with db_pool.connection() as conn:
        status = await _sunat_json(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert status["estado_cp"] == 0
    assert status["rechecks"] == 3
    # The first two runs schedule the next one; the third is at the cap and stops.
    assert jobs == ["sunat_recheck", "sunat_recheck"]
    assert outbox == []


async def test_annulled_answer_alerts_the_admin_and_leaves_the_state(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-V02: a recheck answering estadoCp 2 alerts the admin and changes no state."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id)
    reading = _reading(await _today(db_pool))
    invoice_id = await _fetching_invoice(db_pool, profile_id, reading, rechecks=1)

    await _run(db_pool, cfg, invoice_id, transport=_sunat_handler(2))

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        status = await _sunat_json(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert row == ("FETCHING_DOCS", None)
    assert status["estado_cp"] == 2
    assert status["rechecks"] == 2
    assert jobs == []
    assert outbox == [
        (
            f"⚠️ SUNAT ahora dice que el comprobante #{invoice_id} "
            f"(F001-{reading.value('numero')}) está ANULADO.",
            "BR-V02_admin",
            [],
        )
    ]


async def test_outage_reschedules_on_the_backoff(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-V04: an outage stores no answer, increments rechecks and reschedules for the next attempt."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id)
    invoice_id = await _fetching_invoice(db_pool, profile_id, _reading(await _today(db_pool)), rechecks=1)

    await _run(db_pool, cfg, invoice_id, transport=_sunat_handler(1, validez_status=500))

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        status = await _sunat_json(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        minutes = await _run_after_minutes(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert row == ("FETCHING_DOCS", None)
    assert status["estado_cp"] is None
    assert status["rechecks"] == 2
    assert jobs == ["sunat_recheck"]
    # It stored recheck 2, so the one it schedules is recheck 3.
    assert minutes == pytest.approx(backoff_minutes(3), abs=0.1)
    assert outbox == []


async def test_each_backoff_recheck_is_scheduled_further_out(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-V04: the second BR-V04 recheck waits longer than the first."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id)
    first = await _fetching_invoice(db_pool, profile_id, _reading(await _today(db_pool)), rechecks=0)
    second = await _fetching_invoice(db_pool, profile_id, _reading(await _today(db_pool)), rechecks=1)

    await _run(db_pool, cfg, first, transport=_sunat_handler(1, validez_status=500), job_id=1)
    await _run(db_pool, cfg, second, transport=_sunat_handler(1, validez_status=500), job_id=2)

    async with db_pool.connection() as conn:
        first_minutes = await _run_after_minutes(conn, first)
        second_minutes = await _run_after_minutes(conn, second)
    assert first_minutes == pytest.approx(backoff_minutes(2), abs=0.1)
    assert second_minutes == pytest.approx(backoff_minutes(3), abs=0.1)
    assert second_minutes > first_minutes


async def test_cancelled_invoice_is_skipped(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.1: an invoice cancelled before its recheck runs is left untouched."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id)
    invoice_id = await _fetching_invoice(db_pool, profile_id, _reading(await _today(db_pool)), rechecks=1)
    async with db_pool.connection() as conn, conn.transaction():
        await set_state(conn, invoice_id, "CANCELLED", rule_id="BR-D06")

    await _run(db_pool, cfg, invoice_id, transport=httpx.MockTransport(_unreachable))

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        status = await _sunat_json(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert row == ("CANCELLED", "BR-D06")
    assert status["rechecks"] == 1
    assert jobs == []
    assert outbox == []


async def test_validation_off_skips_a_queued_recheck(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D125: with validation off the app's recheck handler returns and leaves the invoice untouched."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id)
    invoice_id = await _fetching_invoice(db_pool, profile_id, _reading(await _today(db_pool)), rechecks=0)
    handler = _validation_handlers(cfg, db_pool, cast(Any, None), None)[SUNAT_RECHECK_STEP]

    await handler(_job(1, invoice_id))

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        status = await _sunat_json(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert row == ("FETCHING_DOCS", None)
    assert status["rechecks"] == 0
    assert jobs == []
    assert outbox == []


async def test_cancelled_between_read_and_record_writes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D125: a cancellation after the read leaves the stored answer and the admins alone."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id)
    invoice_id = await _fetching_invoice(db_pool, profile_id, _reading(await _today(db_pool)), rechecks=1)
    recheck = await _read(db_pool, invoice_id)
    assert recheck is not None
    async with db_pool.connection() as conn, conn.transaction():
        await set_state(conn, invoice_id, "CANCELLED", rule_id="BR-D06")

    status = CpStatus(estado_cp=2, estado_ruc="00", cond_domi_ruc="00")
    async with db_pool.connection() as conn, conn.transaction():
        await _record(conn, recheck, cfg, status, decide(status, None))

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        stored = await _sunat_json(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert row == ("CANCELLED", "BR-D06")
    assert stored["estado_cp"] == 0
    assert stored["rechecks"] == 1
    assert jobs == []
    assert outbox == []


async def test_a_recheck_superseded_by_a_newer_count_writes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D125: a count another check changed after the read leaves the invoice and the queue alone."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id)
    invoice_id = await _fetching_invoice(db_pool, profile_id, _reading(await _today(db_pool)), rechecks=0)
    recheck = await _read(db_pool, invoice_id)
    assert recheck is not None
    async with db_pool.connection() as conn, conn.transaction():
        await set_sunat_status(
            conn,
            invoice_id,
            CpStatus(estado_cp=3, estado_ruc="00", cond_domi_ruc="00"),
            rechecks=1,
        )

    async with db_pool.connection() as conn, conn.transaction():
        await _record(conn, recheck, cfg, None, decide(None, None))

    async with db_pool.connection() as conn:
        stored = await _sunat_json(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert stored["estado_cp"] == 3
    assert stored["rechecks"] == 1
    assert jobs == []
    assert outbox == []


async def test_incomplete_reading_ends_the_recheck_quietly(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D125: a recheck of a reading SUNAT cannot validate logs a warning and leaves the invoice alone."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id)
    reading = _incomplete_reading(await _today(db_pool))
    invoice_id = await _fetching_invoice(db_pool, profile_id, reading, rechecks=1)

    with caplog.at_level(logging.WARNING):
        await _run(db_pool, cfg, invoice_id, transport=_sunat_handler(1))

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        stored = await _sunat_json(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert row == ("FETCHING_DOCS", None)
    assert stored["rechecks"] == 1
    assert jobs == []
    assert outbox == []
    assert "sunat recheck skipped: incomplete reading" in caplog.text
