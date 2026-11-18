"""Integration tests for the app's rejected-config alert wiring (R-9.6, BR-O05)."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.app import _alert_config_rejected
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
RULE_CONFIG_ALERT = "BR-O05"


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def clean_outbox_and_config_claim(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> AsyncIterator[None]:
    """Start each test with no outbox rows and no BR-O05 rate-limit claim."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM outbox")
        await conn.execute("DELETE FROM ops_state WHERE key = %s", (RULE_CONFIG_ALERT,))
    yield


def _admin_names(cfg: Config) -> list[str]:
    """Return the ids of the config's admin profiles, which every admin alert reaches."""
    return [name for name, profile in cfg.profiles.items() if profile.admin]


async def _outbox_rows(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> list[tuple[str, str, str]]:
    """Return the queued WhatsApp rows as (recipient, rule id, text)."""
    async with db_pool.connection() as conn:
        cur = await conn.execute(
            """SELECT "to", payload_json->>'rule_id', payload_json->>'text'
               FROM outbox WHERE channel = 'whatsapp' ORDER BY "to" """
        )
        return [(str(row[0]), str(row[1]), str(row[2])) for row in await cur.fetchall()]


async def test_rejected_config_queues_one_alert_per_admin(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.6, BR-O05: a rejected config queues one alert per admin with its reason."""
    cfg = load_config(EXAMPLE)

    await _alert_config_rejected(db_pool, cfg, "yaml")

    rows = await _outbox_rows(db_pool)
    assert [recipient for recipient, _, _ in rows] == _admin_names(cfg)
    assert {rule_id for _, rule_id, _ in rows} == {RULE_CONFIG_ALERT}
    assert all("yaml" in text for _, _, text in rows)


async def test_second_rejection_inside_the_window_queues_none(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-9.6, BR-O05: a second rejection within the window queues no further alert."""
    cfg = load_config(EXAMPLE)

    await _alert_config_rejected(db_pool, cfg, "yaml")
    await _alert_config_rejected(db_pool, cfg, "validation")

    rows = await _outbox_rows(db_pool)
    assert [recipient for recipient, _, _ in rows] == _admin_names(cfg)
