"""Integration tests for the app's backup-failure alert wiring (BR-O04)."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.app import RULE_BACKUP_ALERT, _alert_backup_failure
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, Profile
from invoicebot.rules.messages_es import BACKUP_REASON_DESC

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def clean_outbox_and_backup_claim(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> AsyncIterator[None]:
    """Start each test with no outbox rows and no BR-O04 rate-limit claim."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM outbox")
        await conn.execute("DELETE FROM ops_state WHERE key = %s", (RULE_BACKUP_ALERT,))
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


async def test_backup_failure_queues_one_alert_per_admin_inside_the_window(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-O04: an upload failure queues one alert per admin, and a second call adds none."""
    cfg = _config_with_two_admins()

    await _alert_backup_failure(db_pool, cfg, "upload")
    await _alert_backup_failure(db_pool, cfg, "upload")

    async with db_pool.connection() as conn:
        cur = await conn.execute(
            """SELECT "to", payload_json->>'rule_id', payload_json->>'text'
               FROM outbox WHERE channel = 'whatsapp' ORDER BY "to" """
        )
        rows = [(str(row[0]), str(row[1]), str(row[2])) for row in await cur.fetchall()]

    assert [recipient for recipient, _, _ in rows] == ["segundo", "yo"]
    assert {rule_id for _, rule_id, _ in rows} == {RULE_BACKUP_ALERT}
    assert all(BACKUP_REASON_DESC["upload"] in text for _, _, text in rows)
