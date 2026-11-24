"""Integration tests for BR-F14's portal offer and its three answers (D189, BR-F14)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import InboundMessage
from invoicebot.config.loader import load_config
from invoicebot.config.models import Profile
from invoicebot.extract.fields import Field, Reading
from invoicebot.net.guard import Resolver
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.portal_offer import ask_portal, handle_press
from invoicebot.pipeline.teach import TeachSetup
from invoicebot.rules.messages_es import (
    BUTTON_APPROVE,
    BUTTON_DONT_KNOW,
    BUTTON_NO,
    BUTTON_REJECT,
    BUTTON_YES,
    MESSAGES,
    state_desc,
)
from invoicebot.store import taught_links
from invoicebot.store.invoices import create_invoice, save_reading
from invoicebot.store.taught_links import TaughtLink

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
#: From tests/synthetic_rucs.txt; no real RUC may appear here.
ISSUER_RUC = 20000000001
PUBLIC_ADDRESS = "93.184.216.34"
#: The URL printed on the ticket, which BR-F14 offers first (D189).
PRINTED_URL = "https://portal.example.pe/consulta"
#: A QR link on a host `qr_url` may not fetch, so BR-F14 may offer it (D189).
OTHER_URL = "https://otro.example.pe/comprobante"
#: SUNAT's generic query page, which BR-F14 never offers (D189).
SUNAT_URL = "https://e-consulta.sunat.gob.pe/cl-ti-itmrconsruc/consulta"
RAZON_SOCIAL = "Tienda Example"
SENDER = "asistente"
#: The owner profile is the example config's only admin (R-11.4).
ADMIN = "yo"
#: A profile that owns no invoice in these tests.
OTHER_PROFILE = "segundo"


class FakeResolver(Resolver):
    """A ``Resolver`` that answers the public address, whatever the host is."""

    async def resolve(self, host: str) -> list[str]:
        """Return the fixed public address."""
        return [PUBLIC_ADDRESS]


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def clean_offer_tables(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> AsyncIterator[None]:
    """Start each test from empty taught links and outbox rows."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM taught_links")
        await conn.execute("DELETE FROM outbox")
    yield


def _setup() -> TeachSetup:
    """Return a setup with the sender, the owner and a second admin profile."""
    cfg = load_config(EXAMPLE)
    owner = cfg.profiles[ADMIN]
    profiles: dict[str, Profile] = {
        ADMIN: owner,
        OTHER_PROFILE: owner.model_copy(update={"name": "Segundo", "whatsapp": "51977777777"}),
        SENDER: cfg.profiles[SENDER],
    }
    return TeachSetup(profiles=profiles, resolver=FakeResolver())


def _reading(
    *,
    printed_url: str | None = None,
    document_url: str | None = None,
) -> Reading:
    """Return a minimal reading for ISSUER_RUC with the URLs BR-F14 may offer."""
    fields: dict[str, Field] = {
        "razon_social": Field(RAZON_SOCIAL, "qr", 1.0),
        "ruc_emisor": Field(str(ISSUER_RUC), "qr", 1.0),
    }
    return Reading(fields=fields, document_url=document_url, printed_url=printed_url)


async def _invoice_with_reading(conn: AsyncConnection[Any], profile_id: str, reading: Reading) -> int:
    """Create an invoice for profile_id with ``reading`` and return its id."""
    invoice_id = await create_invoice(conn, profile_id, "whatsapp")
    await save_reading(conn, invoice_id, reading)
    return invoice_id


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


def _offer_buttons(invoice_id: int) -> list[dict[str, str]]:
    """Return the button payload BR-F14's offer must carry (D189)."""
    base = f"u{invoice_id}"
    return [
        {"id": f"{base}:yes", "title": BUTTON_YES},
        {"id": f"{base}:no", "title": BUTTON_NO},
        {"id": f"{base}:dk", "title": BUTTON_DONT_KNOW},
    ]


def _decision_buttons(version: int = 1) -> list[dict[str, str]]:
    """Return the button payload BR-S06's owner alert must carry (D118)."""
    return [
        {"id": f"t{ISSUER_RUC}v{version}:ok", "title": BUTTON_APPROVE},
        {"id": f"t{ISSUER_RUC}v{version}:no", "title": BUTTON_REJECT},
    ]


def _br_f02_text() -> str:
    """Return BR-F02's question for ISSUER_RUC (BR-F02)."""
    return MESSAGES["BR-F02"].format(razon_social=RAZON_SOCIAL, ruc_emisor=ISSUER_RUC)


def _button_press(profile_id: str, button_id: str) -> InboundMessage:
    """Build a canonical button press from ``profile_id``."""
    return InboundMessage(
        channel="whatsapp",
        external_id=f"ext-{uuid4().hex}",
        profile_id=profile_id,
        sender_role="owner",
        button_id=button_id,
        received_at=datetime.now(UTC),
    )


