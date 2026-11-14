"""Integration tests for the route handler and its summary (S4.2)."""

from __future__ import annotations

import asyncio
import itertools
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import Attachment, InboundMessage
from invoicebot.channels.whatsapp.payloads import button_id
from invoicebot.config.loader import load_config
from invoicebot.config.models import Brand, Config, Store, StoreDirectory
from invoicebot.extract.fields import Field, Flag, Reading
from invoicebot.extract.qr_payload import INFERRED_CONFIDENCE
from invoicebot.pipeline.extract import ROUTE_STEP
from invoicebot.pipeline.intake import handle_inbound
from invoicebot.pipeline.route import make_route_handler
from invoicebot.queue.jobs import Job, enqueue
from invoicebot.routing.router import CompiledRule
from invoicebot.store.flow_b import load_flow_data, merge_flow_data
from invoicebot.store.invoices import create_invoice, save_reading, set_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
# Both values come from tests/synthetic_rucs.txt; the unknown buyer fails its check digit.
OWNER_RUC = "20000000001"
WORK_RUC = "51900000000"
UNKNOWN_BUYER_RUC = "20000000002"
DNI_BUYER = "40382716"
OWNER_FLOW = "personal_google"
WORK_FLOW = "work_microsoft"
FULL_PROFILE = "yo"
LIMITED_PROFILE = "asistente"
RULES = (
    CompiledRule(id="BR-R01", flow=OWNER_FLOW, when={"num_doc_adq": frozenset({OWNER_RUC})}),
    CompiledRule(id="BR-R02", flow=WORK_FLOW, when={"num_doc_adq": frozenset({WORK_RUC})}),
)
FACTURA = "01"
BOLETA = "03"
OLD_INVOICE_DAYS = 400
#: Each invoice gets its own numero, so one test's natural key never hides another's (D93).
_NEXT_NUMERO = itertools.count(20000)


def _config(profile_id: str, template: str) -> Config:
    """Return the example config with one profile, copied from ``template``, under ``profile_id``."""
    cfg = load_config(EXAMPLE)
    return cfg.model_copy(update={"profiles": {profile_id: cfg.profiles[template]}})


def _field(
    value: str,
    confidence: float = 1.0,
    flag: Flag | None = None,
    source: str = "qr",
) -> Field:
    return Field(value=value, source=source, confidence=confidence, flag=flag)


def _reading(
    fecha: date,
    *,
    buyer: str = WORK_RUC,
    buyer_type: str = "6",
    tipo: str = FACTURA,
    tipo_flag: Flag | None = None,
    total: str = "49.90",
) -> Reading:
    """Build a reading of an invoice to ``buyer`` with its own numero."""
    return Reading(
        fields={
            "ruc_emisor": _field(OWNER_RUC),
            "tipo": _field(tipo, flag=tipo_flag),
            "serie": _field("F001"),
            "numero": _field(str(next(_NEXT_NUMERO))),
            "igv": _field("7.61"),
            "total": _field(total),
            "fecha_emision": _field(fecha.isoformat()),
            "tipo_doc_adq": _field(buyer_type),
            "num_doc_adq": _field(buyer),
        },
        document_url=None,
    )


def _ocr_buyer_reading(fecha: date, *, buyer: str = WORK_RUC, buyer_type: str = "6") -> Reading:
    """Build a reading whose buyer was read by OCR at confidence 0.5 (BR-R04)."""
    reading = _reading(fecha, buyer=buyer, buyer_type=buyer_type)
    fields = dict(reading.fields)
    fields["num_doc_adq"] = _field(buyer, source="ocr", confidence=0.5)
    return Reading(fields=fields, document_url=None)


