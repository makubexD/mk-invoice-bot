"""Integration tests for the ``portal_check`` step that guesses and offers a portal (BR-F14, D192)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import InboundMessage
from invoicebot.config.loader import load_config
from invoicebot.extract.fields import Field, Reading
from invoicebot.net.guard import Resolver
from invoicebot.pipeline.portal_check import make_portal_check_handler
from invoicebot.pipeline.portal_guess import candidate_urls
from invoicebot.pipeline.portal_offer import handle_press
from invoicebot.pipeline.questions import yes_press_suffix
from invoicebot.pipeline.teach import TeachSetup
from invoicebot.queue.jobs import Job
from invoicebot.rules.messages_es import (
    BUTTON_DONT_KNOW,
    BUTTON_FIX,
    BUTTON_THIS_ONE,
    MESSAGES,
)
from invoicebot.states import CANCELLED, DONE, FETCHING_DOCS
from invoicebot.store import portal_candidates, taught_links
from invoicebot.store.invoices import create_invoice, save_reading, set_state
from invoicebot.store.taught_links import TaughtLink

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
#: From tests/synthetic_rucs.txt; no real RUC may appear here.
ISSUER_RUC = 20000000001
PUBLIC_ADDRESS = "93.184.216.34"
#: A synthetic portal the strip reads, without a scheme as OCR prints it.
URL_STRIP = "portal.example.pe/consulta"
#: What the strip's candidates start with; the printed URL is the same address (D192).
PRINTED_URL = "https://portal.example.pe/consulta"
#: A strip with no portal address in it, so :func:`candidate_urls` returns nothing (D192).
UNREADABLE_STRIP = "consulta en la tienda"
#: A QR link BR-F14 may offer when the strip is no address (D192).
QR_URL = "https://otro.example.pe/comprobante"
RAZON_SOCIAL = "Tienda Example"
SENDER = "asistente"


class FakeResolver(Resolver):
    """A ``Resolver`` that answers the public address, whatever the host is."""

    async def resolve(self, host: str) -> list[str]:
        """Return the fixed public address."""
        return [PUBLIC_ADDRESS]


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def clean_portal_check_tables(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> AsyncIterator[None]:
    """Start each test from empty taught links, candidates and outbox rows."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM taught_links")
        await conn.execute("DELETE FROM portal_candidates")
        await conn.execute("DELETE FROM outbox")
    yield


def _reading(
    *,
    printed_url: str | None = None,
    document_url: str | None = None,
    url_strip: str | None = None,
    razon_social: str = RAZON_SOCIAL,
    ruc_emisor: int | None = ISSUER_RUC,
) -> Reading:
    """Return a minimal reading for the synthetic issuer with the URLs BR-F14 may offer."""
    fields: dict[str, Field] = {"razon_social": Field(razon_social, "qr", 1.0)}
    if ruc_emisor is not None:
        fields["ruc_emisor"] = Field(str(ruc_emisor), "qr", 1.0)
    return Reading(
        fields=fields,
        document_url=document_url,
        printed_url=printed_url,
        url_strip=url_strip,
    )


async def _invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    reading: Reading | None,
    *,
    state: str = FETCHING_DOCS,
    rule_id: str | None = None,
) -> int:
    """Create and commit an invoice in ``state`` with an optional reading."""
    async with db_pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, SENDER, "whatsapp")
        if reading is not None:
            await save_reading(conn, invoice_id, reading)
        await set_state(conn, invoice_id, state, rule_id=rule_id)
    return invoice_id


def _job(invoice_id: int) -> Job:
    """Build the leased job row the check handler receives."""
    return Job(
        id=1,
        invoice_id=invoice_id,
        step="portal_check",
        attempts=0,
        crashes=0,
        lease_until=datetime.now(UTC),
    )