async def test_ask_portal_with_printed_url_offers_it_with_three_buttons(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D189, BR-F14: the printed URL wins over the QR one and is offered with three buttons."""
    reading = _reading(printed_url=PRINTED_URL, document_url=OTHER_URL)
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        rows = await _outbox(conn, SENDER)

    expected = MESSAGES["BR-F14"].format(razon_social=RAZON_SOCIAL, url=PRINTED_URL)
    assert rows == [(expected, "BR-F14", _offer_buttons(invoice_id))]


async def test_ask_portal_with_sunat_document_url_asks_br_f02(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D189, BR-F02: a QR link on SUNAT's host is not offered; the sender is asked for the portal."""
    reading = _reading(document_url=SUNAT_URL)
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        rows = await _outbox(conn, SENDER)

    assert rows == [(_br_f02_text(), "BR-F02", [])]


async def test_ask_portal_with_other_document_url_offers_it(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D189, BR-F14: a QR link outside SUNAT's host is offered when nothing is printed."""
    reading = _reading(document_url=OTHER_URL)
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        rows = await _outbox(conn, SENDER)

    expected = MESSAGES["BR-F14"].format(razon_social=RAZON_SOCIAL, url=OTHER_URL)
    assert rows == [(expected, "BR-F14", _offer_buttons(invoice_id))]


async def test_ask_portal_with_no_url_asks_br_f02(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D189, BR-F02: with no URL to offer the sender is asked for the portal."""
    reading = _reading()
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        rows = await _outbox(conn, SENDER)

    assert rows == [(_br_f02_text(), "BR-F02", [])]


async def test_yes_teaches_the_offered_url_and_alerts_the_owner(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D189, BR-F14: [Sí] proposes the offered URL and sends BR-S06's owner alert."""
    setup = _setup()
    reading = _reading(printed_url=PRINTED_URL)
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        await handle_press(conn, _button_press(SENDER, f"u{invoice_id}:yes"), setup)
        link = await taught_links.get(conn, ISSUER_RUC)
        sender_rows = await _outbox(conn, SENDER)
        owner_rows = await _outbox(conn, ADMIN)

    owner_text = MESSAGES["BR-S06_owner"].format(
        sender="Asistente",
        razon_social=RAZON_SOCIAL,
        ruc_emisor=ISSUER_RUC,
        url=PRINTED_URL,
    )
    assert link == TaughtLink(ISSUER_RUC, PRINTED_URL, SENDER, "proposed")
    assert sender_rows[-1] == (MESSAGES["BR-S06"], "BR-S06", [])
    assert owner_rows == [(owner_text, "BR-S06", _decision_buttons())]


async def test_no_asks_br_f02_for_the_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D189, BR-F14: [No] asks BR-F02 for that invoice and teaches no link."""
    setup = _setup()
    reading = _reading(printed_url=PRINTED_URL)
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        await handle_press(conn, _button_press(SENDER, f"u{invoice_id}:no"), setup)
        link = await taught_links.get(conn, ISSUER_RUC)
        rows = await _outbox(conn, SENDER)

    assert link is None
    assert rows[-1] == (_br_f02_text(), "BR-F02", [])


async def test_dont_know_answers_br_f02_dont_know(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D189, BR-F14: [No sé] answers BR-F02_dont_know and teaches no link."""
    setup = _setup()
    reading = _reading(printed_url=PRINTED_URL)
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        await handle_press(conn, _button_press(SENDER, f"u{invoice_id}:dk"), setup)
        link = await taught_links.get(conn, ISSUER_RUC)
        rows = await _outbox(conn, SENDER)

    assert link is None
    assert rows[-1] == (MESSAGES["BR-F02_dont_know"], "BR-F02_dont_know", [])


async def test_press_from_another_profile_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D189, BR-F14: a press for someone else's invoice answers nothing and teaches nothing."""
    setup = _setup()
    reading = _reading(printed_url=PRINTED_URL)
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        before = await _outbox(conn, SENDER)
        await handle_press(conn, _button_press(OTHER_PROFILE, f"u{invoice_id}:yes"), setup)
        after = await _outbox(conn, SENDER)
        link = await taught_links.get(conn, ISSUER_RUC)
        owner_rows = await _outbox(conn, ADMIN)

    assert after == before
    assert link is None
    assert owner_rows == []


async def test_press_after_a_newer_question_is_stale(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D189, BR-S02: a press whose BR-F14 is no longer the newest question answers BR-S02 only."""
    setup = _setup()
    reading = _reading(printed_url=PRINTED_URL)
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        await queue_message(
            conn,
            Addressee("whatsapp", SENDER),
            "¿Confirmas el flujo?",
            invoice_id=invoice_id,
            rule_id="BR-R03",
        )
        before = await _outbox(conn, SENDER)
        await handle_press(conn, _button_press(SENDER, f"u{invoice_id}:yes"), setup)
        after = await _outbox(conn, SENDER)
        link = await taught_links.get(conn, ISSUER_RUC)
        owner_rows = await _outbox(conn, ADMIN)
        state = await _state(conn, invoice_id)

    stale = MESSAGES["BR-S02"].format(id=invoice_id, state_desc=state_desc(state))
    assert after == [*before, (stale, "BR-S02", [])]
    assert link is None
    assert owner_rows == []


async def test_yes_still_answers_after_another_invoices_br_f13(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D189, BR-F14: a BR-F13 notice for another invoice sent after the offer does not make [Sí] stale."""
    setup = _setup()
    reading = _reading(printed_url=PRINTED_URL)
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        other_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        await queue_message(
            conn, Addressee("whatsapp", SENDER), "BR-F13 docs needed", invoice_id=other_id, rule_id="BR-F13"
        )
        await handle_press(conn, _button_press(SENDER, f"u{invoice_id}:yes"), setup)
        link = await taught_links.get(conn, ISSUER_RUC)

    assert link == TaughtLink(ISSUER_RUC, PRINTED_URL, SENDER, "proposed")


async def _state(conn: AsyncConnection[Any], invoice_id: int) -> str:
    """Return the invoice's state."""
    cur = await conn.execute("SELECT state FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0])