async def _today(pool: AsyncConnectionPool[AsyncConnection[Any]]) -> date:
    """Return today in Lima from the database clock, as the handler sees it."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT (now() AT TIME ZONE 'America/Lima')::date")
        row = await cur.fetchone()
    assert row is not None
    return cast(date, row[0])


async def _routing_invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    reading: Reading,
    invoice_id: int | None = None,
    answers: dict[str, Any] | None = None,
) -> int:
    """Save ``reading`` (and stored answers) on an invoice and move it to ROUTING."""
    async with pool.connection() as conn, conn.transaction():
        if invoice_id is None:
            invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await save_reading(conn, invoice_id, reading)
        if answers:
            await merge_flow_data(conn, invoice_id, {"answers": answers})
        await set_state(conn, invoice_id, "ROUTING")
    return invoice_id


def _job(job_id: int, invoice_id: int) -> Job:
    """Build the leased job row the route handler receives."""
    return Job(
        id=job_id,
        invoice_id=invoice_id,
        step=ROUTE_STEP,
        attempts=0,
        crashes=0,
        lease_until=datetime.now(UTC),
    )


async def _run(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    cfg: Config,
    invoice_id: int,
    directory: StoreDirectory | None = None,
    job: Job | None = None,
) -> None:
    await make_route_handler(pool, cfg, directory or StoreDirectory(stores={}, portals={}), RULES)(
        job or _job(1, invoice_id)
    )


def _store(ruc: str, *comercios: str) -> Store:
    """Build a synthetic store with one brand per ``comercio``."""
    return Store(
        ruc=ruc,
        razon_social="Comercio Sintético",
        tipo="restaurante",
        brands=[Brand(comercio=comercio) for comercio in comercios],
        portal=None,
    )


def _directory(store: Store) -> StoreDirectory:
    """Build a store directory holding only ``store``."""
    return StoreDirectory(stores={store.ruc: store}, portals={})


async def _invoice(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str, str | None, str | None]:
    """Return ``(state, rule_id, flow_id)`` of an invoice."""
    cur = await conn.execute("SELECT state, rule_id, flow_id FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return row[0], row[1], row[2]


async def _stored_key(conn: AsyncConnection[Any], invoice_id: int) -> str | None:
    """Return the natural key stored on invoice_id, or None when it holds none (D93)."""
    cur = await conn.execute("SELECT natural_key FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _jobs(conn: AsyncConnection[Any], invoice_id: int) -> list[str]:
    """Return the steps of the invoice's queued jobs, in insertion order."""
    cur = await conn.execute("SELECT step FROM jobs WHERE invoice_id = %s ORDER BY id", (invoice_id,))
    return [str(step) for (step,) in await cur.fetchall()]


async def _texts(conn: AsyncConnection[Any], profile_id: str, rule_id: str | None = None) -> list[str]:
    """Return the outbox texts for ``profile_id``, optionally only under ``rule_id``."""
    cur = await conn.execute(
        """
        SELECT payload_json->>'text'
        FROM outbox
        WHERE "to" = %s AND (%s::text IS NULL OR payload_json->>'rule_id' = %s)
        ORDER BY id
        """,
        (profile_id, rule_id, rule_id),
    )
    return [str(text) for (text,) in await cur.fetchall()]


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


async def _route_one(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    reading: Reading,
    template: str = FULL_PROFILE,
) -> tuple[tuple[str, str | None, str | None], list[str]]:
    """Route one invoice of a fresh profile; return its row and its outbox texts."""
    profile_id = f"{template}-{uuid4().hex}"
    cfg = _config(profile_id, template)
    invoice_id = await _routing_invoice(pool, profile_id, reading)
    await _run(pool, cfg, invoice_id)
    async with pool.connection() as conn:
        return await _invoice(conn, invoice_id), await _texts(conn, profile_id)


async def test_work_factura_is_routed_with_one_summary(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.1, R-3.4: a factura to the work RUC is routed, waits in FETCHING_DOCS, and gets one summary."""
    today = await _today(db_pool)
    reading = _reading(today)

    row, texts = await _route_one(db_pool, reading)

    assert row == ("FETCHING_DOCS", "BR-R02", WORK_FLOW)
    assert len(texts) == 1
    assert "Trabajo (Microsoft)" in texts[0]
    assert texts[0].startswith(f"🧾 Factura F001-{reading.value('numero')} · {today:%d/%m/%Y}")
    assert "Revisa" not in texts[0]
    assert "Deducido" not in texts[0]


async def test_happy_path_sends_ack_and_summary(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """R-3.4, BR-S09: a photo routed end to end leaves exactly the ack and the summary in the outbox."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    msg = InboundMessage(
        channel="whatsapp",
        external_id=uuid4().hex,
        profile_id=profile_id,
        sender_role="owner",
        attachments=[Attachment(kind="photo", media_ref="media-1", mime="image/jpeg", size=1024)],
        received_at=datetime.now(UTC),
    )
    async with db_pool.connection() as conn:
        intake = await handle_inbound(conn, msg, max_queued_per_sender=5)
    assert intake.invoice_id is not None
    await _routing_invoice(db_pool, profile_id, _reading(await _today(db_pool)), intake.invoice_id)

    await _run(db_pool, cfg, intake.invoice_id)

    async with db_pool.connection() as conn:
        texts = await _texts(conn, profile_id)
        summaries = await _texts(conn, profile_id, "BR-R02")
    assert len(texts) == 2
    assert len(summaries) == 1


