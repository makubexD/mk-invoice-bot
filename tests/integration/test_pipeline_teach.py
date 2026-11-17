"""Integration tests for teaching a portal link and BR-S06's owner review (R-10.2, R-11.4)."""

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
from invoicebot.gaps.kinds import fingerprint
from invoicebot.net.guard import Resolver
from invoicebot.pipeline.intake import IntakeRoles, handle_inbound
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.teach import TeachContext, TeachSetup, answer, handle_press, teach
from invoicebot.queue import outbox
from invoicebot.rules.messages_es import BUTTON_APPROVE, BUTTON_REJECT, MESSAGES
from invoicebot.store import taught_links
from invoicebot.store.gaps import get_gap
from invoicebot.store.invoices import create_invoice, save_reading
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
#: The owner profile is the example config's only admin (R-11.4).
ADMIN = "yo"
#: The ops repo an approved link is commented onto when GitHub is enabled (D118).
GITHUB_REPO = "owner/mk-invoice-bot-ops"
#: The synthetic issue the PORTAL_MISSING gap was reported to.
ISSUE_URL = "https://github.com/owner/mk-invoice-bot-ops/issues/7"
GAP_FINGERPRINT = fingerprint("PORTAL_MISSING", str(ISSUER_RUC))


class FakeResolver(Resolver):
    """A ``Resolver`` that answers the public address, whatever the host is."""

    async def resolve(self, host: str) -> list[str]:
        """Return the fixed public address."""
        return [PUBLIC_ADDRESS]


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def clean_teach_tables(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> AsyncIterator[None]:
    """Start each test from empty taught links, gaps and outbox rows."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM taught_links")
        await conn.execute("DELETE FROM invoice_gaps")
        await conn.execute("DELETE FROM gaps")
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


def _decision_buttons(version: int = 1) -> list[dict[str, str]]:
    """Return the button payload BR-S06's owner message must carry."""
    return [
        {"id": f"t{ISSUER_RUC}v{version}:ok", "title": BUTTON_APPROVE},
        {"id": f"t{ISSUER_RUC}v{version}:no", "title": BUTTON_REJECT},
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
        assert await taught_links.propose(conn, ISSUER_RUC, GOOD_URL, SENDER) == 1
        assert await taught_links.decide(conn, ISSUER_RUC, 1, approved=False) is not None
        assert await taught_links.propose(conn, ISSUER_RUC, OTHER_URL, SENDER) == 2
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
        assert await taught_links.propose(conn, ISSUER_RUC, GOOD_URL, SENDER) is not None
        first = await taught_links.decide(conn, ISSUER_RUC, 1, approved=approved)
        second = await taught_links.decide(conn, ISSUER_RUC, 1, approved=approved)
        cur = await conn.execute(
            "SELECT approved_at IS NOT NULL FROM taught_links WHERE ruc_emisor = %s",
            (ISSUER_RUC,),
        )
        row = await cur.fetchone()

    assert first == TaughtLink(ISSUER_RUC, GOOD_URL, SENDER, state)
    assert second is None
    assert row is not None
    assert row[0] is approved


def _reading(*, ruc_emisor: str | None = str(ISSUER_RUC)) -> Reading:
    """Return a minimal reading for ISSUER_RUC, or one without an issuer when None."""
    fields: dict[str, Field] = {"razon_social": Field(RAZON_SOCIAL, "qr", 1.0)}
    if ruc_emisor is not None:
        fields["ruc_emisor"] = Field(ruc_emisor, "qr", 1.0)
    return Reading(fields=fields, document_url=None)


async def _invoice_with_reading(conn: AsyncConnection[Any], profile_id: str, reading: Reading) -> int:
    """Create an invoice for profile_id with ``reading`` and return its id."""
    invoice_id = await create_invoice(conn, profile_id, "whatsapp")
    await save_reading(conn, invoice_id, reading)
    return invoice_id


async def _ask_for_portal(conn: AsyncConnection[Any], profile_id: str, invoice_id: int) -> None:
    """Queue BR-F02 for the invoice's sender, as the fetch pipeline does (BR-F02)."""
    text = MESSAGES["BR-F02"].format(razon_social=RAZON_SOCIAL, ruc_emisor=ISSUER_RUC)
    await queue_message(
        conn, Addressee("whatsapp", profile_id), text, invoice_id=invoice_id, rule_id="BR-F02"
    )


def _message(profile_id: str, text: str) -> InboundMessage:
    """Build a canonical text message from profile_id."""
    return InboundMessage(
        channel="whatsapp",
        external_id=f"ext-{uuid4().hex}",
        profile_id=profile_id,
        sender_role="owner",
        text=text,
        received_at=datetime.now(UTC),
    )


def _setup(*, github_repo: str | None = None) -> TeachSetup:
    """Return a teach setup from the example profiles, a fake resolver and an optional repo."""
    ctx = _context()
    return TeachSetup(profiles=ctx.profiles, resolver=ctx.resolver, github_repo=github_repo)


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


def _press(profile_id: str, *, approved: bool, version: int = 1) -> InboundMessage:
    """Build a BR-S06 decision press from ``profile_id`` (D118, D137)."""
    suffix = "ok" if approved else "no"
    return _button_press(profile_id, f"t{ISSUER_RUC}v{version}:{suffix}")


async def _insert_gap(conn: AsyncConnection[Any], *, issue_url: str | None = None) -> None:
    """Insert the PORTAL_MISSING gap an approved taught link fills (D118)."""
    await conn.execute(
        """
        INSERT INTO gaps (fingerprint, kind, subject, state, issue_url)
        VALUES (%s, 'PORTAL_MISSING', %s, 'open', %s)
        """,
        (GAP_FINGERPRINT, f"RUC {ISSUER_RUC}", issue_url),
    )


async def _github_rows(conn: AsyncConnection[Any]) -> list[dict[str, Any]]:
    """Return the github channel's outbox payloads, oldest first."""
    cur = await conn.execute("SELECT payload_json FROM outbox WHERE channel = 'github' ORDER BY id")
    return [cast(dict[str, Any], payload) for (payload,) in await cur.fetchall()]


async def test_answer_url_proposes_the_link(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.4: a URL after BR-F02 stores a proposed link and answers BR-S06."""
    setup = _setup()
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, _reading())
        await _ask_for_portal(conn, SENDER, invoice_id)
        handled = await answer(conn, setup, _message(SENDER, GOOD_URL))
        link = await taught_links.get(conn, ISSUER_RUC)
        sender_rows = await _outbox(conn, SENDER)

    assert handled is True
    assert link == TaughtLink(ISSUER_RUC, GOOD_URL, SENDER, "proposed")
    assert sender_rows[-1] == (MESSAGES["BR-S06"], "BR-S06", [])


async def test_answer_dont_know_stores_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.4: "no sé" after BR-F02 stores no link and answers BR-F02_dont_know."""
    setup = _setup()
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, _reading())
        await _ask_for_portal(conn, SENDER, invoice_id)
        handled = await answer(conn, setup, _message(SENDER, "no sé"))
        link = await taught_links.get(conn, ISSUER_RUC)
        sender_rows = await _outbox(conn, SENDER)

    assert handled is True
    assert link is None
    assert sender_rows[-1] == (MESSAGES["BR-F02_dont_know"], "BR-F02_dont_know", [])


async def test_answer_ignores_a_text_that_is_not_a_url_or_dont_know(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.4: any other text after BR-F02 is not handled and answers nothing."""
    setup = _setup()
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, _reading())
        await _ask_for_portal(conn, SENDER, invoice_id)
        handled = await answer(conn, setup, _message(SENDER, "serie=F001"))
        link = await taught_links.get(conn, ISSUER_RUC)
        sender_rows = await _outbox(conn, SENDER)

    assert handled is False
    assert link is None
    assert [rule_id for _text, rule_id, _buttons in sender_rows] == ["BR-F02"]


async def test_answer_ignores_a_url_when_the_newest_question_is_not_br_f02(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.4: a URL after a different newest question is not taken as a portal."""
    setup = _setup()
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, _reading())
        await queue_message(
            conn,
            Addressee("whatsapp", SENDER),
            "¿Confirmas el flujo?",
            invoice_id=invoice_id,
            rule_id="BR-R03",
        )
        handled = await answer(conn, setup, _message(SENDER, GOOD_URL))
        link = await taught_links.get(conn, ISSUER_RUC)

    assert handled is False
    assert link is None


async def test_answer_ignores_a_url_without_an_issuer_in_the_reading(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.4: a URL whose invoice reading has no ruc_emisor is not handled."""
    setup = _setup()
    async with db_pool.connection() as conn:
        invoice_id = await _invoice_with_reading(conn, SENDER, _reading(ruc_emisor=None))
        await _ask_for_portal(conn, SENDER, invoice_id)
        handled = await answer(conn, setup, _message(SENDER, GOOD_URL))
        link = await taught_links.get(conn, ISSUER_RUC)

    assert handled is False
    assert link is None


async def test_answer_with_no_question_does_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.4: a URL from a sender with no outbox question is not handled."""
    setup = _setup()
    async with db_pool.connection() as conn:
        handled = await answer(conn, setup, _message(SENDER, GOOD_URL))
        link = await taught_links.get(conn, ISSUER_RUC)

    assert handled is False
    assert link is None


# BR-S06's [Aprobar] and [Rechazar] (D118)


async def _outbox_count(conn: AsyncConnection[Any]) -> int:
    """Return how many outbox rows there are."""
    cur = await conn.execute("SELECT count(*) FROM outbox")
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def test_press_approve_marks_the_link_and_the_gap_hint(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S06: [Aprobar] sets the link approved, stores the gap's hint and answers the admin."""
    async with db_pool.connection() as conn:
        assert await taught_links.propose(conn, ISSUER_RUC, GOOD_URL, SENDER) is not None
        await _insert_gap(conn)
        await handle_press(conn, _press(ADMIN, approved=True), _setup())
        link = await taught_links.get(conn, ISSUER_RUC)
        gap = await get_gap(conn, GAP_FINGERPRINT)
        admin_rows = await _outbox(conn, ADMIN)

    expected = MESSAGES["BR-S06_approved"].format(ruc_emisor=ISSUER_RUC)
    assert link == TaughtLink(ISSUER_RUC, GOOD_URL, SENDER, "approved")
    assert gap is not None
    assert gap.hint_url == GOOD_URL
    assert admin_rows == [(expected, "BR-S06_approved", [])]


async def test_press_approve_queues_one_comment_on_the_issue(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S06: with an issue and GitHub enabled, [Aprobar] queues exactly one comment row."""
    async with db_pool.connection() as conn:
        assert await taught_links.propose(conn, ISSUER_RUC, GOOD_URL, SENDER) is not None
        await _insert_gap(conn, issue_url=ISSUE_URL)
        await handle_press(conn, _press(ADMIN, approved=True), _setup(github_repo=GITHUB_REPO))
        rows = await _github_rows(conn)

    assert rows == [
        {
            "op": "comment",
            "fingerprint": GAP_FINGERPRINT,
            "issue_url": ISSUE_URL,
            "hint_url": GOOD_URL,
        }
    ]


async def test_press_approve_sets_the_pending_issue_hint(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S06: with no issue yet, [Aprobar] writes the link into the pending create_issue fields."""
    fields: dict[str, Any] = {
        "kind": "PORTAL_MISSING",
        "fingerprint": GAP_FINGERPRINT,
        "hint_url": None,
    }
    async with db_pool.connection() as conn:
        assert await taught_links.propose(conn, ISSUER_RUC, GOOD_URL, SENDER) is not None
        await _insert_gap(conn)
        await outbox.add(
            conn,
            "github",
            GITHUB_REPO,
            {"op": "create_issue", "fingerprint": GAP_FINGERPRINT, "fields": fields},
        )
        await handle_press(conn, _press(ADMIN, approved=True), _setup(github_repo=GITHUB_REPO))
        rows = await _github_rows(conn)

    assert len(rows) == 1
    assert rows[0]["fields"]["hint_url"] == GOOD_URL


async def test_press_second_press_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S06: a second [Aprobar] finds no proposed link and adds no outbox row."""
    async with db_pool.connection() as conn:
        assert await taught_links.propose(conn, ISSUER_RUC, GOOD_URL, SENDER) is not None
        await _insert_gap(conn)
        setup = _setup()
        await handle_press(conn, _press(ADMIN, approved=True), setup)
        before = await _outbox_count(conn)
        await handle_press(conn, _press(ADMIN, approved=True), setup)
        after = await _outbox_count(conn)
        link = await taught_links.get(conn, ISSUER_RUC)
        gap = await get_gap(conn, GAP_FINGERPRINT)

    assert before == after == 1
    assert link == TaughtLink(ISSUER_RUC, GOOD_URL, SENDER, "approved")
    assert gap is not None
    assert gap.hint_url == GOOD_URL


async def test_press_from_a_non_admin_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S06: only an admin's press counts; another sender's press changes nothing."""
    async with db_pool.connection() as conn:
        assert await taught_links.propose(conn, ISSUER_RUC, GOOD_URL, SENDER) is not None
        await _insert_gap(conn)
        await handle_press(conn, _press(SENDER, approved=True), _setup())
        link = await taught_links.get(conn, ISSUER_RUC)
        gap = await get_gap(conn, GAP_FINGERPRINT)
        sender_rows = await _outbox(conn, SENDER)

    assert link == TaughtLink(ISSUER_RUC, GOOD_URL, SENDER, "proposed")
    assert gap is not None
    assert gap.hint_url is None
    assert sender_rows == []


async def test_press_reject_sets_rejected_and_a_later_teach_proposes_again(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S06: [Rechazar] sets the link rejected and a later proposal is accepted again."""
    ctx = _context()
    async with db_pool.connection() as conn:
        await teach(conn, ctx, ISSUER_RUC, RAZON_SOCIAL, GOOD_URL)
        await conn.execute("DELETE FROM outbox")
        await handle_press(conn, _press(ADMIN, approved=False), _setup())
        rejected = await taught_links.get(conn, ISSUER_RUC)
        admin_rows = await _outbox(conn, ADMIN)
        await teach(conn, ctx, ISSUER_RUC, RAZON_SOCIAL, OTHER_URL)
        reproposed = await taught_links.get(conn, ISSUER_RUC)

    expected = MESSAGES["BR-S06_rejected"].format(ruc_emisor=ISSUER_RUC)
    assert rejected == TaughtLink(ISSUER_RUC, GOOD_URL, SENDER, "rejected")
    assert admin_rows == [(expected, "BR-S06_rejected", [])]
    assert reproposed == TaughtLink(ISSUER_RUC, OTHER_URL, SENDER, "proposed")


async def test_press_current_proposal_version_approves_it(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S06: a press on the current proposal's version approves it (D137)."""
    async with db_pool.connection() as conn:
        version = await taught_links.propose(conn, ISSUER_RUC, GOOD_URL, SENDER)
        assert version is not None
        await _insert_gap(conn)
        await handle_press(conn, _press(ADMIN, approved=True, version=version), _setup())
        link = await taught_links.get(conn, ISSUER_RUC)

    assert link == TaughtLink(ISSUER_RUC, GOOD_URL, SENDER, "approved")


async def test_press_older_proposal_version_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S06: after [Rechazar], a press of the first proposal leaves the new one proposed (D137)."""
    async with db_pool.connection() as conn:
        first = await taught_links.propose(conn, ISSUER_RUC, GOOD_URL, SENDER)
        assert first is not None
        await handle_press(conn, _press(ADMIN, approved=False, version=first), _setup())
        second = await taught_links.propose(conn, ISSUER_RUC, OTHER_URL, SENDER)
        assert second is not None
        assert second != first
        await conn.execute("DELETE FROM outbox")
        await handle_press(conn, _press(ADMIN, approved=True, version=first), _setup())
        link = await taught_links.get(conn, ISSUER_RUC)
        admin_rows = await _outbox(conn, ADMIN)

    assert link == TaughtLink(ISSUER_RUC, OTHER_URL, SENDER, "proposed")
    assert admin_rows == []


async def test_press_unversioned_button_id_is_not_handled(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S06: an id in the old ``t<ruc>:ok`` form is not handled (D137)."""
    async with db_pool.connection() as conn:
        assert await taught_links.propose(conn, ISSUER_RUC, GOOD_URL, SENDER) is not None
        await handle_press(conn, _button_press(ADMIN, f"t{ISSUER_RUC}:ok"), _setup())
        link = await taught_links.get(conn, ISSUER_RUC)

    assert link == TaughtLink(ISSUER_RUC, GOOD_URL, SENDER, "proposed")


async def test_press_through_intake_approves_without_an_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S06: intake hands a ``t<ruc>v<version>:ok`` press to teach and stores it without an invoice."""
    async with db_pool.connection() as conn:
        assert await taught_links.propose(conn, ISSUER_RUC, GOOD_URL, SENDER) is not None
        await _insert_gap(conn)
        result = await handle_inbound(
            conn,
            _press(ADMIN, approved=True),
            max_queued_per_sender=5,
            media_max_mb=10,
            timezone="America/Lima",
            roles=IntakeRoles(teach=_setup()),
        )
        link = await taught_links.get(conn, ISSUER_RUC)
        gap = await get_gap(conn, GAP_FINGERPRINT)

    assert result.status == "stored"
    assert result.invoice_id is None
    assert link == TaughtLink(ISSUER_RUC, GOOD_URL, SENDER, "approved")
    assert gap is not None
    assert gap.hint_url == GOOD_URL
