"""Integration tests for the route handler and its summary (S4.2)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import Attachment, InboundMessage
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config
from invoicebot.extract.fields import Field, Flag, Reading
from invoicebot.extract.qr_payload import INFERRED_CONFIDENCE
from invoicebot.pipeline.extract import ROUTE_STEP
from invoicebot.pipeline.intake import handle_inbound
from invoicebot.pipeline.route import make_route_handler
from invoicebot.queue.jobs import Job
from invoicebot.routing.router import CompiledRule
from invoicebot.store.invoices import create_invoice, save_reading, set_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
# Both values come from tests/synthetic_rucs.txt; the unknown buyer fails its check digit.
OWNER_RUC = "20000000001"
WORK_RUC = "51900000000"
UNKNOWN_BUYER_RUC = "20000000002"
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


def _config(profile_id: str, template: str) -> Config:
    """Return the example config with one profile, copied from ``template``, under ``profile_id``."""
    cfg = load_config(EXAMPLE)
    return cfg.model_copy(update={"profiles": {profile_id: cfg.profiles[template]}})


def _field(value: str, confidence: float = 1.0, flag: Flag | None = None) -> Field:
    return Field(value=value, source="qr", confidence=confidence, flag=flag)


def _reading(
    fecha: date,
    *,
    buyer: str = WORK_RUC,
    tipo: str = FACTURA,
    tipo_flag: Flag | None = None,
) -> Reading:
    """Build a QR reading of a factura to ``buyer``."""
    return Reading(
        fields={
            "ruc_emisor": _field(OWNER_RUC),
            "tipo": _field(tipo, flag=tipo_flag),
            "serie": _field("F001"),
            "numero": _field("15160"),
            "igv": _field("7.61"),
            "total": _field("49.90"),
            "fecha_emision": _field(fecha.isoformat()),
            "tipo_doc_adq": _field("6"),
            "num_doc_adq": _field(buyer),
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


async def _routing_invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    reading: Reading,
    invoice_id: int | None = None,
) -> int:
    """Save ``reading`` on an invoice (a new one unless given) and move it to ROUTING."""
    async with pool.connection() as conn, conn.transaction():
        if invoice_id is None:
            invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await save_reading(conn, invoice_id, reading)
        await set_state(conn, invoice_id, "ROUTING")
    return invoice_id


async def _run(pool: AsyncConnectionPool[AsyncConnection[Any]], cfg: Config, invoice_id: int) -> None:
    job = Job(
        id=1, invoice_id=invoice_id, step=ROUTE_STEP, attempts=0, crashes=0, lease_until=datetime.now(UTC)
    )
    await make_route_handler(pool, cfg, RULES)(job)


async def _invoice(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str, str | None, str | None]:
    """Return ``(state, rule_id, flow_id)`` of an invoice."""
    cur = await conn.execute("SELECT state, rule_id, flow_id FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return row[0], row[1], row[2]


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

    row, texts = await _route_one(db_pool, _reading(today))

    assert row == ("FETCHING_DOCS", "BR-R02", WORK_FLOW)
    assert len(texts) == 1
    assert "Trabajo (Microsoft)" in texts[0]
    assert texts[0].startswith(f"🧾 Factura F001-15160 · {today:%d/%m/%Y}")
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


async def test_unknown_buyer_is_cancelled_not_asked(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-R07, D80: an unknown buyer gets BR-R07 (the `ask` shortcut) and never enters CONFIRMING."""
    row, texts = await _route_one(db_pool, _reading(await _today(db_pool), buyer=UNKNOWN_BUYER_RUC))

    assert row == ("CANCELLED", "BR-R07", None)
    assert texts == [
        f"Este comprobante está a nombre del RUC {UNKNOWN_BUYER_RUC}, que no corresponde a ninguno "
        "de tus flujos, así que no lo proceso."
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
        fields={**base.fields, "numero": _field("15160", flag="conflict")},
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