async def test_boleta_to_work_ruc_is_cancelled(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """BR-R08: a boleta routed to the facturas-only work flow is cancelled with BR-R08's message."""
    row, texts = await _route_one(db_pool, _reading(await _today(db_pool), tipo=BOLETA))

    assert row == ("CANCELLED", "BR-R08", None)
    assert texts == [
        "Este comprobante es una boleta, y Trabajo (Microsoft) solo acepta facturas. No lo proceso."
    ]


async def test_unknown_buyer_is_asked_which_flow(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-R03: an unknown RUC buyer now gets the flow question instead of the BR-R07 shortcut."""
    row, texts = await _route_one(db_pool, _reading(await _today(db_pool), buyer=UNKNOWN_BUYER_RUC))

    assert row == ("CONFIRMING", "BR-R03", None)
    assert texts == [
        f"Este comprobante está a nombre del RUC {UNKNOWN_BUYER_RUC}. ¿En qué flujo lo registro?"
    ]


async def test_profile_without_the_flow_is_cancelled(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-R05: a profile that may not use the work flow gets BR-R05 naming that flow."""
    row, texts = await _route_one(db_pool, _reading(await _today(db_pool)), LIMITED_PROFILE)

    assert row == ("CANCELLED", "BR-R05", None)
    assert texts == [
        "No tienes permiso para registrar comprobantes de Trabajo (Microsoft). "
        "Pídele al administrador que te habilite."
    ]


async def test_old_date_is_a_warning_line(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """D80: a date 400 days old still routes, and the summary's warning line names the date."""
    old = await _today(db_pool) - timedelta(days=OLD_INVOICE_DAYS)

    row, texts = await _route_one(db_pool, _reading(old))

    assert row == ("FETCHING_DOCS", "BR-R02", WORK_FLOW)
    assert texts[0].splitlines()[-1] == "⚠️ Revisa: fecha"


async def test_conflict_is_a_warning_line(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """D80: a summary for a reading with one conflict still routes, naming that field's label."""
    base = _reading(await _today(db_pool))
    reading = Reading(
        fields={**base.fields, "numero": _field(base.value("numero") or "", flag="conflict")},
        document_url=None,
    )

    row, texts = await _route_one(db_pool, reading)

    assert row == ("FETCHING_DOCS", "BR-R02", WORK_FLOW)
    assert texts[0].splitlines()[-1] == "⚠️ Revisa: número"


async def test_inferred_tipo_is_named(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """D79: a tipo repaired from the serie letter is named on the summary's "Deducido" line."""
    reading = _reading(await _today(db_pool), tipo_flag="inferred")

    row, texts = await _route_one(db_pool, reading)

    assert row == ("FETCHING_DOCS", "BR-R02", WORK_FLOW)
    assert texts[0].splitlines()[-1] == "ℹ️ Deducido del comprobante: tipo de comprobante"


async def test_low_confidence_without_flag_is_not_named(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-R04: a field read at confidence 0.9 with no flag gets no "Deducido" line."""
    base = _reading(await _today(db_pool))
    reading = Reading(
        fields={**base.fields, "tipo": _field(FACTURA, INFERRED_CONFIDENCE)},
        document_url=None,
    )

    row, texts = await _route_one(db_pool, reading)

    assert row == ("FETCHING_DOCS", "BR-R02", WORK_FLOW)
    assert "Deducido" not in texts[0]


async def test_routed_invoice_enqueues_one_fetch_job(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """S4.2, R-5.1: a routed invoice leaves exactly one queued job, the ``fetch`` step for it."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _routing_invoice(db_pool, profile_id, _reading(await _today(db_pool)))

    await _run(db_pool, cfg, invoice_id)

    async with db_pool.connection() as conn:
        assert await _jobs(conn, invoice_id) == ["fetch"]


async def test_refused_invoice_enqueues_no_fetch_job(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """S4.2, R-5.1: a refused invoice is cancelled with no ``fetch`` job queued."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _routing_invoice(db_pool, profile_id, _reading(await _today(db_pool), tipo=BOLETA))

    await _run(db_pool, cfg, invoice_id)

    async with db_pool.connection() as conn:
        assert "fetch" not in await _jobs(conn, invoice_id)


async def test_second_run_changes_nothing(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """R-9.4: running the route handler again on a routed invoice sends nothing more."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _routing_invoice(db_pool, profile_id, _reading(await _today(db_pool)))
    await _run(db_pool, cfg, invoice_id)

    await _run(db_pool, cfg, invoice_id)

    async with db_pool.connection() as conn:
        assert await _invoice(conn, invoice_id) == ("FETCHING_DOCS", "BR-R02", WORK_FLOW)
        assert len(await _texts(conn, profile_id)) == 1


async def test_total_above_cap_waits_for_confirmation(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.13, BR-D06: a total above the cap waits in CONFIRMING with the question and two buttons."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    reading = _reading(await _today(db_pool), total="63.50")
    invoice_id = await _routing_invoice(db_pool, profile_id, reading)

    await _run(db_pool, cfg, invoice_id)

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
        jobs = await _jobs(conn, invoice_id)
    assert row == ("CONFIRMING", "BR-D06", WORK_FLOW)
    assert jobs == []
    assert len(outbox) == 1
    text, rule_id, buttons = outbox[0]
    assert rule_id == "BR-D06"
    assert text.startswith(f"🧾 Factura F001-{reading.value('numero')}")
    assert text.endswith(
        "Esta factura es de S/ 63.50 y el tope es S/ 50.00: se reembolsarán S/ 50.00. ¿Continúo?"
    )
    assert buttons == [
        {"id": button_id(invoice_id, "continue"), "title": "Continuar"},
        {"id": button_id(invoice_id, "cancel"), "title": "Cancelar"},
    ]


async def test_total_at_cap_routes_without_a_question(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.13, BR-D06: a total at the cap routes normally, with no question and its fetch job."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _routing_invoice(db_pool, profile_id, _reading(await _today(db_pool), total="50.00"))

    await _run(db_pool, cfg, invoice_id)

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
        jobs = await _jobs(conn, invoice_id)
    assert row == ("FETCHING_DOCS", "BR-R02", WORK_FLOW)
    assert jobs == ["fetch"]
    assert len(outbox) == 1
    assert "¿Continúo?" not in outbox[0][0]
    assert outbox[0][2] == [
        {"id": button_id(invoice_id, "edit"), "title": "Editar"},
        {"id": button_id(invoice_id, "cancel"), "title": "Cancelar"},
    ]


async def test_flow_without_reimbursement_is_not_asked(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.13, BR-D06: a flow with no reimbursement asks nothing, even above the other flow's cap."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _routing_invoice(
        db_pool, profile_id, _reading(await _today(db_pool), buyer=OWNER_RUC, total="63.50")
    )

    await _run(db_pool, cfg, invoice_id)

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
        jobs = await _jobs(conn, invoice_id)
    assert row == ("FETCHING_DOCS", "BR-R01", OWNER_FLOW)
    assert jobs == ["fetch"]
    assert len(outbox) == 1
    assert "¿Continúo?" not in outbox[0][0]
    assert outbox[0][2] == [
        {"id": button_id(invoice_id, "edit"), "title": "Editar"},
        {"id": button_id(invoice_id, "cancel"), "title": "Cancelar"},
    ]


async def test_routed_summary_has_edit_and_cancel_buttons(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.4: a routed invoice's summary carries [Editar] then [Cancelar]."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _routing_invoice(db_pool, profile_id, _reading(await _today(db_pool)))

    await _run(db_pool, cfg, invoice_id)

    async with db_pool.connection() as conn:
        outbox = await _outbox(conn, profile_id)
    assert len(outbox) == 1
    assert outbox[0][2] == [
        {"id": button_id(invoice_id, "edit"), "title": "Editar"},
        {"id": button_id(invoice_id, "cancel"), "title": "Cancelar"},
    ]


async def test_dni_buyer_asks_which_flow(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-R03: an unmatched DNI buyer gets one question with a button per candidate flow plus cancel."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _routing_invoice(
        db_pool, profile_id, _reading(await _today(db_pool), buyer=DNI_BUYER, buyer_type="1")
    )

    await _run(db_pool, cfg, invoice_id)

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
        jobs = await _jobs(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
    assert row == ("CONFIRMING", "BR-R03", None)
    assert jobs == []
    assert len(outbox) == 1
    text, rule_id, buttons = outbox[0]
    assert rule_id == "BR-R03"
    assert "del DNI 40382716" in text
    assert buttons == [
        {"id": button_id(invoice_id, "pick", 0), "title": "Mi empresa (Google)"},
        {"id": button_id(invoice_id, "pick", 1), "title": "Trabajo (Microsoft)"},
        {"id": button_id(invoice_id, "cancel"), "title": "Cancelar"},
    ]
    assert flow_data["choices"] == [OWNER_FLOW, WORK_FLOW]


async def test_answered_flow_is_routed_to_summary(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-R03: a stored answers.flow that is still a candidate routes straight to the summary."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _routing_invoice(
        db_pool,
        profile_id,
        _reading(await _today(db_pool), buyer=DNI_BUYER, buyer_type="1"),
        answers={"flow": WORK_FLOW},
    )

    await _run(db_pool, cfg, invoice_id)

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        texts = await _texts(conn, profile_id)
        jobs = await _jobs(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
    assert row == ("FETCHING_DOCS", "BR-R03", WORK_FLOW)
    assert jobs == ["fetch"]
    assert len(texts) == 1
    assert "➡️ Trabajo (Microsoft)" in texts[0]
    assert "choices" not in flow_data


async def test_no_candidate_for_the_tipo_is_cancelled(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-R07: an unmatched buyer whose tipo no flow accepts is cancelled with BR-R07."""
    row, texts = await _route_one(
        db_pool, _reading(await _today(db_pool), buyer=DNI_BUYER, buyer_type="1", tipo="07")
    )

    assert row == ("CANCELLED", "BR-R07", None)
    assert texts == [
        f"Este comprobante está a nombre del DNI {DNI_BUYER}, que no corresponde a ninguno "
        "de tus flujos, así que no lo proceso."
    ]


async def test_doubtful_ocr_buyer_asks_confirmation(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-R04: an OCR buyer below the review threshold asks for confirmation with three buttons."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _routing_invoice(
        db_pool,
        profile_id,
        _ocr_buyer_reading(await _today(db_pool)),
    )

    await _run(db_pool, cfg, invoice_id)

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
        jobs = await _jobs(conn, invoice_id)
    assert row == ("CONFIRMING", "BR-R04", WORK_FLOW)
    assert jobs == []
    assert len(outbox) == 1
    text, rule_id, buttons = outbox[0]
    assert rule_id == "BR-R04"
    assert WORK_RUC in text
    assert "Trabajo (Microsoft)" in text
    assert buttons == [
        {"id": button_id(invoice_id, "yes"), "title": "Sí"},
        {"id": button_id(invoice_id, "change"), "title": "Cambiar"},
        {"id": button_id(invoice_id, "cancel"), "title": "Cancelar"},
    ]


async def test_buyer_ok_true_continues_to_summary(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-R04: a confirmed doubtful buyer goes on to the summary and fetch."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _routing_invoice(
        db_pool,
        profile_id,
        _ocr_buyer_reading(await _today(db_pool)),
        answers={"buyer_ok": True},
    )

    await _run(db_pool, cfg, invoice_id)

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)
    assert row == ("FETCHING_DOCS", "BR-R02", WORK_FLOW)
    assert jobs == ["fetch"]
    assert len(texts) == 1
    assert "¿Correcto?" not in texts[0]


async def test_buyer_ok_false_asks_which_flow(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-R03: a doubtful buyer the sender refused is asked to pick the flow instead."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _routing_invoice(
        db_pool,
        profile_id,
        _ocr_buyer_reading(await _today(db_pool)),
        answers={"buyer_ok": False},
    )

    await _run(db_pool, cfg, invoice_id)

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert row == ("CONFIRMING", "BR-R03", None)
    text, rule_id, buttons = outbox[0]
    assert rule_id == "BR-R03"
    assert "¿En qué flujo lo registro?" in text
    assert len(buttons) == 3


async def test_flow_picked_after_buyer_refused_continues_to_summary(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-R03, D92: a flow picked after [Cambiar] routes on; neither question is asked again."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _routing_invoice(
        db_pool,
        profile_id,
        _ocr_buyer_reading(await _today(db_pool)),
        answers={"buyer_ok": False, "flow": WORK_FLOW},
    )

    await _run(db_pool, cfg, invoice_id)

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
    assert row == ("FETCHING_DOCS", "BR-R03", WORK_FLOW)
    assert jobs == ["fetch"]


async def test_two_brand_store_with_unknown_serie_asks_brand(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-R09: a two-brand store whose serie matches no prefix asks which local."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    directory = _directory(_store(OWNER_RUC, "Local Uno", "Local Dos"))
    invoice_id = await _routing_invoice(db_pool, profile_id, _reading(await _today(db_pool)))

    await _run(db_pool, cfg, invoice_id, directory)

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
        jobs = await _jobs(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
    assert row == ("CONFIRMING", "BR-R09", WORK_FLOW)
    assert jobs == []
    assert len(outbox) == 1
    text, rule_id, buttons = outbox[0]
    assert rule_id == "BR-R09"
    assert "Comercio Sintético" in text
    assert buttons == [
        {"id": button_id(invoice_id, "brand", 0), "title": "Local Uno"},
        {"id": button_id(invoice_id, "brand", 1), "title": "Local Dos"},
        {"id": button_id(invoice_id, "cancel"), "title": "Cancelar"},
    ]
    assert flow_data["choices"] == ["Local Uno", "Local Dos"]


async def test_answered_comercio_skips_the_brand_question(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-R09: a stored answers.comercio that is a brand routes to the summary."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    directory = _directory(_store(OWNER_RUC, "Local Uno", "Local Dos"))
    invoice_id = await _routing_invoice(
        db_pool,
        profile_id,
        _reading(await _today(db_pool)),
        answers={"comercio": "Local Dos"},
    )

    await _run(db_pool, cfg, invoice_id, directory)

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)
    assert row == ("FETCHING_DOCS", "BR-R02", WORK_FLOW)
    assert jobs == ["fetch"]
    assert len(texts) == 1
    assert "¿De qué local" not in texts[0]


async def test_one_brand_store_is_not_asked(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-R09: a store with a single brand resolves it and never asks."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    directory = _directory(_store(OWNER_RUC, "Local Único"))
    invoice_id = await _routing_invoice(db_pool, profile_id, _reading(await _today(db_pool)))

    await _run(db_pool, cfg, invoice_id, directory)

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)
    assert row == ("FETCHING_DOCS", "BR-R02", WORK_FLOW)
    assert jobs == ["fetch"]
    assert len(texts) == 1
    assert "¿De qué local" not in texts[0]


async def test_stale_answered_flow_is_dropped_and_reasked(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-R03: a stored answers.flow that is no longer a candidate is dropped and re-asked."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    # A boleta has only personal_google as a candidate, so the stored work flow is stale.
    invoice_id = await _routing_invoice(
        db_pool,
        profile_id,
        _reading(await _today(db_pool), buyer=DNI_BUYER, buyer_type="1", tipo=BOLETA),
        answers={"flow": WORK_FLOW},
    )

    await _run(db_pool, cfg, invoice_id)

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert row == ("CONFIRMING", "BR-R03", None)
    _text, rule_id, buttons = outbox[0]
    assert rule_id == "BR-R03"
    assert buttons == [
        {"id": button_id(invoice_id, "pick", 0), "title": "Mi empresa (Google)"},
        {"id": button_id(invoice_id, "cancel"), "title": "Cancelar"},
    ]


async def test_stale_comercio_is_dropped_and_reasked(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-R09: a stored answers.comercio that is not a brand is dropped and re-asked."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    directory = _directory(_store(OWNER_RUC, "Local Uno", "Local Dos"))
    invoice_id = await _routing_invoice(
        db_pool,
        profile_id,
        _reading(await _today(db_pool)),
        answers={"comercio": "Otro Local"},
    )

    await _run(db_pool, cfg, invoice_id, directory)

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert row == ("CONFIRMING", "BR-R09", WORK_FLOW)
    _text, rule_id, buttons = outbox[0]
    assert rule_id == "BR-R09"
    assert [button["title"] for button in buttons] == ["Local Uno", "Local Dos", "Cancelar"]


async def test_second_invoice_with_a_done_key_is_cancelled(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-E04: a second invoice whose key a non-terminal invoice holds is cancelled."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    reading = _reading(await _today(db_pool))
    first = await _routing_invoice(db_pool, profile_id, reading)
    await _run(db_pool, cfg, first)
    async with db_pool.connection() as conn, conn.transaction():
        await set_state(conn, first, "DONE")

    second = await _routing_invoice(db_pool, profile_id, reading)
    await _run(db_pool, cfg, second)

    async with db_pool.connection() as conn:
        row = await _invoice(conn, second)
        texts = await _texts(conn, profile_id, "BR-E04")
        jobs = await _jobs(conn, second)
        second_key = await _stored_key(conn, second)
    assert row == ("CANCELLED", "BR-E04", None)
    assert jobs == []
    assert second_key is None
    assert len(texts) == 1
    assert f"(#{first})" in texts[0]
    assert f"/estado {first}" in texts[0]


async def test_same_reading_from_two_profiles_is_not_a_duplicate(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D93, BR-E04, R-10.1: the natural key is per profile, so two profiles may hold it."""
    first_profile = f"{FULL_PROFILE}-{uuid4().hex}"
    second_profile = f"{FULL_PROFILE}-{uuid4().hex}"
    first_cfg = _config(first_profile, FULL_PROFILE)
    second_cfg = _config(second_profile, FULL_PROFILE)
    reading = _reading(await _today(db_pool))
    first = await _routing_invoice(db_pool, first_profile, reading)
    await _run(db_pool, first_cfg, first)
    async with db_pool.connection() as conn, conn.transaction():
        await set_state(conn, first, "DONE")

    second = await _routing_invoice(db_pool, second_profile, reading)
    await _run(db_pool, second_cfg, second)

    async with db_pool.connection() as conn:
        row = await _invoice(conn, second)
        first_duplicates = await _texts(conn, first_profile, "BR-E04")
        second_duplicates = await _texts(conn, second_profile, "BR-E04")
        first_key = await _stored_key(conn, first)
        second_key = await _stored_key(conn, second)
    assert row == ("FETCHING_DOCS", "BR-R02", WORK_FLOW)
    assert first_duplicates == []
    assert second_duplicates == []
    assert first_key is not None
    assert first_key.startswith(f"{first_profile}|")
    assert second_key is not None
    assert second_key.startswith(f"{second_profile}|")


async def test_failed_holder_gives_the_key_up(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D93: a FAILED holder releases the key, so the next invoice routes and holds it."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    reading = _reading(await _today(db_pool))
    first = await _routing_invoice(db_pool, profile_id, reading)
    await _run(db_pool, cfg, first)
    async with db_pool.connection() as conn, conn.transaction():
        await set_state(conn, first, "FAILED", rule_id="BR-E07")

    second = await _routing_invoice(db_pool, profile_id, reading)
    await _run(db_pool, cfg, second)

    key = f"{profile_id}|{OWNER_RUC}|{FACTURA}|F001|{reading.value('numero')}"
    async with db_pool.connection() as conn:
        row = await _invoice(conn, second)
        first_key = await _stored_key(conn, first)
        second_key = await _stored_key(conn, second)
    assert row == ("FETCHING_DOCS", "BR-R02", WORK_FLOW)
    assert first_key is None
    assert second_key == key


async def test_rerouted_invoice_is_not_its_own_duplicate(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D93: an invoice re-routed after an answer keeps its key and is not its own duplicate."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    reading = _reading(await _today(db_pool), buyer=DNI_BUYER, buyer_type="1")
    invoice_id = await _routing_invoice(db_pool, profile_id, reading)
    await _run(db_pool, cfg, invoice_id)
    invoice_id = await _routing_invoice(
        db_pool, profile_id, reading, invoice_id=invoice_id, answers={"flow": WORK_FLOW}
    )

    await _run(db_pool, cfg, invoice_id)

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        duplicates = await _texts(conn, profile_id, "BR-E04")
        stored = await _stored_key(conn, invoice_id)
    assert row == ("FETCHING_DOCS", "BR-R03", WORK_FLOW)
    assert duplicates == []
    assert stored == f"{profile_id}|{OWNER_RUC}|{FACTURA}|F001|{reading.value('numero')}"


async def test_edited_numero_changes_the_stored_key(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D93: an edited numero makes the invoice's re-route store the new natural key."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _routing_invoice(db_pool, profile_id, _reading(await _today(db_pool)))
    await _run(db_pool, cfg, invoice_id)
    edited = _reading(await _today(db_pool))
    await _routing_invoice(db_pool, profile_id, edited, invoice_id=invoice_id)

    await _run(db_pool, cfg, invoice_id)

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        stored = await _stored_key(conn, invoice_id)
    assert row == ("FETCHING_DOCS", "BR-R02", WORK_FLOW)
    assert stored == f"{profile_id}|{OWNER_RUC}|{FACTURA}|F001|{edited.value('numero')}"


async def test_edit_during_routing_wins(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D95: a route job superseded by an edit's own job writes nothing; the edit's job routes."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _routing_invoice(db_pool, profile_id, _reading(await _today(db_pool)))
    async with db_pool.connection() as conn, conn.transaction():
        first_id = await enqueue(conn, invoice_id, ROUTE_STEP)

    edited = _reading(await _today(db_pool))
    async with db_pool.connection() as conn, conn.transaction():
        await save_reading(conn, invoice_id, edited)
        second_id = await enqueue(conn, invoice_id, ROUTE_STEP)

    await _run(db_pool, cfg, invoice_id, job=_job(first_id, invoice_id))
    async with db_pool.connection() as conn:
        assert await _invoice(conn, invoice_id) == ("ROUTING", None, None)
        assert await _outbox(conn, profile_id) == []
        assert await _stored_key(conn, invoice_id) is None

    await _run(db_pool, cfg, invoice_id, job=_job(second_id, invoice_id))

    async with db_pool.connection() as conn:
        row = await _invoice(conn, invoice_id)
        stored = await _stored_key(conn, invoice_id)
    assert row == ("FETCHING_DOCS", "BR-R02", WORK_FLOW)
    assert stored == f"{profile_id}|{OWNER_RUC}|{FACTURA}|F001|{edited.value('numero')}"


async def test_route_waits_for_an_uncommitted_edit_and_yields_to_it(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D95: a route job blocks on the invoice row while an edit is open, then sees the edit's job."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id, FULL_PROFILE)
    invoice_id = await _routing_invoice(db_pool, profile_id, _reading(await _today(db_pool)))
    async with db_pool.connection() as conn, conn.transaction():
        first_id = await enqueue(conn, invoice_id, ROUTE_STEP)

    async with db_pool.connection() as edit_conn:
        async with edit_conn.transaction():
            await save_reading(edit_conn, invoice_id, _reading(await _today(db_pool)))
            await enqueue(edit_conn, invoice_id, ROUTE_STEP)
            route = asyncio.create_task(_run(db_pool, cfg, invoice_id, job=_job(first_id, invoice_id)))
            await asyncio.sleep(0.3)
            assert not route.done()
        await route

    async with db_pool.connection() as conn:
        assert await _invoice(conn, invoice_id) == ("ROUTING", None, None)
        assert await _outbox(conn, profile_id) == []
        assert await _stored_key(conn, invoice_id) is None
