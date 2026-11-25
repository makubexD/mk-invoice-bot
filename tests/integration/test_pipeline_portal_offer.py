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
from invoicebot.net.taught_url import MAX_TAUGHT_URL_CHARS
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.portal_offer import answer, ask_portal, handle_press, offer_candidate
from invoicebot.pipeline.questions import yes_press_suffix
from invoicebot.pipeline.teach import TeachSetup
from invoicebot.rules.messages_es import (
    BUTTON_APPROVE,
    BUTTON_DONT_KNOW,
    BUTTON_FIX,
    BUTTON_REJECT,
    BUTTON_THIS_ONE,
    BUTTON_WRITE_PORTAL,
    MESSAGES,
    state_desc,
)
from invoicebot.store import portal_candidates, taught_links
from invoicebot.store.invoices import create_invoice, save_reading, set_state
from invoicebot.store.taught_links import TaughtLink

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
#: From tests/synthetic_rucs.txt; no real RUC may appear here.
ISSUER_RUC = 20000000001
#: A second synthetic RUC that owns an approved link in one test.
OTHER_RUC = 20000000010
PUBLIC_ADDRESS = "93.184.216.34"
#: A private address the request guard refuses.
PRIVATE_ADDRESS = "10.0.0.1"
#: The URL printed on the ticket, which BR-F14 offers first (D189).
PRINTED_URL = "https://portal.example.pe/consulta"
#: A QR link on a host `qr_url` may not fetch, so BR-F14 may offer it (D189).
OTHER_URL = "https://otro.example.pe/comprobante"
#: SUNAT's generic query page, which BR-F14 never offers (D189).
SUNAT_URL = "https://e-consulta.sunat.gob.pe/cl-ti-itmrconsruc/consulta"
RAZON_SOCIAL = "Tienda Example"
#: The strip OCR read under ``CONSULT… EN``, garbled as a scan prints it (D191).
URL_STRIP = "hitps.//sy.example pe/tiemda"
STRIP_RAZON_SOCIAL = "TIENDA SOL S.A.C."
#: What the strip repairs to with no approved host close enough (D191).
REPAIRED_URL = "https://sy.example.pe/tienda"
#: The approved host ``sy.example.pe`` repairs to; it belongs to another RUC (D191).
APPROVED_HOST_URL = "https://sv.example.pe/tienda"
#: A URL BR-F14 would offer if it recomputed instead of using the stored candidate.
CHANGED_URL = "https://otro.example.pe/otro-comprobante"
#: The valid link a sender types over BR-F14 as the correction (D192).
TYPED_URL = "https://b.example.pe/portal"
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


