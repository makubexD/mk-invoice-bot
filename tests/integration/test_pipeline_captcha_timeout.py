"""Integration tests for the captcha timeout job (D209, BR-F05)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.fetchers.browser.protocol import CaptchaForm
from invoicebot.pipeline.captcha import CAPTCHA_DIRECTORY
from invoicebot.pipeline.captcha_timeout import make_captcha_timeout_handler
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.steps import CAPTCHA_TIMEOUT_STEP, FETCH_STEP
from invoicebot.queue.jobs import Job
from invoicebot.store import captchas
from invoicebot.store.invoices import create_invoice, set_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

PORTAL_KEY = "synthetic_portal"
PORTAL_URL = "https://portal.ejemplo.pe/consulta"
SENDER = "asistente"
EXPIRED_S = -5


def _form() -> CaptchaForm:
    return CaptchaForm(
        page_url=PORTAL_URL,
        action_url=f"{PORTAL_URL}/enviar",
        fields=(("serie", "F001"),),
        captcha_name="captcha",
        submit_name="ok",
        submit_value="Consultar",
    )


async def _waiting(
    pool: AsyncConnectionPool[AsyncConnection[Any]], data_dir: Path, *, answered: bool = False
) -> tuple[int, Path]:
    """Create an invoice waiting on an expired captcha whose PNG is on disk."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, SENDER, "whatsapp")
        await set_state(conn, invoice_id, "FETCHING_DOCS")
        await set_state(conn, invoice_id, "WAITING_CAPTCHA", rule_id="BR-F18", expected="FETCHING_DOCS")
        image = data_dir / CAPTCHA_DIRECTORY / str(invoice_id) / "ask.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"png")
        await captchas.put(conn, invoice_id, PORTAL_KEY, _form().model_dump(mode="json"), str(image), 60)
        if answered:
            assert await captchas.set_answer(conn, invoice_id, "AB12")
        await conn.execute(
            "UPDATE portal_captchas SET expires_at = now() + make_interval(secs => %s) WHERE invoice_id = %s",
            (EXPIRED_S, invoice_id),
        )
        await queue_message(
            conn, Addressee("whatsapp", SENDER), "captcha", invoice_id=invoice_id, rule_id="BR-F18"
        )
    return invoice_id, image


async def _run(pool: AsyncConnectionPool[AsyncConnection[Any]], data_dir: Path, invoice_id: int) -> None:
    handler = make_captcha_timeout_handler(pool, data_dir)
    await handler(
        Job(
            id=0,
            invoice_id=invoice_id,
            step=CAPTCHA_TIMEOUT_STEP,
            attempts=0,
            crashes=0,
            lease_until=datetime.now(UTC),
        )
    )


async def _facts(
    pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int
) -> tuple[str, bool, list[str], list[str], int]:
    """Return state, row present, outbox rule ids, attempt reasons and fetch job count."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT state FROM invoices WHERE id = %s", (invoice_id,))
        row = await cur.fetchone()
        assert row is not None
        pending = await captchas.get(conn, invoice_id)
        cur = await conn.execute(
            "SELECT payload_json->>'rule_id' FROM outbox WHERE invoice_id = %s ORDER BY id", (invoice_id,)
        )
        rules = [str(r[0]) for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT fetcher || ':' || reason FROM fetch_attempts WHERE invoice_id = %s", (invoice_id,)
        )
        attempts = [str(r[0]) for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT count(*) FROM jobs WHERE invoice_id = %s AND step = %s", (invoice_id, FETCH_STEP)
        )
        jobs = await cur.fetchone()
        assert jobs is not None
    return str(row[0]), pending is not None, rules, attempts, int(jobs[0])


async def test_expired_unanswered_ask_moves_on(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D209: BR-F05 is sent, the row and PNG go, a captcha_timeout attempt is kept, the fetch resumes."""
    invoice_id, image = await _waiting(db_pool, tmp_path)
    await _run(db_pool, tmp_path, invoice_id)
    assert await _facts(db_pool, invoice_id) == (
        "FETCHING_DOCS",
        False,
        ["BR-F18", "BR-F05"],
        ["portal_registry:captcha_timeout"],
        1,
    )
    assert not image.exists()


async def test_answered_ask_is_left_alone(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D209: an answered ask is the sender's to resume; the timeout changes nothing."""
    invoice_id, image = await _waiting(db_pool, tmp_path, answered=True)
    await _run(db_pool, tmp_path, invoice_id)
    assert await _facts(db_pool, invoice_id) == ("WAITING_CAPTCHA", True, ["BR-F18"], [], 0)
    assert image.exists()


async def test_cancelled_invoice_is_left_alone(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D209: an invoice that moved on (CANCELLED) is not touched."""
    invoice_id, image = await _waiting(db_pool, tmp_path)
    async with db_pool.connection() as conn, conn.transaction():
        await set_state(conn, invoice_id, "CANCELLED", rule_id="BR-E07")
    await _run(db_pool, tmp_path, invoice_id)
    assert await _facts(db_pool, invoice_id) == ("CANCELLED", True, ["BR-F18"], [], 0)
    assert image.exists()
