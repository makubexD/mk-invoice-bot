"""Integration tests for the SUNAT validate step and the summary inputs it saves (R-4.1, R-4.2, D206)."""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.buttons import button_id
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, ValidezApi
from invoicebot.extract.fields import Field, Reading
from invoicebot.pipeline.questions import INVALID_OK_ACTION
from invoicebot.pipeline.summary_post import SUMMARY_KEY, save_summary_inputs
from invoicebot.pipeline.validate import VALIDATE_STEP, make_validate_handler
from invoicebot.queue.jobs import Job
from invoicebot.rules.messages_es import MESSAGES
from invoicebot.secrets import Secrets, SecretStore
from invoicebot.store.fetch_attempts import list_attempts, record_attempt
from invoicebot.store.flow_b import load_flow_data
from invoicebot.store.invoices import create_invoice, save_reading, set_flow, set_state
from invoicebot.sunat.token import ADAPTER, SECRETS

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
#: The owner timezone, from the example config (S11.4).
TIMEZONE = "America/Lima"
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
_NEXT_NUMERO = itertools.count(40000)


def _config(profile_id: str, template: str) -> Config:
    """Return the example config for ``profile_id`` with the synthetic owner RUC and a test API."""
    cfg = load_config(EXAMPLE)
    return cfg.model_copy(
        update={
            "profiles": {profile_id: cfg.profiles[template]},
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


async def _today(pool: AsyncConnectionPool[AsyncConnection[Any]]) -> date:
    """Return today in Lima from the database clock, as the handler sees it."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT (now() AT TIME ZONE 'America/Lima')::date")
        row = await cur.fetchone()
    assert row is not None
    return cast(date, row[0])


async def _validating_invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]], profile_id: str, reading: Reading
) -> int:
    """Save ``reading`` on a fresh invoice, route it and leave it in VALIDATING."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await save_reading(conn, invoice_id, reading)
        await set_flow(conn, invoice_id, WORK_FLOW)
        await set_state(conn, invoice_id, "VALIDATING", rule_id="BR-R02")
    return invoice_id


def _job(job_id: int, invoice_id: int) -> Job:
    """Build the leased job row the validate handler receives."""
    return Job(
        id=job_id,
        invoice_id=invoice_id,
        step=VALIDATE_STEP,
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
    secrets: Secrets | None,
    transport: httpx.MockTransport,
    job: Job | None = None,
) -> None:
    """Run the validate handler on ``invoice_id`` with the given secrets and transport."""
    async with httpx.AsyncClient(transport=transport) as http:
        await make_validate_handler(pool, http, secrets, cfg)(job or _job(1, invoice_id))


async def _invoice(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str, str | None, str | None]:
    """Return ``(state, rule_id, flow_id)`` of an invoice."""
    cur = await conn.execute("SELECT state, rule_id, flow_id FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return row[0], row[1], row[2]


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


async def _run_after_minutes(conn: AsyncConnection[Any], invoice_id: int, step: str) -> float | None:
    """Return how many minutes from now the invoice's ``step`` job runs, or None when absent."""
    cur = await conn.execute(
        """
        SELECT EXTRACT(EPOCH FROM (run_after - now())) / 60
        FROM jobs
        WHERE invoice_id = %s AND step = %s
        """,
        (invoice_id, step),
    )
    row = await cur.fetchone()
    return None if row is None else float(row[0])


async def _saved(conn: AsyncConnection[Any], invoice_id: int) -> dict[str, Any]:
    """Return the summary inputs saved in the invoice's flow data (D206)."""
    saved = (await load_flow_data(conn, invoice_id))[SUMMARY_KEY]
    assert isinstance(saved, dict)
    return saved


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


async def test_validation_off_saves_summary_inputs_and_moves_on(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.1, D124, D206: with no secrets validate saves the summary inputs and moves to FETCHING_DOCS."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _validating_invoice(db_pool, profile_id, _reading(await _today(db_pool)))

    await _run(db_pool, cfg, invoice_id, secrets=None, transport=httpx.MockTransport(_unreachable))

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
        saved = await _saved(conn, invoice_id)
    assert row == ("FETCHING_DOCS", "BR-R02", WORK_FLOW)
    assert jobs == ["fetch", "fetch_progress"]
    assert outbox == []
    assert saved == {"rule_id": "BR-R02", "extra_lines": []}


async def test_validation_after_an_edit_starts_a_new_fetch_round(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D166, D97: re-validating an edited invoice starts a new fetch round; old attempts no longer count."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _validating_invoice(db_pool, profile_id, _reading(await _today(db_pool)))
    async with db_pool.connection() as conn, conn.transaction():
        await record_attempt(conn, invoice_id, "qr_url", None, "not_found_yet", 1)

    await _run(db_pool, cfg, invoice_id, secrets=None, transport=httpx.MockTransport(_unreachable))

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        attempts = await list_attempts(conn, invoice_id)
    assert row == ("FETCHING_DOCS", "BR-R02", WORK_FLOW)
    assert attempts == []


async def test_estado_cp_1_stores_the_status_without_a_sunat_line(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.2: estadoCp 1 moves the invoice on with the status stored and no SUNAT line."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _validating_invoice(db_pool, profile_id, _reading(await _today(db_pool)))

    await _run(db_pool, cfg, invoice_id, secrets=_secrets(), transport=_sunat_handler(1))

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        status = await _sunat_json(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
        saved = await _saved(conn, invoice_id)
    assert row == ("FETCHING_DOCS", "BR-R02", WORK_FLOW)
    assert status["estado_cp"] == 1
    assert status["rechecks"] == 0
    assert jobs == ["fetch", "fetch_progress"]
    assert outbox == []
    assert saved["extra_lines"] == []


async def test_estado_cp_0_adds_br_v01_and_schedules_a_recheck(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.2, BR-V01: estadoCp 0 adds the BR-V01 line and a recheck job about 24 hours ahead."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _validating_invoice(db_pool, profile_id, _reading(await _today(db_pool)))

    await _run(db_pool, cfg, invoice_id, secrets=_secrets(), transport=_sunat_handler(0))

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        minutes = await _run_after_minutes(conn, invoice_id, "sunat_recheck")
        outbox = await _outbox(conn, profile_id)
        saved = await _saved(conn, invoice_id)
    assert row == ("FETCHING_DOCS", "BR-R02", WORK_FLOW)
    assert jobs == ["fetch", "fetch_progress", "sunat_recheck"]
    assert minutes is not None
    assert 1439 <= minutes <= 1441
    assert outbox == []
    assert "SUNAT aún no lo registra" in saved["extra_lines"][0]


async def test_estado_cp_3_adds_br_v05(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.2, BR-V05: estadoCp 3 adds the BR-V05 line and schedules no recheck."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _validating_invoice(db_pool, profile_id, _reading(await _today(db_pool)))

    await _run(db_pool, cfg, invoice_id, secrets=_secrets(), transport=_sunat_handler(3))

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
        saved = await _saved(conn, invoice_id)
    assert row == ("FETCHING_DOCS", "BR-R02", WORK_FLOW)
    assert jobs == ["fetch", "fetch_progress"]
    assert outbox == []
    assert "SUNAT lo registra como AUTORIZADO" in saved["extra_lines"][0]


async def test_estado_cp_2_asks_confirm_invalid(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.2, BR-V02: estadoCp 2 waits in CONFIRM_INVALID with BR-V02 and the [Sí] button."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _validating_invoice(db_pool, profile_id, _reading(await _today(db_pool)))

    await _run(db_pool, cfg, invoice_id, secrets=_secrets(), transport=_sunat_handler(2))

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert row == ("CONFIRM_INVALID", "BR-V02", WORK_FLOW)
    assert jobs == []
    assert len(outbox) == 1
    text, rule_id, buttons = outbox[0]
    assert rule_id == "BR-V02"
    assert text == "⚠️ SUNAT dice que este comprobante está ANULADO. ¿Lo registro igual?"
    assert buttons == [
        {"id": button_id(invoice_id, INVALID_OK_ACTION), "title": "Sí"},
        {"id": button_id(invoice_id, "cancel"), "title": "Cancelar"},
    ]


async def test_estado_cp_4_asks_confirm_invalid(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.2, BR-V06: estadoCp 4 waits in CONFIRM_INVALID with BR-V06 and the [Sí] button."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _validating_invoice(db_pool, profile_id, _reading(await _today(db_pool)))

    await _run(db_pool, cfg, invoice_id, secrets=_secrets(), transport=_sunat_handler(4))

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert row == ("CONFIRM_INVALID", "BR-V06", WORK_FLOW)
    assert jobs == []
    assert len(outbox) == 1
    text, rule_id, buttons = outbox[0]
    assert rule_id == "BR-V06"
    assert text == "⚠️ SUNAT dice que este comprobante NO está AUTORIZADO. ¿Lo registro igual?"
    assert buttons == [
        {"id": button_id(invoice_id, INVALID_OK_ACTION), "title": "Sí"},
        {"id": button_id(invoice_id, "cancel"), "title": "Cancelar"},
    ]


async def test_500_answer_adds_br_v04_and_schedules_a_recheck(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-V04: a 500 from the validez API adds the BR-V04 line and a recheck on the backoff."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _validating_invoice(db_pool, profile_id, _reading(await _today(db_pool)))

    await _run(db_pool, cfg, invoice_id, secrets=_secrets(), transport=_sunat_handler(1, validez_status=500))

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        status = await _sunat_json(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        minutes = await _run_after_minutes(conn, invoice_id, "sunat_recheck")
        outbox = await _outbox(conn, profile_id)
        saved = await _saved(conn, invoice_id)
    assert row == ("FETCHING_DOCS", "BR-R02", WORK_FLOW)
    assert status["estado_cp"] is None
    assert jobs == ["fetch", "fetch_progress", "sunat_recheck"]
    assert minutes is not None
    assert 1 <= minutes <= 3
    assert outbox == []
    assert "No pude validar con SUNAT ahora" in saved["extra_lines"][0]


async def test_inactive_issuer_adds_br_v03_line(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-V03: an issuer not ACTIVO/HABIDO adds its (estado, condición) line to the summary."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _validating_invoice(db_pool, profile_id, _reading(await _today(db_pool)))

    await _run(db_pool, cfg, invoice_id, secrets=_secrets(), transport=_sunat_handler(1, estado_ruc="10"))

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
        saved = await _saved(conn, invoice_id)
    assert row == ("FETCHING_DOCS", "BR-R02", WORK_FLOW)
    assert jobs == ["fetch", "fetch_progress"]
    assert outbox == []
    assert "El emisor figura BAJA DEFINITIVA/HABIDO en SUNAT." in saved["extra_lines"][0]


async def test_cancelled_invoice_is_left_alone(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.1: an invoice cancelled before the validate job runs is left untouched."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _validating_invoice(db_pool, profile_id, _reading(await _today(db_pool)))
    async with db_pool.connection() as conn, conn.transaction():
        await set_state(conn, invoice_id, "CANCELLED", rule_id="BR-D06")

    await _run(db_pool, cfg, invoice_id, secrets=None, transport=httpx.MockTransport(_unreachable))

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert row == ("CANCELLED", "BR-D06", WORK_FLOW)
    assert jobs == []
    assert outbox == []


async def test_cancel_during_sunat_call_ends_quietly(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-4.1: a cancel during the SUNAT call rolls the answer back and ends the job quietly."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _validating_invoice(db_pool, profile_id, _reading(await _today(db_pool)))

    async def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2/token" in str(request.url):
            return httpx.Response(200, json={"access_token": ACCESS_VALUE, "expires_in": 3600})
        # A /cancelar committed while SUNAT is being asked wins: move the invoice before
        # answering, the way other tests cancel with a direct UPDATE through the pool.
        async with db_pool.connection() as conn, conn.transaction():
            await conn.execute("UPDATE invoices SET state = 'CANCELLED' WHERE id = %s", (invoice_id,))
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"estadoCp": 1, "estadoRuc": "00", "condDomiRuc": "00"},
            },
        )

    await _run(db_pool, cfg, invoice_id, secrets=_secrets(), transport=httpx.MockTransport(handler))

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        status = await _sunat_json(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert row == ("CANCELLED", "BR-R02", WORK_FLOW)
    assert status is None
    assert jobs == []
    assert outbox == []


async def _run_after_seconds(conn: AsyncConnection[Any], invoice_id: int, step: str) -> float:
    """Return how many seconds ``step``'s run_after is after the ``fetch`` job's."""
    cur = await conn.execute(
        """
        SELECT EXTRACT(EPOCH FROM (p.run_after - f.run_after))
        FROM jobs p JOIN jobs f ON f.invoice_id = p.invoice_id AND f.step = 'fetch'
        WHERE p.invoice_id = %s AND p.step = %s
        """,
        (invoice_id, step),
    )
    row = await cur.fetchone()
    assert row is not None
    return float(row[0])


async def test_routed_invoice_queues_fetch_progress_and_no_summary(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D206: a valid routed invoice gets fetch and fetch_progress jobs 20 s apart, no message."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _validating_invoice(db_pool, profile_id, _reading(await _today(db_pool)))

    await _run(db_pool, cfg, invoice_id, secrets=_secrets(), transport=_sunat_handler(1))

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        gap = await _run_after_seconds(conn, invoice_id, "fetch_progress")
        outbox = await _outbox(conn, profile_id)
        saved = await _saved(conn, invoice_id)
    assert row == ("FETCHING_DOCS", "BR-R02", WORK_FLOW)
    assert jobs == ["fetch", "fetch_progress"]
    assert 19 <= gap <= 21
    assert outbox == []
    assert saved["rule_id"] == "BR-R02"


async def test_br_v04_line_is_saved_in_the_summary_inputs(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D206: the BR-V04 line is saved in the summary inputs, not sent."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _validating_invoice(db_pool, profile_id, _reading(await _today(db_pool)))

    await _run(db_pool, cfg, invoice_id, secrets=_secrets(), transport=_sunat_handler(1, validez_status=500))

    async with db_pool.connection() as conn:
        saved = await _saved(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert saved["extra_lines"] == [MESSAGES["BR-V04"]]
    assert outbox == []


async def test_br_v02_ask_is_queued_at_once(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D206: the BR-V02 question is still queued at once, with no fetch or progress job."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _validating_invoice(db_pool, profile_id, _reading(await _today(db_pool)))

    await _run(db_pool, cfg, invoice_id, secrets=_secrets(), transport=_sunat_handler(2))

    async with db_pool.connection() as conn:
        jobs = await _jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert jobs == []
    assert [rule for _, rule, _ in outbox] == ["BR-V02"]


async def test_br_v02_ask_discards_inputs_an_earlier_validation_saved(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D206: a re-validation that asks drops the earlier saved inputs, so no stale SUNAT line is posted."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _validating_invoice(db_pool, profile_id, _reading(await _today(db_pool)))
    async with db_pool.connection() as conn, conn.transaction():
        await save_summary_inputs(conn, invoice_id, rule_id="BR-R02", extra_lines=["stale"])

    await _run(db_pool, cfg, invoice_id, secrets=_secrets(), transport=_sunat_handler(2))

    async with db_pool.connection() as conn:
        assert (await load_flow_data(conn, invoice_id))[SUMMARY_KEY] is None
