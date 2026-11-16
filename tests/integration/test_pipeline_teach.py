"""Integration tests for teaching a portal link and BR-S06's owner review (R-10.2, R-11.4)."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.config.loader import load_config
from invoicebot.config.models import Profile
from invoicebot.net.guard import Resolver
from invoicebot.pipeline.outgoing import Addressee
from invoicebot.pipeline.teach import TeachContext, teach
from invoicebot.rules.messages_es import BUTTON_APPROVE, BUTTON_REJECT, MESSAGES
from invoicebot.store import taught_links
from invoicebot.store.taught_links import TaughtLink

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
#: From tests/synthetic_rucs.txt; no real RUC may appear here.
ISSUER_RUC = 20000000001
PUBLIC_ADDRESS = "93.184.216.34"
GOOD_URL = "https://portal.example.pe/consulta"
OTHER_URL = "https://portal.example.pe/otra"
RAZON_SOCIAL = "Tienda Example"
SENDER = "asistente"


class FakeResolver(Resolver):
    """A ``Resolver`` that answers the public address, whatever the host is."""

    async def resolve(self, host: str) -> list[str]:
        """Return the fixed public address."""
        return [PUBLIC_ADDRESS]


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def clean_teach_tables(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> AsyncIterator[None]:
    """Start each test from empty taught links and outbox rows."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM taught_links")
        await conn.execute("DELETE FROM outbox")
    yield


def _context() -> TeachContext:
    """Return a context with two admin profiles and one non-admin sender."""
    cfg = load_config(EXAMPLE)
    owner = cfg.profiles["yo"]
    profiles: dict[str, Profile] = {
        "yo": owner,
        "segundo": owner.model_copy(update={"name": "Segundo", "whatsapp": "51977777777"}),
        SENDER: cfg.profiles[SENDER],
    }
    return TeachContext(
        profiles=profiles,
        sender=Addressee("whatsapp", SENDER),
        resolver=FakeResolver(),
    )


async def _outbox(
    conn: AsyncConnection[Any],
    recipient: str,
) -> list[tuple[str, str | None, list[dict[str, str]]]]:
    """Return a recipient's outbox ``(text, rule_id, buttons)`` rows, oldest first."""
    cur = await conn.execute(
        """
        SELECT payload_json->>'text', payload_json->>'rule_id', payload_json->'buttons'
        FROM outbox
        WHERE "to" = %s
        ORDER BY id
        """,
        (recipient,),
    )
    rows = await cur.fetchall()
    return [(str(text), rule_id, cast(list[dict[str, str]], buttons)) for text, rule_id, buttons in rows]


def _decision_buttons() -> list[dict[str, str]]:
    """Return the button payload BR-S06's owner message must carry."""
    return [
        {"id": f"t{ISSUER_RUC}:ok", "title": BUTTON_APPROVE},
        {"id": f"t{ISSUER_RUC}:no", "title": BUTTON_REJECT},
    ]


def _owner_text() -> str:
    """Return BR-S06's owner message for the sender's proposal."""
    return MESSAGES["BR-S06_owner"].format(
        sender="Asistente",
        razon_social=RAZON_SOCIAL,
        ruc_emisor=ISSUER_RUC,
        url=GOOD_URL,
    )


