"""Integration tests for the operations rate limit and BR-D05's token alert (BR-O01, BR-D05)."""

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.config.loader import load_config
from invoicebot.config.models import Profile
from invoicebot.pipeline.outgoing import queue_token_error_alert
from invoicebot.rules.messages_es import TOKEN_REASON_DESC
from invoicebot.store.ops_state import claim, last

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
#: The window every claim test uses.
EVERY = timedelta(hours=1)
REPO = "owner/mk-invoice-bot-ops"


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def clean_ops_tables(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> AsyncIterator[None]:
    """Start each test from empty ops_state and outbox tables."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM ops_state")
        await conn.execute("DELETE FROM outbox")
    yield


def _profiles() -> dict[str, Profile]:
    """Return two admin profiles and one non-admin, as the app loads them."""
    cfg = load_config(EXAMPLE)
    owner = cfg.profiles["yo"]
    return {
        "yo": owner,
        "segundo": owner.model_copy(update={"name": "Segundo", "whatsapp": "51977777777"}),
        "asistente": cfg.profiles["asistente"],
    }


async def test_second_claim_within_window_is_false(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-O01: two claims within the window let only the first through."""
    async with db_pool.connection() as conn:
        first = await claim(conn, "BR-O01", EVERY)
        second = await claim(conn, "BR-O01", EVERY)

    assert (first, second) == (True, False)


async def test_claim_after_window_is_true(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-O01: a claim after the window elapsed is true again."""
    async with db_pool.connection() as conn:
        await claim(conn, "BR-O01", EVERY)
        await conn.execute(
            "UPDATE ops_state SET last_at = now() - %s WHERE key = %s",
            (EVERY + timedelta(minutes=1), "BR-O01"),
        )
        again = await claim(conn, "BR-O01", EVERY)

    assert again is True


async def test_concurrent_claims_let_exactly_one_through(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-O01: two concurrent claims on separate connections give exactly one true."""

    async def claimed() -> bool:
        async with db_pool.connection() as conn:
            return await claim(conn, "BR-O01", EVERY)

    results = await asyncio.gather(claimed(), claimed())

    assert sorted(results) == [False, True]


async def test_last_is_none_for_a_missing_key(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-O01: last returns None for a key that was never claimed."""
    async with db_pool.connection() as conn:
        stamped = await last(conn, "BR-O01")

    assert stamped is None


async def test_last_returns_the_claim_time(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-O01: last returns a timestamp after the key was claimed."""
    async with db_pool.connection() as conn:
        assert await claim(conn, "BR-O01", EVERY) is True
        stamped = await last(conn, "BR-O01")

    assert isinstance(stamped, datetime)


async def test_two_token_errors_in_a_day_queue_one_alert_per_admin(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-D05: two token errors in one day queue one alert per admin with repo and reason."""
    profiles = _profiles()
    async with db_pool.connection() as conn:
        first = await queue_token_error_alert(conn, profiles, REPO, "uninstalled")
        second = await queue_token_error_alert(conn, profiles, REPO, "uninstalled")
        cur = await conn.execute(
            """SELECT "to", payload_json->>'text' FROM outbox
               WHERE payload_json->>'rule_id' = 'BR-D05'
               ORDER BY "to" """
        )
        alerts = [(str(row[0]), str(row[1])) for row in await cur.fetchall()]

    assert (first, second) == (True, False)
    assert [recipient for recipient, _ in alerts] == ["segundo", "yo"]
    assert all(REPO in text and TOKEN_REASON_DESC["uninstalled"] in text for _, text in alerts)