async def _run(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    invoice_id: int,
    transport: httpx.MockTransport,
) -> None:
    """Run the check handler on ``invoice_id`` with the given transport."""
    async with httpx.AsyncClient(transport=transport) as http:
        await make_portal_check_handler(db_pool, http)(_job(invoice_id))


def _answering(*answer_urls: str) -> tuple[httpx.MockTransport, list[str]]:
    """Return a transport that answers 2xx only for ``answer_urls`` and records each URL asked."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200 if str(request.url) in answer_urls else 404)

    return httpx.MockTransport(handler), requested


def _refusing() -> tuple[httpx.MockTransport, list[str]]:
    """Return a transport that fails every request with ConnectError and records the URLs."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.MockTransport(handler), requested


def _cancelling(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    invoice_id: int,
) -> tuple[httpx.MockTransport, list[str]]:
    """Return a transport that cancels ``invoice_id`` in a committed connection, then answers."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        async with db_pool.connection() as conn, conn.transaction():
            await set_state(conn, invoice_id, CANCELLED, rule_id="BR-D06")
        return httpx.Response(200)

    return httpx.MockTransport(handler), requested


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


def _offer_buttons(invoice_id: int, url: str) -> list[dict[str, str]]:
    """Return the button payload BR-F14's offer must carry (D191)."""
    base = f"u{invoice_id}"
    return [
        {"id": f"{base}:{yes_press_suffix(url)}", "title": BUTTON_THIS_ONE},
        {"id": f"{base}:fix", "title": BUTTON_FIX},
        {"id": f"{base}:dk", "title": BUTTON_DONT_KNOW},
    ]


def _unread_buttons(invoice_id: int) -> list[dict[str, str]]:
    """Return the button payload BR-F15's unread-strip question must carry (D192)."""
    return [{"id": f"u{invoice_id}:dk", "title": BUTTON_DONT_KNOW}]


def _setup() -> TeachSetup:
    """Return a setup with the example config's profiles and a public-address resolver."""
    return TeachSetup(profiles=dict(load_config(EXAMPLE).profiles), resolver=FakeResolver())


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


def _candidates(text: str = URL_STRIP) -> tuple[str, ...]:
    """Return the candidates the handler derives with no approved host."""
    return candidate_urls(text, RAZON_SOCIAL, frozenset())