async def test_propose_stores_the_link_and_asks_each_admin(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-10.2: a valid link is stored proposed, thanks the sender and asks each admin."""
    ctx = _context()
    async with db_pool.connection() as conn:
        await teach(conn, ctx, ISSUER_RUC, RAZON_SOCIAL, GOOD_URL)
        link = await taught_links.get(conn, ISSUER_RUC)
        sender_rows = await _outbox(conn, SENDER)
        owner_rows = await _outbox(conn, "yo")
        second_rows = await _outbox(conn, "segundo")

    assert link == TaughtLink(ISSUER_RUC, GOOD_URL, SENDER, "proposed")
    assert sender_rows == [(MESSAGES["BR-S06"], "BR-S06", [])]
    assert owner_rows == [(_owner_text(), "BR-S06", _decision_buttons())]
    assert second_rows == [(_owner_text(), "BR-S06", _decision_buttons())]


async def test_propose_invalid_link_repeats_br_f02(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.4: a non-https link stores nothing and repeats BR-F02 to the sender."""
    ctx = _context()
    async with db_pool.connection() as conn:
        await teach(conn, ctx, ISSUER_RUC, RAZON_SOCIAL, "http://portal.example.pe/consulta")
        link = await taught_links.get(conn, ISSUER_RUC)
        sender_rows = await _outbox(conn, SENDER)
        owner_rows = await _outbox(conn, "yo")

    expected = MESSAGES["BR-F02"].format(razon_social=RAZON_SOCIAL, ruc_emisor=ISSUER_RUC)
    assert link is None
    assert sender_rows == [(expected, "BR-F02", [])]
    assert owner_rows == []


async def test_propose_second_link_while_proposed_asks_no_admin_again(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S06: a second proposal while one is proposed thanks the sender and asks nobody."""
    ctx = _context()
    async with db_pool.connection() as conn:
        await teach(conn, ctx, ISSUER_RUC, RAZON_SOCIAL, GOOD_URL)
        await conn.execute("DELETE FROM outbox")
        await teach(conn, ctx, ISSUER_RUC, RAZON_SOCIAL, OTHER_URL)
        link = await taught_links.get(conn, ISSUER_RUC)
        sender_rows = await _outbox(conn, SENDER)
        owner_rows = await _outbox(conn, "yo")
        second_rows = await _outbox(conn, "segundo")

    assert link == TaughtLink(ISSUER_RUC, GOOD_URL, SENDER, "proposed")
    assert sender_rows == [(MESSAGES["BR-S06"], "BR-S06", [])]
    assert owner_rows == []
    assert second_rows == []


async def test_propose_link_for_approved_issuer_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S06: a link for an approved RUC answers BR-S06 and changes nothing."""
    ctx = _context()
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO taught_links (ruc_emisor, url, profile_id, state, approved_at)
            VALUES (%s, %s, %s, 'approved', now())
            """,
            (ISSUER_RUC, GOOD_URL, SENDER),
        )
        await teach(conn, ctx, ISSUER_RUC, RAZON_SOCIAL, OTHER_URL)
        link = await taught_links.get(conn, ISSUER_RUC)
        sender_rows = await _outbox(conn, SENDER)
        owner_rows = await _outbox(conn, "yo")

    assert link == TaughtLink(ISSUER_RUC, GOOD_URL, SENDER, "approved")
    assert sender_rows == [(MESSAGES["BR-S06"], "BR-S06", [])]
    assert owner_rows == []


async def test_store_propose_replaces_a_rejected_row(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S06: a rejected link can be proposed again."""
    async with db_pool.connection() as conn:
        assert await taught_links.propose(conn, ISSUER_RUC, GOOD_URL, SENDER) is True
        assert await taught_links.decide(conn, ISSUER_RUC, approved=False) is not None
        assert await taught_links.propose(conn, ISSUER_RUC, OTHER_URL, SENDER) is True
        link = await taught_links.get(conn, ISSUER_RUC)

    assert link == TaughtLink(ISSUER_RUC, OTHER_URL, SENDER, "proposed")


@pytest.mark.parametrize("approved", [True, False])
async def test_store_decide_moves_a_proposed_link_once(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    approved: bool,
) -> None:
    """BR-S06: decide moves a proposed link to approved or rejected, then returns None."""
    state = "approved" if approved else "rejected"
    async with db_pool.connection() as conn:
        assert await taught_links.propose(conn, ISSUER_RUC, GOOD_URL, SENDER) is True
        first = await taught_links.decide(conn, ISSUER_RUC, approved=approved)
        second = await taught_links.decide(conn, ISSUER_RUC, approved=approved)
        cur = await conn.execute(
            "SELECT approved_at IS NOT NULL FROM taught_links WHERE ruc_emisor = %s",
            (ISSUER_RUC,),
        )
        row = await cur.fetchone()

    assert first == TaughtLink(ISSUER_RUC, GOOD_URL, SENDER, state)
    assert second is None
    assert row is not None
    assert row[0] is approved
