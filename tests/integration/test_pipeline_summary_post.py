"""Integration tests for saving the summary's inputs and posting it once (D206)."""

from __future__ import annotations

import itertools
from datetime import date
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.config.loader import load_config
from invoicebot.config.models import Config
from invoicebot.extract.fields import Field, Reading, Source
from invoicebot.pipeline.summary_post import post_summary, save_summary_inputs
from invoicebot.rules.messages_es import SUMMARY_MOVING_QUIET, SUMMARY_TAP_HINT
from invoicebot.store.invoices import create_invoice, save_reading, set_flow, set_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
# From tests/synthetic_rucs.txt; no real RUC appears in this file.
ISSUER_RUC = "51900000000"
WORK_FLOW = "work_microsoft"
FULL_PROFILE = "yo"
RULE_ID = "BR-R02"
EXTRA = "🧾 línea extra"
FOUND = "📄 Encontré el PDF"
_NEXT_NUMERO = itertools.count(60000)


def _config(profile_id: str) -> Config:
    """Return the example config with ``profile_id`` copied from the full profile."""
    cfg = load_config(EXAMPLE)
    return cfg.model_copy(update={"profiles": {profile_id: cfg.profiles[FULL_PROFILE]}})


def _reading(fecha: date, *, source: Source = "qr", total_source: Source | None = None) -> Reading:
    """Build a complete reading whose ``total`` may come from another source."""
    fields = {
        "ruc_emisor": Field(value=ISSUER_RUC, source="qr", confidence=1.0),
        "tipo": Field(value="01", source="qr", confidence=1.0),
        "serie": Field(value="F001", source="qr", confidence=1.0),
        "numero": Field(value=str(next(_NEXT_NUMERO)), source="qr", confidence=1.0),
        "igv": Field(value="7.61", source="qr", confidence=1.0),
        "total": Field(value="49.90", source=total_source or source, confidence=1.0),
        "fecha_emision": Field(value=fecha.isoformat(), source="qr", confidence=1.0),
    }
    return Reading(fields=fields, document_url=None)


async def _today(pool: AsyncConnectionPool[AsyncConnection[Any]]) -> date:
    """Return today in Lima from the database clock."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT (now() AT TIME ZONE 'America/Lima')::date")
        row = await cur.fetchone()
    assert row is not None
    return cast(date, row[0])


async def _invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]], profile_id: str, reading: Reading, *, saved: bool
) -> int:
    """Create a routed invoice holding ``reading``, with or without saved summary inputs."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await save_reading(conn, invoice_id, reading)
        await set_flow(conn, invoice_id, WORK_FLOW)
        await set_state(conn, invoice_id, "FETCHING_DOCS", rule_id=RULE_ID)
        if saved:
            await save_summary_inputs(conn, invoice_id, rule_id=RULE_ID, extra_lines=[EXTRA])
    return invoice_id


async def _post(
    pool: AsyncConnectionPool[AsyncConnection[Any]], cfg: Config, invoice_id: int, **kwargs: Any
) -> bool:
    """Run :func:`post_summary` in its own transaction."""
    async with pool.connection() as conn, conn.transaction():
        return await post_summary(conn, cfg, invoice_id, **kwargs)


async def _outbox(pool: AsyncConnectionPool[AsyncConnection[Any]], profile_id: str) -> list[tuple[str, Any]]:
    """Return the outbox's ``(text, buttons)`` rows for ``profile_id``, oldest first."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT payload_json->>'text', payload_json->'buttons'
            FROM outbox WHERE "to" = %s ORDER BY id
            """,
            (profile_id,),
        )
        return [(str(text), buttons) for text, buttons in await cur.fetchall()]


async def test_saved_inputs_post_one_summary_with_the_moving_line(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D206: saved inputs and no found lines queue one summary ending in the moving line."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id)
    invoice_id = await _invoice(db_pool, profile_id, _reading(await _today(db_pool)), saved=True)

    assert await _post(db_pool, cfg, invoice_id) is True

    outbox = await _outbox(db_pool, profile_id)
    assert len(outbox) == 1
    text, _ = outbox[0]
    assert EXTRA in text
    assert text.endswith(f"{EXTRA}\n{SUMMARY_MOVING_QUIET}")  # all-QR reading: no buttons (D189)


async def test_a_second_call_queues_nothing(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """D206: once posted, a second call returns False and queues nothing."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id)
    invoice_id = await _invoice(db_pool, profile_id, _reading(await _today(db_pool)), saved=True)
    assert await _post(db_pool, cfg, invoice_id) is True

    assert await _post(db_pool, cfg, invoice_id) is False

    assert len(await _outbox(db_pool, profile_id)) == 1


async def test_no_saved_inputs_queue_nothing(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """D206: an invoice with no saved summary inputs returns False and queues nothing."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id)
    invoice_id = await _invoice(db_pool, profile_id, _reading(await _today(db_pool)), saved=False)

    assert await _post(db_pool, cfg, invoice_id) is False

    assert await _outbox(db_pool, profile_id) == []


async def test_found_lines_end_with_the_tap_hint_and_buttons(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D206: found lines then the tap hint close a summary that has [Corregir] [Anular] buttons."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id)
    reading = _reading(await _today(db_pool), total_source="ocr")
    invoice_id = await _invoice(db_pool, profile_id, reading, saved=True)

    assert await _post(db_pool, cfg, invoice_id, found_lines=[FOUND]) is True

    ((text, buttons),) = await _outbox(db_pool, profile_id)
    assert text.endswith(f"{FOUND}\n{SUMMARY_TAP_HINT}")
    assert [b["title"] for b in buttons] == ["✏️ Corregir", "✖️ Anular"]


async def test_xml_confirmed_reading_has_no_review_line_or_buttons(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D206: a reading confirmed by the XML shows no Revisa line and no buttons."""
    profile_id = f"{FULL_PROFILE}-{uuid4().hex}"
    cfg = _config(profile_id)
    reading = _reading(await _today(db_pool), source="xml")
    invoice_id = await _invoice(db_pool, profile_id, reading, saved=True)

    assert await _post(db_pool, cfg, invoice_id, found_lines=[FOUND]) is True

    ((text, buttons),) = await _outbox(db_pool, profile_id)
    assert "⚠️ Revisa" not in text
    assert buttons == []
    assert text.endswith(FOUND)
