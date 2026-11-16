"""Integration tests for the self-alerts on the outbox backlog and the disk (BR-O01, BR-O02)."""

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, Profile
from invoicebot.ops.selfcheck import check
from invoicebot.queue import outbox

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
DATA_DIR = Path("/data")
RULE_OUTBOX_BACKLOG = "BR-O01"
RULE_DISK = "BR-O02"


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def clean_outbox_and_selfcheck_claims(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> AsyncIterator[None]:
    """Start each test with no outbox rows and no BR-O01 or BR-O02 rate-limit claim."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM outbox")
        await conn.execute("DELETE FROM ops_state WHERE key IN (%s, %s)", (RULE_OUTBOX_BACKLOG, RULE_DISK))
    yield


def _config_with_two_admins() -> Config:
    """Return the example config with a second admin profile on WhatsApp."""
    cfg = load_config(EXAMPLE)
    owner = cfg.profiles["yo"]
    profiles: dict[str, Profile] = {
        "yo": owner,
        "segundo": owner.model_copy(update={"name": "Segundo", "whatsapp": "51977777777"}),
    }
    return cfg.model_copy(update={"profiles": profiles})


async def _queue_pending(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    rows: int,
    age: timedelta | None = None,
) -> None:
    """Insert ``rows`` pending outbox rows, all ``age`` old when one is given."""
    async with pool.connection() as conn, conn.transaction():
        for _ in range(rows):
            await outbox.add(conn, "whatsapp", "yo", {"text": "pendiente"})
        if age is not None:
            await conn.execute("UPDATE outbox SET created_at = now() - %s", (age,))


async def _alert_texts(pool: AsyncConnectionPool[AsyncConnection[Any]], rule_id: str) -> list[str]:
    """Return the texts of the outbox rows carrying ``rule_id``, in insertion order."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT payload_json->>'text' FROM outbox WHERE payload_json->>'rule_id' = %s ORDER BY id",
            (rule_id,),
        )
        return [str(row[0]) for row in await cur.fetchall()]


async def test_outbox_backlog_over_max_rows_alerts_once_per_admin(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-O01: 21 pending rows alert each admin once, and a second check adds none."""
    cfg = _config_with_two_admins()
    await _queue_pending(db_pool, rows=21)

    await check(db_pool, cfg.profiles, cfg.ops.self_alerts, DATA_DIR, lambda _: 0.0)
    await check(db_pool, cfg.profiles, cfg.ops.self_alerts, DATA_DIR, lambda _: 0.0)

    texts = await _alert_texts(db_pool, RULE_OUTBOX_BACKLOG)
    assert len(texts) == 2
    assert all("Hay 21 mensajes sin enviar" in text for text in texts)


async def test_outbox_row_older_than_max_age_alerts(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-O01: one pending row 16 minutes old alerts with its age in minutes."""
    cfg = load_config(EXAMPLE)
    await _queue_pending(db_pool, rows=1, age=timedelta(minutes=16))

    await check(db_pool, cfg.profiles, cfg.ops.self_alerts, DATA_DIR, lambda _: 0.0)

    texts = await _alert_texts(db_pool, RULE_OUTBOX_BACKLOG)
    assert len(texts) == 1
    assert "16 min" in texts[0]


async def test_one_fresh_outbox_row_does_not_alert(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-O01: one fresh pending row is below both thresholds and alerts nobody."""
    cfg = load_config(EXAMPLE)
    await _queue_pending(db_pool, rows=1)

    await check(db_pool, cfg.profiles, cfg.ops.self_alerts, DATA_DIR, lambda _: 0.0)

    assert await _alert_texts(db_pool, RULE_OUTBOX_BACKLOG) == []


async def test_disk_above_threshold_alerts(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-O02: an 81% disk alerts once per admin with the rounded percentage."""
    cfg = _config_with_two_admins()

    await check(db_pool, cfg.profiles, cfg.ops.self_alerts, DATA_DIR, lambda _: 81.0)

    texts = await _alert_texts(db_pool, RULE_DISK)
    assert len(texts) == 2
    assert all("81%" in text for text in texts)


async def test_disk_below_threshold_does_not_alert(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-O02: a 79% disk is below the 80% threshold and alerts nobody."""
    cfg = load_config(EXAMPLE)

    await check(db_pool, cfg.profiles, cfg.ops.self_alerts, DATA_DIR, lambda _: 79.0)

    assert await _alert_texts(db_pool, RULE_DISK) == []