async def test_second_candidate_answering_is_the_one_offered(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-F14, D192: when the first guess does not answer, the second is offered and stored."""
    candidates = _candidates()
    assert len(candidates) >= 2
    transport, requested = _answering(candidates[1])
    invoice_id = await _invoice(db_pool, _reading(url_strip=URL_STRIP))

    await _run(db_pool, invoice_id, transport)

    async with db_pool.connection() as conn:
        rows = await _outbox(conn, SENDER)
        candidate = await portal_candidates.get(conn, invoice_id)
    expected = MESSAGES["BR-F14"].format(razon_social=RAZON_SOCIAL, url=candidates[1])
    assert requested == [candidates[0], candidates[1]]
    assert rows == [(expected, "BR-F14", _offer_buttons(invoice_id, candidates[1]))]
    assert candidate == candidates[1]


async def test_no_candidate_answering_offers_the_first_as_unverified(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-F14, D192: when no guess answers, the first is offered as unverified with three buttons."""
    candidates = _candidates()
    transport, requested = _answering()
    invoice_id = await _invoice(db_pool, _reading(url_strip=URL_STRIP))

    await _run(db_pool, invoice_id, transport)

    async with db_pool.connection() as conn:
        rows = await _outbox(conn, SENDER)
        candidate = await portal_candidates.get(conn, invoice_id)
    expected = MESSAGES["BR-F14_unverified"].format(razon_social=RAZON_SOCIAL, url=candidates[0])
    assert requested == list(candidates)
    assert rows == [(expected, "BR-F14", _offer_buttons(invoice_id, candidates[0]))]
    assert len(rows[0][2]) == 3
    assert candidate == candidates[0]


async def test_yes_on_the_unverified_offer_proposes_that_url(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-F14, D192: [Sí] on the unverified offer proposes exactly the URL it showed."""
    candidates = _candidates()
    transport, _ = _answering()
    invoice_id = await _invoice(db_pool, _reading(url_strip=URL_STRIP))
    await _run(db_pool, invoice_id, transport)

    async with db_pool.connection() as conn, conn.transaction():
        await handle_press(
            conn,
            _button_press(SENDER, f"u{invoice_id}:{yes_press_suffix(candidates[0])}"),
            _setup(),
        )
        link = await taught_links.get(conn, ISSUER_RUC)

    assert link == TaughtLink(ISSUER_RUC, candidates[0], SENDER, "proposed")


async def test_printed_url_is_tried_before_the_strip(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-F14, D192: the printed URL, not the strip, gives the first candidate tried."""
    transport, requested = _answering(PRINTED_URL)
    invoice_id = await _invoice(db_pool, _reading(printed_url=PRINTED_URL, url_strip=URL_STRIP))

    await _run(db_pool, invoice_id, transport)

    async with db_pool.connection() as conn:
        rows = await _outbox(conn, SENDER)
        candidate = await portal_candidates.get(conn, invoice_id)
    expected = MESSAGES["BR-F14"].format(razon_social=RAZON_SOCIAL, url=PRINTED_URL)
    assert requested[0] == PRINTED_URL
    assert rows == [(expected, "BR-F14", _offer_buttons(invoice_id, PRINTED_URL))]
    assert candidate == PRINTED_URL


@pytest.mark.parametrize(
    "url",
    ["https://portal.example.pe/cpe?ruc=1", "https://portal.example.app/cpe"],
    ids=["query", "other_top_level"],
)
async def test_printed_url_the_repair_loses_is_tried_first_as_printed(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], url: str
) -> None:
    """BR-F14, D192: a printed URL with a query or another top level is tried and offered verbatim."""
    transport, requested = _answering(url)
    invoice_id = await _invoice(db_pool, _reading(printed_url=url))

    await _run(db_pool, invoice_id, transport)

    async with db_pool.connection() as conn:
        rows = await _outbox(conn, SENDER)
    expected = MESSAGES["BR-F14"].format(razon_social=RAZON_SOCIAL, url=url)
    assert requested == [url]
    assert rows == [(expected, "BR-F14", _offer_buttons(invoice_id, url))]


async def test_printed_sunat_url_is_never_tried(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-F14, D192: a printed SUNAT URL sends no request and is not offered."""
    transport, requested = _answering()
    invoice_id = await _invoice(db_pool, _reading(printed_url="https://e-consulta.sunat.gob.pe/x"))

    await _run(db_pool, invoice_id, transport)

    async with db_pool.connection() as conn:
        rows = await _outbox(conn, SENDER)
        candidate = await portal_candidates.get(conn, invoice_id)
    assert requested == []
    assert [row[1] for row in rows] == ["BR-F15"]
    assert candidate is None


async def test_strip_without_a_candidate_asks_br_f15_with_the_strip(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-F14, D192: a strip that is no address and no QR link asks BR-F15 with the strip and [No sé]."""
    transport, requested = _answering()
    invoice_id = await _invoice(db_pool, _reading(url_strip=UNREADABLE_STRIP))

    await _run(db_pool, invoice_id, transport)

    async with db_pool.connection() as conn:
        rows = await _outbox(conn, SENDER)
        candidate = await portal_candidates.get(conn, invoice_id)
    expected = MESSAGES["BR-F15_unread"].format(strip=UNREADABLE_STRIP, razon_social=RAZON_SOCIAL)
    assert requested == []
    assert rows == [(expected, "BR-F15", _unread_buttons(invoice_id))]
    assert candidate is None


async def test_unreadable_strip_with_a_teachable_qr_link_offers_it(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-F14, D192: a strip with no candidate falls back to the QR link without sending a request."""
    transport, requested = _answering()
    invoice_id = await _invoice(db_pool, _reading(url_strip=UNREADABLE_STRIP, document_url=QR_URL))

    await _run(db_pool, invoice_id, transport)

    async with db_pool.connection() as conn:
        rows = await _outbox(conn, SENDER)
        candidate = await portal_candidates.get(conn, invoice_id)
    expected = MESSAGES["BR-F14"].format(razon_social=RAZON_SOCIAL, url=QR_URL)
    assert requested == []
    assert rows == [(expected, "BR-F14", _offer_buttons(invoice_id, QR_URL))]
    assert candidate == QR_URL


async def test_unreadable_strip_without_an_issuer_queues_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-F14, D192: BR-F15 could never be taught without an issuer, so it is not asked."""
    transport, requested = _answering()
    invoice_id = await _invoice(db_pool, _reading(url_strip=UNREADABLE_STRIP, ruc_emisor=None))

    await _run(db_pool, invoice_id, transport)

    async with db_pool.connection() as conn:
        rows = await _outbox(conn, SENDER)
        candidate = await portal_candidates.get(conn, invoice_id)
    assert requested == []
    assert rows == []
    assert candidate is None


@pytest.mark.parametrize(("state", "rule_id"), [(CANCELLED, "BR-D06"), (DONE, None)])
async def test_state_outside_the_two_offers_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    state: str,
    rule_id: str | None,
) -> None:
    """BR-F14, D192: a cancelled or done invoice sends no request and queues nothing."""
    transport, requested = _answering()
    invoice_id = await _invoice(
        db_pool, _reading(printed_url=PRINTED_URL, url_strip=URL_STRIP), state=state, rule_id=rule_id
    )

    await _run(db_pool, invoice_id, transport)

    async with db_pool.connection() as conn:
        rows = await _outbox(conn, SENDER)
        candidate = await portal_candidates.get(conn, invoice_id)
    assert requested == []
    assert rows == []
    assert candidate is None


async def test_cancelled_during_the_request_queues_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-F14, D192: a cancellation committed while the probe waits leaves the invoice untouched."""
    candidates = _candidates()
    invoice_id = await _invoice(db_pool, _reading(url_strip=URL_STRIP))
    transport, requested = _cancelling(db_pool, invoice_id)

    await _run(db_pool, invoice_id, transport)

    async with db_pool.connection() as conn:
        rows = await _outbox(conn, SENDER)
        candidate = await portal_candidates.get(conn, invoice_id)
    assert requested == [candidates[0]]
    assert rows == []
    assert candidate is None


async def test_connection_error_offers_the_first_guess_unverified(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-F14, D192: a transport that always raises leaves the first guess offered and does not raise."""
    candidates = _candidates()
    transport, requested = _refusing()
    invoice_id = await _invoice(db_pool, _reading(url_strip=URL_STRIP))

    await _run(db_pool, invoice_id, transport)

    async with db_pool.connection() as conn:
        rows = await _outbox(conn, SENDER)
        candidate = await portal_candidates.get(conn, invoice_id)
    expected = MESSAGES["BR-F14_unverified"].format(razon_social=RAZON_SOCIAL, url=candidates[0])
    assert requested == list(candidates)
    assert rows == [(expected, "BR-F14", _offer_buttons(invoice_id, candidates[0]))]
    assert candidate == candidates[0]


async def test_invoice_without_a_reading_queues_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-F14, D192: an invoice with no reading sends no request and queues nothing."""
    transport, requested = _answering()
    invoice_id = await _invoice(db_pool, None)

    await _run(db_pool, invoice_id, transport)

    async with db_pool.connection() as conn:
        rows = await _outbox(conn, SENDER)
        candidate = await portal_candidates.get(conn, invoice_id)
    assert requested == []
    assert rows == []
    assert candidate is None