class PrivateResolver(Resolver):
    """A ``Resolver`` that answers a private address, which the request guard refuses."""

    async def resolve(self, host: str) -> list[str]:
        """Return the fixed private address."""
        return [PRIVATE_ADDRESS]


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def clean_offer_tables(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> AsyncIterator[None]:
    """Start each test from empty taught links and outbox rows."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM taught_links")
        await conn.execute("DELETE FROM outbox")
    yield


def _setup(resolver: Resolver | None = None) -> TeachSetup:
    """Return a setup with the sender, the owner and a second admin profile."""
    cfg = load_config(EXAMPLE)
    owner = cfg.profiles[ADMIN]
    profiles: dict[str, Profile] = {
        ADMIN: owner,
        OTHER_PROFILE: owner.model_copy(update={"name": "Segundo", "whatsapp": "51977777777"}),
        SENDER: cfg.profiles[SENDER],
    }
    return TeachSetup(profiles=profiles, resolver=FakeResolver() if resolver is None else resolver)


def _reading(
    *,
    printed_url: str | None = None,
    document_url: str | None = None,
    url_strip: str | None = None,
    razon_social: str = RAZON_SOCIAL,
) -> Reading:
    """Return a minimal reading for ISSUER_RUC with the URLs BR-F14 may offer."""
    fields: dict[str, Field] = {
        "razon_social": Field(razon_social, "qr", 1.0),
        "ruc_emisor": Field(str(ISSUER_RUC), "qr", 1.0),
    }
    return Reading(
        fields=fields,
        document_url=document_url,
        printed_url=printed_url,
        url_strip=url_strip,
    )


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


def _yes_id(invoice_id: int) -> str:
    """Return the [Sí] press id of the offer of ``PRINTED_URL``."""
    return f"u{invoice_id}:{yes_press_suffix(PRINTED_URL)}"


def _offer_buttons(invoice_id: int, url: str) -> list[dict[str, str]]:
    """Return the button payload BR-F14's offer must carry (D191)."""
    base = f"u{invoice_id}"
    return [
        {"id": f"{base}:{yes_press_suffix(url)}", "title": BUTTON_THIS_ONE},
        {"id": f"{base}:fix", "title": BUTTON_FIX},
        {"id": f"{base}:dk", "title": BUTTON_DONT_KNOW},
    ]


def _ask_portal_buttons(invoice_id: int) -> list[dict[str, str]]:
    """Return the button payload BR-F02's question must carry (D191)."""
    base = f"u{invoice_id}"
    return [
        {"id": f"{base}:fix", "title": BUTTON_WRITE_PORTAL},
        {"id": f"{base}:dk", "title": BUTTON_DONT_KNOW},
    ]


def _br_f15_buttons(invoice_id: int) -> list[dict[str, str]]:
    """Return the button payload BR-F15's question must carry (D191)."""
    return [{"id": f"u{invoice_id}:dk", "title": BUTTON_DONT_KNOW}]


def _decision_buttons(version: int = 1) -> list[dict[str, str]]:
    """Return the button payload BR-S06's owner alert must carry (D118)."""
    return [
        {"id": f"t{ISSUER_RUC}v{version}:ok", "title": BUTTON_APPROVE},
        {"id": f"t{ISSUER_RUC}v{version}:no", "title": BUTTON_REJECT},
    ]


def _br_f02_text() -> str:
    """Return BR-F02's question for ISSUER_RUC (BR-F02)."""
    return MESSAGES["BR-F02"].format(razon_social=RAZON_SOCIAL, ruc_emisor=ISSUER_RUC)


def _br_f15_text() -> str:
    """Return BR-F15's question for ISSUER_RUC (BR-F15)."""
    return MESSAGES["BR-F15"].format(razon_social=RAZON_SOCIAL)


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


def _text_message(profile_id: str, text: str) -> InboundMessage:
    """Build a canonical free-text message from ``profile_id``."""
    return InboundMessage(
        channel="whatsapp",
        external_id=f"ext-{uuid4().hex}",
        profile_id=profile_id,
        sender_role="owner",
        text=text,
        received_at=datetime.now(UTC),
    )


async def test_ask_portal_with_printed_url_offers_it_with_three_buttons(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D191, BR-F14: the printed URL wins over the QR one and is offered with three buttons."""
    reading = _reading(printed_url=PRINTED_URL, document_url=OTHER_URL)
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        rows = await _outbox(conn, SENDER)

    expected = MESSAGES["BR-F14"].format(razon_social=RAZON_SOCIAL, url=PRINTED_URL)
    assert rows == [(expected, "BR-F14", _offer_buttons(invoice_id, PRINTED_URL))]


async def test_ask_portal_with_sunat_document_url_asks_br_f02(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D191, BR-F02: a QR link on SUNAT's host is not offered; the sender is asked for the portal."""
    reading = _reading(document_url=SUNAT_URL)
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        rows = await _outbox(conn, SENDER)

    assert rows == [(_br_f02_text(), "BR-F02", _ask_portal_buttons(invoice_id))]


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
    assert rows == [(expected, "BR-F14", _offer_buttons(invoice_id, OTHER_URL))]


async def test_ask_portal_with_no_url_asks_br_f02(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D191, BR-F02: with no URL to offer the sender is asked for the portal with [✏️ Lo escribo] [No sé]."""
    reading = _reading()
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        rows = await _outbox(conn, SENDER)

    assert rows == [(_br_f02_text(), "BR-F02", _ask_portal_buttons(invoice_id))]


async def test_ask_portal_repairs_the_strip_and_stores_the_candidate(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D191, BR-F14: the garbled strip is repaired from the issuer name and offered, then stored."""
    reading = _reading(url_strip=URL_STRIP, razon_social=STRIP_RAZON_SOCIAL)
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        rows = await _outbox(conn, SENDER)
        candidate = await portal_candidates.get(conn, invoice_id)

    expected = MESSAGES["BR-F14"].format(razon_social=STRIP_RAZON_SOCIAL, url=REPAIRED_URL)
    assert rows == [(expected, "BR-F14", _offer_buttons(invoice_id, REPAIRED_URL))]
    assert candidate == REPAIRED_URL


async def test_ask_portal_repairs_the_strip_against_an_approved_host(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D191, BR-F14: an approved host close to the strip's host replaces it in the offer."""
    reading = _reading(url_strip=URL_STRIP, razon_social=STRIP_RAZON_SOCIAL)
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO taught_links (ruc_emisor, url, profile_id, state, approved_at)
            VALUES (%s, %s, %s, 'approved', now())
            """,
            (OTHER_RUC, APPROVED_HOST_URL, ADMIN),
        )
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        rows = await _outbox(conn, SENDER)
        candidate = await portal_candidates.get(conn, invoice_id)

    expected = MESSAGES["BR-F14"].format(razon_social=STRIP_RAZON_SOCIAL, url=APPROVED_HOST_URL)
    assert rows == [(expected, "BR-F14", _offer_buttons(invoice_id, APPROVED_HOST_URL))]
    assert candidate == APPROVED_HOST_URL


async def test_ask_portal_with_a_strip_that_repairs_to_nothing_asks_br_f02(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D191, BR-F02: a strip that is no portal address, with no QR link, asks BR-F02."""
    reading = _reading(url_strip="no hay nada")
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        rows = await _outbox(conn, SENDER)
        candidate = await portal_candidates.get(conn, invoice_id)

    assert rows == [(_br_f02_text(), "BR-F02", _ask_portal_buttons(invoice_id))]
    assert candidate is None


async def test_ask_portal_with_an_unrepairable_strip_offers_the_qr_link(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D191, BR-F14: a strip that repairs to nothing does not hide the QR's own link."""
    reading = _reading(document_url=CHANGED_URL, url_strip="no hay nada")
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        candidate = await portal_candidates.get(conn, invoice_id)

    assert candidate == CHANGED_URL


async def test_ask_portal_printed_url_wins_over_the_strip(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D191, BR-F14: the printed URL is offered and stored even when the strip reads a portal."""
    reading = _reading(printed_url=PRINTED_URL, url_strip=URL_STRIP, razon_social=STRIP_RAZON_SOCIAL)
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        rows = await _outbox(conn, SENDER)
        candidate = await portal_candidates.get(conn, invoice_id)

    expected = MESSAGES["BR-F14"].format(razon_social=STRIP_RAZON_SOCIAL, url=PRINTED_URL)
    assert rows == [(expected, "BR-F14", _offer_buttons(invoice_id, PRINTED_URL))]
    assert candidate == PRINTED_URL


async def test_ask_portal_with_a_qr_link_too_long_to_teach_asks_br_f02(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D191, BR-F02: a QR link longer than a teachable URL cannot be the candidate, so BR-F02 is asked."""
    prefix = "https://otro.example.pe/"
    reading = _reading(document_url=prefix + "a" * (MAX_TAUGHT_URL_CHARS + 1 - len(prefix)))
    assert len(reading.document_url or "") == MAX_TAUGHT_URL_CHARS + 1
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        rows = await _outbox(conn, SENDER)
        candidate = await portal_candidates.get(conn, invoice_id)

    assert rows == [(_br_f02_text(), "BR-F02", _ask_portal_buttons(invoice_id))]
    assert candidate is None


async def test_yes_teaches_the_offered_url_and_alerts_the_owner(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D189, BR-F14: [Sí] proposes the offered URL and sends BR-S06's owner alert."""
    setup = _setup()
    reading = _reading(printed_url=PRINTED_URL)
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        await handle_press(conn, _button_press(SENDER, _yes_id(invoice_id)), setup)
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


async def test_yes_teaches_the_stored_candidate_after_the_reading_changes(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D191, BR-S06: [Sí] proposes the URL BR-F14 stored, not one recomputed from the reading."""
    setup = _setup()
    reading = _reading(printed_url=PRINTED_URL)
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        await save_reading(conn, invoice_id, _reading(document_url=CHANGED_URL))
        await handle_press(conn, _button_press(SENDER, _yes_id(invoice_id)), setup)
        link = await taught_links.get(conn, ISSUER_RUC)
        owner_rows = await _outbox(conn, ADMIN)

    owner_text = MESSAGES["BR-S06_owner"].format(
        sender="Asistente",
        razon_social=RAZON_SOCIAL,
        ruc_emisor=ISSUER_RUC,
        url=PRINTED_URL,
    )
    assert link == TaughtLink(ISSUER_RUC, PRINTED_URL, SENDER, "proposed")
    assert owner_rows == [(owner_text, "BR-S06", _decision_buttons())]


async def test_yes_with_a_private_resolver_asks_br_f15_retry_and_teaches_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D191, BR-F15: [Sí] whose stored candidate resolves private asks BR-F15 again and teaches nothing."""
    setup = _setup(PrivateResolver())
    reading = _reading(printed_url=PRINTED_URL)
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        await handle_press(conn, _button_press(SENDER, _yes_id(invoice_id)), setup)
        link = await taught_links.get(conn, ISSUER_RUC)
        sender_rows = await _outbox(conn, SENDER)
        owner_rows = await _outbox(conn, ADMIN)

    assert link is None
    assert sender_rows[-1] == (MESSAGES["BR-F15_retry"], "BR-F15", _br_f15_buttons(invoice_id))
    assert owner_rows == []


async def test_fix_on_br_f14_asks_br_f15(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D191, BR-F15: [✏️ Corregir] on the newest BR-F14 asks BR-F15 with [No sé]."""
    setup = _setup()
    reading = _reading(printed_url=PRINTED_URL)
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        await handle_press(conn, _button_press(SENDER, f"u{invoice_id}:fix"), setup)
        rows = await _outbox(conn, SENDER)

    br_f15_rows = [row for row in rows if row[1] == "BR-F15"]
    assert br_f15_rows == [(_br_f15_text(), "BR-F15", _br_f15_buttons(invoice_id))]


async def test_write_portal_on_br_f02_asks_br_f15(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D191, BR-F15: [✏️ Lo escribo] on the newest BR-F02 asks BR-F15 with [No sé]."""
    setup = _setup()
    reading = _reading()
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        await handle_press(conn, _button_press(SENDER, f"u{invoice_id}:fix"), setup)
        rows = await _outbox(conn, SENDER)

    br_f15_rows = [row for row in rows if row[1] == "BR-F15"]
    assert br_f15_rows == [(_br_f15_text(), "BR-F15", _br_f15_buttons(invoice_id))]


async def test_fix_on_an_older_br_f14_is_stale(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D191, BR-S02: [✏️ Corregir] whose BR-F14 is no longer the newest answers BR-S02 and asks nothing."""
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
        await handle_press(conn, _button_press(SENDER, f"u{invoice_id}:fix"), setup)
        after = await _outbox(conn, SENDER)
        state = await _state(conn, invoice_id)

    stale = MESSAGES["BR-S02"].format(id=invoice_id, state_desc=state_desc(state))
    assert after == [*before, (stale, "BR-S02", [])]


async def test_dont_know_while_br_f15_is_newest_answers_br_f02_dont_know(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D191, BR-F15: [No sé] on BR-F15's question answers BR-F02_dont_know."""
    setup = _setup()
    reading = _reading()
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await set_state(conn, invoice_id, "FETCHING_DOCS")
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        await handle_press(conn, _button_press(SENDER, f"u{invoice_id}:fix"), setup)
        await handle_press(conn, _button_press(SENDER, f"u{invoice_id}:dk"), setup)
        rows = await _outbox(conn, SENDER)

    assert rows[-1] == (MESSAGES["BR-F02_dont_know"], "BR-F02_dont_know", [])


async def test_old_no_press_is_logged_and_queues_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D191, BR-F14: a press with the removed ``:no`` action is logged and changes nothing."""
    setup = _setup()
    reading = _reading(printed_url=PRINTED_URL)
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        before = await _outbox(conn, SENDER)
        await handle_press(conn, _button_press(SENDER, f"u{invoice_id}:no"), setup)
        after = await _outbox(conn, SENDER)
        link = await taught_links.get(conn, ISSUER_RUC)

    assert after == before
    assert link is None


async def test_dont_know_answers_br_f02_dont_know_manual(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D189, BR-F14: [No sé] answers BR-F02_dont_know_manual and teaches no link."""
    setup = _setup()
    reading = _reading(printed_url=PRINTED_URL)
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        await handle_press(conn, _button_press(SENDER, f"u{invoice_id}:dk"), setup)
        link = await taught_links.get(conn, ISSUER_RUC)
        rows = await _outbox(conn, SENDER)

    assert link is None
    assert rows[-1] == (MESSAGES["BR-F02_dont_know_manual"], "BR-F02_dont_know", [])


async def test_dont_know_after_br_f02_answers_br_f02_dont_know_manual(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D190, BR-F02: [No sé] on BR-F02's question answers BR-F02_dont_know_manual and teaches no link."""
    setup = _setup()
    reading = _reading()
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        await handle_press(conn, _button_press(SENDER, f"u{invoice_id}:dk"), setup)
        link = await taught_links.get(conn, ISSUER_RUC)
        rows = await _outbox(conn, SENDER)

    assert link is None
    assert rows[-1] == (MESSAGES["BR-F02_dont_know_manual"], "BR-F02_dont_know", [])


async def test_dont_know_on_fetching_docs_answers_br_f02_dont_know(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D190, BR-F02: [No sé] on a FETCHING_DOCS invoice answers BR-F02_dont_know."""
    setup = _setup()
    reading = _reading()
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await set_state(conn, invoice_id, "FETCHING_DOCS")
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        await handle_press(conn, _button_press(SENDER, f"u{invoice_id}:dk"), setup)
        link = await taught_links.get(conn, ISSUER_RUC)
        rows = await _outbox(conn, SENDER)

    assert link is None
    assert rows[-1] == (MESSAGES["BR-F02_dont_know"], "BR-F02_dont_know", [])


async def test_dont_know_on_manual_required_answers_br_f02_dont_know_manual(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D190, BR-F02: [No sé] on a MANUAL_REQUIRED invoice answers BR-F02_dont_know_manual."""
    setup = _setup()
    reading = _reading()
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await set_state(conn, invoice_id, "MANUAL_REQUIRED", rule_id="BR-F07")
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        await handle_press(conn, _button_press(SENDER, f"u{invoice_id}:dk"), setup)
        link = await taught_links.get(conn, ISSUER_RUC)
        rows = await _outbox(conn, SENDER)

    assert link is None
    assert rows[-1] == (MESSAGES["BR-F02_dont_know_manual"], "BR-F02_dont_know", [])


async def test_yes_after_br_f02_is_stale_and_teaches_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D190, BR-F02: a forged [Sí] on BR-F02's question answers BR-S02 and teaches no link."""
    setup = _setup()
    reading = _reading()
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        before = await _outbox(conn, SENDER)
        await handle_press(conn, _button_press(SENDER, _yes_id(invoice_id)), setup)
        after = await _outbox(conn, SENDER)
        link = await taught_links.get(conn, ISSUER_RUC)
        owner_rows = await _outbox(conn, ADMIN)
        state = await _state(conn, invoice_id)

    stale = MESSAGES["BR-S02"].format(id=invoice_id, state_desc=state_desc(state))
    assert after == [*before, (stale, "BR-S02", [])]
    assert link is None
    assert owner_rows == []


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
        await handle_press(conn, _button_press(OTHER_PROFILE, _yes_id(invoice_id)), setup)
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
        await handle_press(conn, _button_press(SENDER, _yes_id(invoice_id)), setup)
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
        await handle_press(conn, _button_press(SENDER, _yes_id(invoice_id)), setup)
        link = await taught_links.get(conn, ISSUER_RUC)

    assert link == TaughtLink(ISSUER_RUC, PRINTED_URL, SENDER, "proposed")


async def _state(conn: AsyncConnection[Any], invoice_id: int) -> str:
    """Return the invoice's state."""
    cur = await conn.execute("SELECT state FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0])


async def test_yes_on_an_older_offer_is_stale_and_teaches_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D191, BR-S02: [Sí] on an earlier BR-F14 never teaches the URL a later BR-F14 offered."""
    reading = _reading(printed_url=PRINTED_URL)
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, _reading(printed_url=CHANGED_URL))
        await handle_press(conn, _button_press(SENDER, _yes_id(invoice_id)), _setup())
        link = await taught_links.get(conn, ISSUER_RUC)
        rows = await _outbox(conn, SENDER)

    assert link is None
    assert rows[-1][1] == "BR-S02"


@pytest.mark.parametrize("suffix", [yes_press_suffix(PRINTED_URL), "fix", "dk"])
async def test_press_after_yes_is_stale(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], suffix: str
) -> None:
    """D191, BR-S02: once [Sí] proposed the URL, no button of that BR-F14 acts again."""
    reading = _reading(printed_url=PRINTED_URL)
    yes = f"u{{}}:{yes_press_suffix(PRINTED_URL)}"
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        await handle_press(conn, _button_press(SENDER, yes.format(invoice_id)), _setup())
        await handle_press(conn, _button_press(SENDER, f"u{invoice_id}:{suffix}"), _setup())
        rows = await _outbox(conn, SENDER)
        owner_rows = await _outbox(conn, ADMIN)

    assert [row[1] for row in rows[-2:]] == ["BR-S06", "BR-S02"]
    assert len(owner_rows) == 1


@pytest.mark.parametrize(
    "url", ["https://xn--prtal-9ta.pe/a", "https://portal.example.pe:8443/a", "https://portal.example.pe/a b"]
)
async def test_ask_portal_with_an_unteachable_qr_link_asks_br_f02(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], url: str
) -> None:
    """D191, BR-F14: a QR link that could not be taught is never offered."""
    reading = _reading(document_url=url)
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await ask_portal(conn, Addressee("whatsapp", SENDER), invoice_id, reading)
        rows = await _outbox(conn, SENDER)

    assert [row[1] for row in rows] == ["BR-F02"]


async def test_typing_a_url_over_br_f14_offers_it_and_the_old_yes_is_stale(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-F14, D192: a typed link replaces the offer, so the earlier [Sí] only answers BR-S02."""
    setup = _setup()
    reading = _reading()
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await offer_candidate(conn, Addressee("whatsapp", SENDER), invoice_id, reading, PRINTED_URL)
        handled = await answer(conn, setup, _text_message(SENDER, TYPED_URL))
        rows = await _outbox(conn, SENDER)
        candidate = await portal_candidates.get(conn, invoice_id)
        await handle_press(conn, _button_press(SENDER, _yes_id(invoice_id)), setup)
        after = await _outbox(conn, SENDER)
        link = await taught_links.get(conn, ISSUER_RUC)
        owner_rows = await _outbox(conn, ADMIN)
        state = await _state(conn, invoice_id)

    expected = MESSAGES["BR-F14"].format(razon_social=RAZON_SOCIAL, url=TYPED_URL)
    stale = MESSAGES["BR-S02"].format(id=invoice_id, state_desc=state_desc(state))
    assert handled is True
    assert rows[-1] == (expected, "BR-F14", _offer_buttons(invoice_id, TYPED_URL))
    assert candidate == TYPED_URL
    assert after == [*rows, (stale, "BR-S02", [])]
    assert link is None
    assert owner_rows == []


async def test_yes_after_typing_a_url_teaches_the_typed_url(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-F14, D192: [Sí] on the replaced offer proposes the URL the sender typed."""
    setup = _setup()
    reading = _reading()
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await offer_candidate(conn, Addressee("whatsapp", SENDER), invoice_id, reading, PRINTED_URL)
        await answer(conn, setup, _text_message(SENDER, TYPED_URL))
        await handle_press(conn, _button_press(SENDER, f"u{invoice_id}:{yes_press_suffix(TYPED_URL)}"), setup)
        link = await taught_links.get(conn, ISSUER_RUC)

    assert link == TaughtLink(ISSUER_RUC, TYPED_URL, SENDER, "proposed")


async def test_typed_url_that_resolves_private_asks_br_f15_retry(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-F14, D192: a typed link the guard refuses asks BR-F15 again and leaves the candidate."""
    setup = _setup(PrivateResolver())
    reading = _reading()
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await offer_candidate(conn, Addressee("whatsapp", SENDER), invoice_id, reading, PRINTED_URL)
        handled = await answer(conn, setup, _text_message(SENDER, TYPED_URL))
        rows = await _outbox(conn, SENDER)
        candidate = await portal_candidates.get(conn, invoice_id)

    assert handled is True
    assert rows[-1] == (MESSAGES["BR-F15_retry"], "BR-F15", _br_f15_buttons(invoice_id))
    assert candidate == PRINTED_URL


async def test_text_that_is_not_a_url_leaves_br_f14_open(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-F14, D192: other text is not an answer, so nothing is queued and the candidate stays."""
    setup = _setup()
    reading = _reading()
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await offer_candidate(conn, Addressee("whatsapp", SENDER), invoice_id, reading, PRINTED_URL)
        before = await _outbox(conn, SENDER)
        handled = await answer(conn, setup, _text_message(SENDER, "hola"))
        after = await _outbox(conn, SENDER)
        candidate = await portal_candidates.get(conn, invoice_id)

    assert handled is False
    assert after == before
    assert candidate == PRINTED_URL


async def test_typed_url_on_a_done_invoice_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-F14, D192: a settled invoice takes no correction, so nothing is queued."""
    setup = _setup()
    reading = _reading()
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await offer_candidate(conn, Addressee("whatsapp", SENDER), invoice_id, reading, PRINTED_URL)
        await set_state(conn, invoice_id, "DONE")
        before = await _outbox(conn, SENDER)
        handled = await answer(conn, setup, _text_message(SENDER, TYPED_URL))
        after = await _outbox(conn, SENDER)
        candidate = await portal_candidates.get(conn, invoice_id)

    assert handled is False
    assert after == before
    assert candidate == PRINTED_URL


async def test_typed_url_after_the_candidate_was_deleted_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-F14, D192: with the candidate consumed, the open BR-F14 takes no correction."""
    setup = _setup()
    reading = _reading()
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, reading)
        await offer_candidate(conn, Addressee("whatsapp", SENDER), invoice_id, reading, PRINTED_URL)
        await portal_candidates.delete(conn, invoice_id)
        before = await _outbox(conn, SENDER)
        handled = await answer(conn, setup, _text_message(SENDER, TYPED_URL))
        after = await _outbox(conn, SENDER)
        candidate = await portal_candidates.get(conn, invoice_id)

    assert handled is False
    assert after == before
    assert candidate is None
