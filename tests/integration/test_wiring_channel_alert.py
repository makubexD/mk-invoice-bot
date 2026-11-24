"""Integration tests for a rejected channel token's alert (BR-O03, D185)."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.config.loader import load_config
from invoicebot.config.models import Channel, Config, TelegramChannel
from invoicebot.wiring.channels import alert_channel_token

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"

#: The rate-limit key ``alert_channel_token`` claims for a Telegram token (BR-O01, BR-O03).
TELEGRAM_ALERT_KEY = "BR-O03:telegram"


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def clean_outbox_and_claim(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> AsyncIterator[None]:
    """Start each test with no outbox rows and no Telegram BR-O03 claim."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM outbox")
        await conn.execute("DELETE FROM ops_state WHERE key = %s", (TELEGRAM_ALERT_KEY,))
    yield


def _config_with_both_channels() -> Config:
    """Return the example config with Telegram enabled and its admin preferring Telegram."""
    cfg = load_config(EXAMPLE)
    channels: list[Channel] = [
        channel.model_copy(update={"enabled": True}) if isinstance(channel, TelegramChannel) else channel
        for channel in cfg.channels
    ]
    profiles = {"yo": cfg.profiles["yo"].model_copy(update={"preferred_channel": "telegram"})}
    return cfg.model_copy(update={"channels": channels, "profiles": profiles})


async def test_rejected_telegram_token_alerts_the_admin_on_whatsapp(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D185, BR-O03: a rejected Telegram token queues one alert for the admin on WhatsApp."""
    cfg = _config_with_both_channels()

    await alert_channel_token(db_pool, cfg, "Telegram")

    async with db_pool.connection() as conn:
        cur = await conn.execute(
            """SELECT channel, "to", payload_json->>'rule_id'
               FROM outbox ORDER BY id"""
        )
        rows = [(str(row[0]), str(row[1]), str(row[2])) for row in await cur.fetchall()]

    assert rows == [("whatsapp", "yo", "BR-O03")]
