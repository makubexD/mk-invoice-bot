"""Integration tests for chat commands, ``/ayuda`` and invoice scoping (R-10.1, BR-S07).

Commands are reached through :func:`handle_inbound` and :func:`resolve_target`, against
real Postgres; every test uses its own profile, so no outbox row leaks between them.
"""

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import Button, InboundMessage
from invoicebot.config.loader import load_config
from invoicebot.config.models import MediaMaxBytes
from invoicebot.fetchers.chain import Run, decide
from invoicebot.gaps.kinds import GAP_KINDS
from invoicebot.net.guard import Resolver
from invoicebot.pipeline.actions import cancel_invoice
from invoicebot.pipeline.commands import (
    COMMANDS,
    CommandContext,
    handle_command,
    help_text,
    parse_command,
    resolve_target,
)
from invoicebot.pipeline.handlers import MEGABYTE
from invoicebot.pipeline.intake import IntakeRoles, handle_inbound
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.resume import resume_delivery
from invoicebot.pipeline.teach import TeachSetup
from invoicebot.rules.messages_es import (
    BUTTON_CANCEL,
    CANCEL_LATE,
    CANCELLED_BY_USER,
    COMMAND_NEEDS_ID,
    GAP_IGNORED,
    GAPS_EMPTY,
    GAPS_HEADER,
    GAPS_ISSUE,
    GAPS_LINE,
    HELP_AYUDA,
    HELP_GAP,
    HELP_GAPS,
    HELP_HEADER,
    HELP_PORTAL,
    MESSAGES,
    PENDING_BATCH_LINE,
    PENDING_HEADER,
    PENDING_LINE,
    PENDING_MORE,
    PENDING_NO_ISSUER,
    PENDING_NONE,
    REPROCESS_NOT_ALLOWED,
    REPROCESS_STARTED,
    REPROCESS_TOO_SOON,
    STATUS,
    STATUS_RULE,
    STATUS_RULE_ONLY,
    state_desc,
)
from invoicebot.states import StateMoved
from invoicebot.store import taught_links
from invoicebot.store.fetch_attempts import list_attempts, record_attempt
from invoicebot.store.flow_b import get_batch_state, open_batch, set_batch_state
from invoicebot.store.gaps import mark_reported, upsert_gap
from invoicebot.store.invoices import (
    InvoiceHead,
    create_invoice,
    load_invoice_head,
    set_flow,
    set_state,
)
from invoicebot.store.taught_links import TaughtLink

MAX_QUEUED = 10
MEDIA_MAX_BYTES = MediaMaxBytes(image=10 * MEGABYTE, pdf=10 * MEGABYTE, xml=MEGABYTE, zip=10 * MEGABYTE)
#: The owner's timezone, for the dates a command prints (S11.4).
TIMEZONE = "America/Lima"
#: An id no test creates, so BR-S07's unknown-id answer is deterministic.
UNKNOWN_INVOICE_ID = 999_999_999
#: The stored question text a BR-R03 invoice's ``/estado`` repeats (S4.9).
QUESTION_TEXT = "¿En qué flujo lo registro?"
#: The flow an invoice must have before ``/reprocesar`` restarts its fetch (D96).
FLOW = "work_microsoft"
#: A synthetic subject for the gaps the gap command tests seed (S8.9).
GAP_SUBJECT = "RUC 20000000001"
#: A synthetic issue link a seeded gap is reported to (S8.9, D139).
GAP_ISSUE_URL = "https://example.invalid/issues/7"
#: The gap kinds those tests seed, one reported and one left open (S8.9).
PORTAL_MISSING = GAP_KINDS["PORTAL_MISSING"]
PARSER_MISS = GAP_KINDS["PARSER_MISS"]


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def drop_channel_windows(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> AsyncIterator[None]:
    """Leave no window behind: each inbound message opens one, and later tests read them per profile."""
    yield
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM channel_windows")


def _profile() -> str:
    """Return a profile id no other test uses."""
    return f"profile-{uuid4().hex}"


def _message(profile_id: str, text: str) -> InboundMessage:
    """Build a canonical text message for profile_id."""
    return InboundMessage(
        channel="whatsapp",
        external_id=f"ext-{uuid4().hex}",
        profile_id=profile_id,
        sender_role="owner",
        text=text,
        attachments=[],
        received_at=datetime.now(UTC),
    )


async def _invoice(pool: AsyncConnectionPool[AsyncConnection[Any]], profile_id: str) -> int:
    """Create one RECEIVED invoice owned by profile_id and return its id."""
    async with pool.connection() as conn, conn.transaction():
        return await create_invoice(conn, profile_id, "whatsapp")


async def _invoice_in(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    state: str,
    rule_id: str | None = None,
    *,
    flow: bool = False,
) -> int:
    """Create an invoice in state, waiting on rule_id and routed to FLOW when asked."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        if flow:
            await set_flow(conn, invoice_id, FLOW)
        await set_state(conn, invoice_id, state, rule_id=rule_id)
    return invoice_id


async def _failed_batch_invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
) -> int:
    """Create a BATCHED invoice whose batch failed, and return its id (D96)."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await set_flow(conn, invoice_id, FLOW)
        batch_id = await open_batch(conn, invoice_id, FLOW, "prepared")
        await set_batch_state(conn, batch_id, "failed")
        await set_state(conn, invoice_id, "BATCHED", rule_id="BR-D07")
    return invoice_id


async def _jobs(conn: AsyncConnection[Any], invoice_id: int) -> list[str]:
    """Return the steps of the invoice's queued jobs, in insertion order."""
    cur = await conn.execute("SELECT step FROM jobs WHERE invoice_id = %s ORDER BY id", (invoice_id,))
    return [str(step) for (step,) in await cur.fetchall()]


async def _question(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    invoice_id: int,
    profile_id: str,
    rule_id: str,
    text: str,
    buttons: Sequence[Button] = (),
) -> None:
    """Store a question outbox row linked to invoice_id and naming rule_id."""
    async with pool.connection() as conn, conn.transaction():
        await queue_message(
            conn,
            Addressee("whatsapp", profile_id),
            text,
            invoice_id=invoice_id,
            rule_id=rule_id,
            buttons=buttons,
        )


async def _backdate_questions(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    invoice_id: int,
    *,
    minutes: int = 11,
) -> None:
    """Move the invoice's outbox rows back in time so /pendientes may re-send a question (S4.8, D94)."""
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            """
            UPDATE outbox
            SET created_at = created_at - make_interval(mins => %s)
            WHERE invoice_id = %s
            """,
            (minutes, invoice_id),
        )


async def _set_fetch_round(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    invoice_id: int,
    *,
    minutes_ago: int,
) -> None:
    """Stamp the invoice's last fetch round minutes_ago, with the database clock (D171)."""
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE invoices SET fetch_round_at = now() - make_interval(mins => %s) WHERE id = %s",
            (minutes_ago, invoice_id),
        )


async def _parked_invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    rule_id: str,
    *,
    waiting_state: str = "CONFIRMING",
) -> int:
    """Create a PENDING_USER invoice parked while waiting on rule_id (D94)."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await conn.execute(
            """
            UPDATE invoices
            SET state = 'PENDING_USER',
                rule_id = 'BR-S08',
                waiting_state = %s,
                waiting_rule_id = %s
            WHERE id = %s
            """,
            (waiting_state, rule_id, invoice_id),
        )
    return invoice_id


async def _set_amount(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    invoice_id: int,
    razon_social: str,
    total: Decimal,
) -> None:
    """Store the issuer and total ``/pendientes`` shows for invoice_id."""
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE invoices SET razon_social = %s, total = %s WHERE id = %s",
            (razon_social, total, invoice_id),
        )


async def _prepared_invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    *,
    days: int,
    razon_social: str,
    total: Decimal,
) -> int:
    """Create a BATCHED invoice on a prepared batch, stamped days ago (S11.4)."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await set_flow(conn, invoice_id, FLOW)
        batch_id = await open_batch(conn, invoice_id, FLOW, "prepared")
        await set_batch_state(conn, batch_id, "prepared")
        await set_state(conn, invoice_id, "BATCHED", rule_id="BR-D07")
        await conn.execute(
            """
            UPDATE invoices
            SET razon_social = %s,
                total = %s,
                updated_at = now() - make_interval(days => %s)
            WHERE id = %s
            """,
            (razon_social, total, days, invoice_id),
        )
    return invoice_id


async def _pending_invoices(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    count: int,
) -> list[int]:
    """Create count parked invoices without a stored question in one transaction."""
    async with pool.connection() as conn, conn.transaction():
        invoice_ids = []
        for _ in range(count):
            invoice_id = await create_invoice(conn, profile_id, "whatsapp")
            await set_state(conn, invoice_id, "PENDING_USER", rule_id="BR-S08")
            invoice_ids.append(invoice_id)
    return invoice_ids


async def _updated_at(conn: AsyncConnection[Any], invoice_id: int) -> datetime:
    """Return the stored updated_at of invoice_id."""
    cur = await conn.execute("SELECT updated_at FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return cast(datetime, row[0])


async def _invoice_payloads(conn: AsyncConnection[Any], invoice_id: int) -> list[dict[str, Any]]:
    """Return the outbox payloads linked to invoice_id, oldest first."""
    cur = await conn.execute(
        "SELECT payload_json FROM outbox WHERE invoice_id = %s ORDER BY id",
        (invoice_id,),
    )
    return [cast(dict[str, Any], payload) for (payload,) in await cur.fetchall()]


async def _replies(conn: AsyncConnection[Any], profile_id: str) -> list[tuple[str, str | None]]:
    """Return the outbox's ``(text, rule_id)`` rows for profile_id, oldest first."""
    cur = await conn.execute(
        """
        SELECT payload_json->>'text', payload_json->>'rule_id'
        FROM outbox
        WHERE "to" = %s
        ORDER BY id
        """,
        (profile_id,),
    )
    rows = await cur.fetchall()
    return [(str(text), rule_id) for text, rule_id in rows]


def _help() -> str:
    """Return the help a non-admin gets: the header and each public command's line."""
    lines = [command.help for command in COMMANDS.values() if not command.admin_only]
    return "\n".join([HELP_HEADER, *lines])


async def test_ayuda_replies_with_the_command_list(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-10.1: /ayuda is stored and answered with the header and /ayuda's own line."""
    profile_id = _profile()
    msg = _message(profile_id, "/ayuda")

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        replies = await _replies(conn, profile_id)

    assert result.status == "stored"
    assert replies == [(_help(), None)]
    assert HELP_AYUDA in _help()


async def test_uppercase_command_is_the_same_command(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-10.1: /AYUDA runs /ayuda, lowering the name."""
    profile_id = _profile()
    msg = _message(profile_id, "/AYUDA")

    async with db_pool.connection() as conn:
        await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        replies = await _replies(conn, profile_id)

    assert replies == [(_help(), None)]


async def test_unknown_command_answers_with_the_help(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-10.1: an unknown command name answers the same help as /ayuda."""
    profile_id = _profile()
    msg = _message(profile_id, "/foo")

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        replies = await _replies(conn, profile_id)

    assert result.status == "stored"
    assert replies == [(_help(), None)]


async def test_own_invoice_id_resolves_to_its_head(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-10.1: the sender's own invoice id resolves to its id and head, with no reply."""
    profile_id = _profile()
    invoice_id = await _invoice(db_pool, profile_id)
    msg = _message(profile_id, f"/estado {invoice_id}")

    async with db_pool.connection() as conn:
        resolved = await resolve_target(conn, msg, "estado", str(invoice_id), frozenset())
        replies = await _replies(conn, profile_id)

    assert resolved == (invoice_id, InvoiceHead("RECEIVED", profile_id, "whatsapp"))
    assert replies == []


async def test_hash_prefixed_invoice_id_resolves_to_its_head(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-10.1: ``#12`` names the same invoice as ``12``."""
    profile_id = _profile()
    invoice_id = await _invoice(db_pool, profile_id)
    msg = _message(profile_id, f"/estado #{invoice_id}")

    async with db_pool.connection() as conn:
        resolved = await resolve_target(conn, msg, "estado", f"#{invoice_id}", frozenset())
        replies = await _replies(conn, profile_id)

    assert resolved == (invoice_id, InvoiceHead("RECEIVED", profile_id, "whatsapp"))
    assert replies == []


async def test_foreign_invoice_id_answers_br_s07(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S07: another profile's id is refused with BR-S07 and changes nothing."""
    owner_profile = _profile()
    invoice_id = await _invoice(db_pool, owner_profile)
    sender_profile = _profile()
    msg = _message(sender_profile, f"/estado {invoice_id}")

    async with db_pool.connection() as conn:
        resolved = await resolve_target(conn, msg, "estado", str(invoice_id), frozenset())
        replies = await _replies(conn, sender_profile)

    assert resolved is None
    assert replies == [(MESSAGES["BR-S07"].format(id=invoice_id), "BR-S07")]


async def test_unknown_invoice_id_answers_the_same_br_s07(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S07: an id that does not exist gets the same answer as another profile's id."""
    profile_id = _profile()
    msg = _message(profile_id, f"/estado {UNKNOWN_INVOICE_ID}")

    async with db_pool.connection() as conn:
        resolved = await resolve_target(conn, msg, "estado", str(UNKNOWN_INVOICE_ID), frozenset())
        replies = await _replies(conn, profile_id)

    assert resolved is None
    assert replies == [(MESSAGES["BR-S07"].format(id=UNKNOWN_INVOICE_ID), "BR-S07")]


async def test_admin_resolves_another_profiles_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-10.1: an admin sender may resolve another profile's invoice."""
    owner_profile = _profile()
    invoice_id = await _invoice(db_pool, owner_profile)
    admin_profile = _profile()
    msg = _message(admin_profile, f"/estado {invoice_id}")

    async with db_pool.connection() as conn:
        resolved = await resolve_target(conn, msg, "estado", str(invoice_id), frozenset({admin_profile}))
        replies = await _replies(conn, admin_profile)

    assert resolved == (invoice_id, InvoiceHead("RECEIVED", owner_profile, "whatsapp"))
    assert replies == []


@pytest.mark.parametrize("arg", ["", "abc", "²", "1" * 19])
async def test_missing_invoice_id_asks_for_it(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    arg: str,
) -> None:
    """R-10.1: a missing, non-ASCII-digit or out-of-range id asks for it with COMMAND_NEEDS_ID."""
    profile_id = _profile()
    msg = _message(profile_id, f"/estado {arg}")

    async with db_pool.connection() as conn:
        resolved = await resolve_target(conn, msg, "estado", arg, frozenset())
        replies = await _replies(conn, profile_id)

    assert resolved is None
    assert replies == [(COMMAND_NEEDS_ID.format(command="estado"), None)]


async def test_parse_command_splits_name_and_argument() -> None:
    """R-10.1: parse_command lower-cases the name, strips the argument and rejects a bare /."""
    assert parse_command(" /AYUDA   12 ") == ("ayuda", "12")
    assert parse_command("hola") is None
    assert parse_command("/") is None


async def test_estado_repeats_the_rule_and_its_last_question(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.1: /estado on a BR-R03 invoice shows its state and the stored question."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, "CONFIRMING", rule_id="BR-R03")
    await _question(db_pool, invoice_id, profile_id, "BR-R03", QUESTION_TEXT)
    msg = _message(profile_id, f"/estado {invoice_id}")
    expected = STATUS.format(id=invoice_id, state_desc=state_desc("CONFIRMING"))
    expected = f"{expected}\n{STATUS_RULE.format(rule_id='BR-R03', text=QUESTION_TEXT)}"

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        replies = await _replies(conn, profile_id)

    assert result.status == "stored"
    assert replies[-1] == (expected, None)


async def test_estado_without_a_rule_shows_only_the_state(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.1: an invoice with no rule gets only the state line."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, "FETCHING_DOCS")
    msg = _message(profile_id, f"/estado {invoice_id}")
    expected = STATUS.format(id=invoice_id, state_desc=state_desc("FETCHING_DOCS"))

    async with db_pool.connection() as conn:
        await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        replies = await _replies(conn, profile_id)

    assert replies == [(expected, None)]


async def test_estado_with_a_rule_but_no_stored_text_shows_the_reason(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.1: a rule with no stored text gets STATUS_RULE_ONLY instead of the question."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, "CONFIRMING", rule_id="BR-R04")
    msg = _message(profile_id, f"/estado {invoice_id}")
    expected = STATUS.format(id=invoice_id, state_desc=state_desc("CONFIRMING"))
    expected = f"{expected}\n{STATUS_RULE_ONLY.format(rule_id='BR-R04')}"

    async with db_pool.connection() as conn:
        await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        replies = await _replies(conn, profile_id)

    assert replies == [(expected, None)]


async def test_estado_on_another_profiles_invoice_answers_br_s07(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S07: /estado on another profile's invoice is refused and changes nothing."""
    owner_profile = _profile()
    invoice_id = await _invoice_in(db_pool, owner_profile, "FETCHING_DOCS")
    sender_profile = _profile()
    msg = _message(sender_profile, f"/estado {invoice_id}")

    async with db_pool.connection() as conn:
        await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        replies = await _replies(conn, sender_profile)

    assert replies == [(MESSAGES["BR-S07"].format(id=invoice_id), "BR-S07")]


async def test_cancelar_cancels_a_confirming_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D96: /cancelar on a CONFIRMING invoice cancels it with the button's reply."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, "CONFIRMING", rule_id="BR-D06")
    msg = _message(profile_id, f"/cancelar {invoice_id}")

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        head = await load_invoice_head(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        replies = await _replies(conn, profile_id)

    assert result.status == "stored"
    assert head is not None
    assert head.state == "CANCELLED"
    assert jobs == []
    assert replies == [(CANCELLED_BY_USER.format(id=invoice_id), None)]


async def test_cancelar_on_a_batched_invoice_is_late(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D96: /cancelar on a BATCHED invoice answers CANCEL_LATE and changes nothing."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, "BATCHED", rule_id="BR-D07")
    msg = _message(profile_id, f"/cancelar {invoice_id}")
    expected = CANCEL_LATE.format(id=invoice_id, state_desc=state_desc("BATCHED"))

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        head = await load_invoice_head(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        replies = await _replies(conn, profile_id)

    assert result.status == "stored"
    assert head is not None
    assert head.state == "BATCHED"
    assert jobs == []
    assert replies == [(expected, None)]


async def test_cancelar_loses_to_a_move_after_resolve_target(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D96: a cancel whose invoice moved to DELIVERING after resolve_target raises StateMoved."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, "CONFIRMING", rule_id="BR-D06")
    msg = _message(profile_id, f"/cancelar {invoice_id}")
    async with db_pool.connection() as conn, conn.transaction():
        await set_state(conn, invoice_id, "DELIVERING")

    async with db_pool.connection() as conn:
        with pytest.raises(StateMoved):
            await cancel_invoice(conn, invoice_id, msg, expected="CONFIRMING")
        head = await load_invoice_head(conn, invoice_id)

    assert head is not None
    assert head.state == "DELIVERING"


async def test_reprocesar_on_failed_with_a_flow_refetches(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D96: /reprocesar on a FAILED invoice with a flow returns it to FETCHING_DOCS."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, "FAILED", rule_id="BR-S03", flow=True)
    msg = _message(profile_id, f"/reprocesar {invoice_id}")

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        head = await load_invoice_head(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        replies = await _replies(conn, profile_id)

    assert result.status == "stored"
    assert head is not None
    assert head.state == "FETCHING_DOCS"
    assert jobs == ["fetch"]
    assert replies == [(REPROCESS_STARTED.format(id=invoice_id), None)]


async def test_reprocesar_on_manual_required_refetches(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D96: /reprocesar on a MANUAL_REQUIRED invoice returns it to FETCHING_DOCS."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, "MANUAL_REQUIRED", rule_id="BR-D02", flow=True)
    msg = _message(profile_id, f"/reprocesar {invoice_id}")

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        head = await load_invoice_head(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        replies = await _replies(conn, profile_id)

    assert result.status == "stored"
    assert head is not None
    assert head.state == "FETCHING_DOCS"
    assert jobs == ["fetch"]
    assert replies == [(REPROCESS_STARTED.format(id=invoice_id), None)]


async def test_reprocesar_too_soon_refuses_the_round_and_keeps_the_state(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D171: /reprocesar refuses a fetch round that started within reprocess_min_interval_s."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, "MANUAL_REQUIRED", rule_id="BR-D02", flow=True)
    await _set_fetch_round(db_pool, invoice_id, minutes_ago=1)
    msg = _message(profile_id, f"/reprocesar {invoice_id}")
    context = CommandContext(admins=frozenset(), timezone=TIMEZONE, reprocess_min_interval_s=600)

    async with db_pool.connection() as conn:
        await handle_command(conn, msg, "reprocesar", str(invoice_id), context=context)
        head = await load_invoice_head(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        replies = await _replies(conn, profile_id)

    assert head is not None
    assert head.state == "MANUAL_REQUIRED"
    assert jobs == []
    assert replies == [(REPROCESS_TOO_SOON.format(id=invoice_id), None)]


async def test_reprocesar_after_the_interval_starts_a_new_round(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D171: a round older than reprocess_min_interval_s lets /reprocesar start a new one."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, "MANUAL_REQUIRED", rule_id="BR-D02", flow=True)
    await _set_fetch_round(db_pool, invoice_id, minutes_ago=20)
    msg = _message(profile_id, f"/reprocesar {invoice_id}")
    context = CommandContext(admins=frozenset(), timezone=TIMEZONE, reprocess_min_interval_s=600)

    async with db_pool.connection() as conn:
        await handle_command(conn, msg, "reprocesar", str(invoice_id), context=context)
        head = await load_invoice_head(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        replies = await _replies(conn, profile_id)

    assert head is not None
    assert head.state == "FETCHING_DOCS"
    assert jobs == ["fetch"]
    assert replies == [(REPROCESS_STARTED.format(id=invoice_id), None)]


async def test_reprocesar_with_the_default_interval_does_not_block(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D171: the default 0 means no wait, so a recent round does not block /reprocesar."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, "MANUAL_REQUIRED", rule_id="BR-D02", flow=True)
    await _set_fetch_round(db_pool, invoice_id, minutes_ago=1)
    msg = _message(profile_id, f"/reprocesar {invoice_id}")
    context = CommandContext(admins=frozenset(), timezone=TIMEZONE, reprocess_min_interval_s=0)

    async with db_pool.connection() as conn:
        await handle_command(conn, msg, "reprocesar", str(invoice_id), context=context)
        head = await load_invoice_head(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        replies = await _replies(conn, profile_id)

    assert head is not None
    assert head.state == "FETCHING_DOCS"
    assert jobs == ["fetch"]
    assert replies == [(REPROCESS_STARTED.format(id=invoice_id), None)]


async def test_reprocesar_on_a_failed_batch_ignores_the_interval(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D171: a BATCHED invoice starts no fetch round, so the brake does not apply to it."""
    profile_id = _profile()
    invoice_id = await _failed_batch_invoice(db_pool, profile_id)
    await _set_fetch_round(db_pool, invoice_id, minutes_ago=1)
    msg = _message(profile_id, f"/reprocesar {invoice_id}")
    context = CommandContext(admins=frozenset(), timezone=TIMEZONE, reprocess_min_interval_s=600)

    async with db_pool.connection() as conn:
        await handle_command(conn, msg, "reprocesar", str(invoice_id), context=context)
        head = await load_invoice_head(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        replies = await _replies(conn, profile_id)

    assert head is not None
    assert head.state == "DELIVERING"
    assert jobs == ["deliver"]
    assert replies == [(REPROCESS_STARTED.format(id=invoice_id), None)]


async def test_reprocesar_after_used_up_fetchers_runs_the_chain_again(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D96, D166: /reprocesar starts a new fetch round, so fetchers used up earlier run again."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, "MANUAL_REQUIRED", rule_id="BR-D02", flow=True)
    flow = load_config(EXAMPLE).flows[FLOW]
    for fetcher in flow.fetchers:
        # Two technical attempts use the fetcher up today and after D167's one retry is spent.
        for _ in range(2):
            async with db_pool.connection() as conn, conn.transaction():
                await record_attempt(conn, invoice_id, fetcher, None, "technical", 1)
    msg = _message(profile_id, f"/reprocesar {invoice_id}")

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        head = await load_invoice_head(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        attempts = await list_attempts(conn, invoice_id)
        replies = await _replies(conn, profile_id)

    assert result.status == "stored"
    assert head is not None
    assert head.state == "FETCHING_DOCS"
    assert jobs == ["fetch"]
    assert attempts == []
    assert replies == [(REPROCESS_STARTED.format(id=invoice_id), None)]
    assert decide(flow.fetchers, flow.fetchers, [], lambda _fetcher: (), no_retry=frozenset()) == Run(
        flow.fetchers[0]
    )


async def test_reprocesar_on_a_failed_batch_redelivers(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D96: /reprocesar on a BATCHED invoice with a failed batch queues deliver again."""
    profile_id = _profile()
    invoice_id = await _failed_batch_invoice(db_pool, profile_id)
    msg = _message(profile_id, f"/reprocesar {invoice_id}")

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        head = await load_invoice_head(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        replies = await _replies(conn, profile_id)

    assert result.status == "stored"
    assert head is not None
    assert head.state == "DELIVERING"
    assert batch_state == "failed"
    assert jobs == ["deliver"]
    assert replies == [(REPROCESS_STARTED.format(id=invoice_id), None)]


async def test_reprocesar_on_done_is_not_allowed(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D96: /reprocesar on a DONE invoice answers REPROCESS_NOT_ALLOWED and changes nothing."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, "DONE")
    msg = _message(profile_id, f"/reprocesar {invoice_id}")
    expected = REPROCESS_NOT_ALLOWED.format(id=invoice_id, state_desc=state_desc("DONE"))

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        head = await load_invoice_head(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        replies = await _replies(conn, profile_id)

    assert result.status == "stored"
    assert head is not None
    assert head.state == "DONE"
    assert jobs == []
    assert replies == [(expected, None)]


async def test_reprocesar_on_failed_without_a_flow_is_not_allowed(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D96: a FAILED invoice with no flow cannot restart its fetch."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, "FAILED", rule_id="BR-S03")
    msg = _message(profile_id, f"/reprocesar {invoice_id}")
    expected = REPROCESS_NOT_ALLOWED.format(id=invoice_id, state_desc=state_desc("FAILED"))

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        head = await load_invoice_head(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        replies = await _replies(conn, profile_id)

    assert result.status == "stored"
    assert head is not None
    assert head.state == "FAILED"
    assert jobs == []
    assert replies == [(expected, None)]


async def test_resume_delivery_sends_a_manual_invoice_back_to_deliver(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.9: resume_delivery moves a MANUAL_REQUIRED invoice to DELIVERING with one deliver job."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, "MANUAL_REQUIRED", rule_id="BR-D02", flow=True)

    async with db_pool.connection() as conn, conn.transaction():
        await resume_delivery(conn, invoice_id)
        head = await load_invoice_head(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)

    assert head is not None
    assert head.state == "DELIVERING"
    assert jobs == ["deliver"]


@pytest.mark.parametrize("command", ["cancelar", "reprocesar"])
async def test_cancel_and_reprocess_on_another_profiles_invoice_answer_br_s07(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    command: str,
) -> None:
    """BR-S07: another profile's id is refused for /cancelar and /reprocesar alike."""
    owner_profile = _profile()
    invoice_id = await _invoice_in(db_pool, owner_profile, "CONFIRMING", rule_id="BR-D06")
    sender_profile = _profile()
    msg = _message(sender_profile, f"/{command} {invoice_id}")

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        head = await load_invoice_head(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        replies = await _replies(conn, sender_profile)

    assert result.status == "stored"
    assert head is not None
    assert head.state == "CONFIRMING"
    assert jobs == []
    assert replies == [(MESSAGES["BR-S07"].format(id=invoice_id), "BR-S07")]


async def test_pendientes_with_nothing_waiting_answers_pending_none(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """S4.8: a profile with nothing pending gets PENDING_NONE."""
    profile_id = _profile()
    msg = _message(profile_id, "/pendientes")

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        replies = await _replies(conn, profile_id)

    assert result.status == "stored"
    assert replies == [(PENDING_NONE, None)]


async def test_pendientes_lists_a_parked_invoice_and_requeues_its_question(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """S4.8: a parked BR-R03 invoice is listed and its question is queued again with its buttons."""
    profile_id = _profile()
    invoice_id = await _parked_invoice(db_pool, profile_id, "BR-R03")
    await _set_amount(db_pool, invoice_id, "Ferretería El Sol", Decimal("45.50"))
    buttons = [
        Button(id=f"{invoice_id}:pick:0", title="Trabajo"),
        Button(id=f"{invoice_id}:cancel", title=BUTTON_CANCEL),
    ]
    await _question(db_pool, invoice_id, profile_id, "BR-R03", QUESTION_TEXT, buttons)
    await _backdate_questions(db_pool, invoice_id)
    msg = _message(profile_id, "/pendientes")
    line = PENDING_LINE.format(
        id=invoice_id,
        issuer="Ferretería El Sol",
        total="45.50",
        state_desc=state_desc("CONFIRMING"),
    )
    expected = f"{PENDING_HEADER}\n{line}"

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        replies = await _replies(conn, profile_id)
        payloads = await _invoice_payloads(conn, invoice_id)

    assert result.status == "stored"
    assert replies == [
        (QUESTION_TEXT, "BR-R03"),
        (expected, None),
        (QUESTION_TEXT, "BR-R03"),
    ]
    original, requeued = payloads
    assert original["buttons"] == requeued["buttons"]
    assert requeued["text"] == original["text"]


async def test_pendientes_requeues_a_question_at_most_every_ten_minutes(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """S4.8, D94: /pendientes re-sends a parked question once, but not again within 10 minutes."""
    profile_id = _profile()
    invoice_id = await _parked_invoice(db_pool, profile_id, "BR-R03")
    await _question(db_pool, invoice_id, profile_id, "BR-R03", QUESTION_TEXT)
    await _backdate_questions(db_pool, invoice_id)

    async with db_pool.connection() as conn:
        await handle_inbound(
            conn,
            _message(profile_id, "/pendientes"),
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
        )
        first = await _invoice_payloads(conn, invoice_id)
        await handle_inbound(
            conn,
            _message(profile_id, "/pendientes"),
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
        )
        second = await _invoice_payloads(conn, invoice_id)

    assert [payload.get("rule_id") for payload in first] == ["BR-R03", "BR-R03"]
    assert second == first

    await _backdate_questions(db_pool, invoice_id)

    async with db_pool.connection() as conn:
        await handle_inbound(
            conn,
            _message(profile_id, "/pendientes"),
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
        )
        third = await _invoice_payloads(conn, invoice_id)

    assert [payload.get("rule_id") for payload in third] == ["BR-R03", "BR-R03", "BR-R03"]


async def test_pendientes_lists_a_prepared_batch_older_than_the_reminder(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """S11.4: a batch prepared 3 days ago gets the batch line."""
    profile_id = _profile()
    invoice_id = await _prepared_invoice(
        db_pool, profile_id, days=3, razon_social="Ferretería El Sol", total=Decimal("45.50")
    )
    msg = _message(profile_id, "/pendientes")

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        replies = await _replies(conn, profile_id)
        prepared_at = (await _updated_at(conn, invoice_id)).astimezone(ZoneInfo(TIMEZONE)).date()

    line = PENDING_BATCH_LINE.format(
        id=invoice_id,
        issuer="Ferretería El Sol",
        prepared_at=prepared_at,
    )
    assert result.status == "stored"
    assert replies == [(f"{PENDING_HEADER}\n{line}", None)]


async def test_pendientes_lists_a_prepared_batch_in_the_owner_timezone(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """S11.4: a batch prepared at 03:30 UTC lists the previous day in America/Lima."""
    profile_id = _profile()
    invoice_id = await _prepared_invoice(
        db_pool, profile_id, days=3, razon_social="Ferretería El Sol", total=Decimal("45.50")
    )
    async with db_pool.connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE invoices SET updated_at = %s WHERE id = %s",
            (datetime(2024, 11, 14, 3, 30, tzinfo=UTC), invoice_id),
        )
    msg = _message(profile_id, "/pendientes")

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        replies = await _replies(conn, profile_id)

    line = PENDING_BATCH_LINE.format(
        id=invoice_id,
        issuer="Ferretería El Sol",
        prepared_at=date(2024, 11, 13),
    )
    assert result.status == "stored"
    assert replies == [(f"{PENDING_HEADER}\n{line}", None)]


async def test_pendientes_skips_a_prepared_batch_inside_the_reminder(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """S11.4: a batch prepared 1 day ago is not listed yet."""
    profile_id = _profile()
    await _prepared_invoice(
        db_pool, profile_id, days=1, razon_social="Ferretería El Sol", total=Decimal("45.50")
    )
    msg = _message(profile_id, "/pendientes")

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        replies = await _replies(conn, profile_id)

    assert result.status == "stored"
    assert replies == [(PENDING_NONE, None)]


async def test_pendientes_skips_another_profiles_invoices(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """S4.8: another profile's pending invoices are not listed."""
    other_profile = _profile()
    await _pending_invoices(db_pool, other_profile, 1)
    profile_id = _profile()
    msg = _message(profile_id, "/pendientes")

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        replies = await _replies(conn, profile_id)

    assert result.status == "stored"
    assert replies == [(PENDING_NONE, None)]


async def test_pendientes_caps_the_list_and_counts_the_rest(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """S11.4: 25 pending invoices list 20 lines and then ``…y 5 más.``"""
    profile_id = _profile()
    invoice_ids = await _pending_invoices(db_pool, profile_id, 25)
    msg = _message(profile_id, "/pendientes")
    lines = [
        PENDING_LINE.format(
            id=invoice_id,
            issuer=PENDING_NO_ISSUER,
            total="—",
            state_desc=state_desc("PENDING_USER"),
        )
        for invoice_id in invoice_ids[:20]
    ]
    expected = "\n".join([PENDING_HEADER, *lines, PENDING_MORE.format(count=5)])

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        replies = await _replies(conn, profile_id)

    assert result.status == "stored"
    assert replies == [(expected, None)]


# /portal (R-11.4)

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
#: The synthetic issuer RUC the command tests teach a link for.
ISSUER_RUC = 20100000001
ISSUER_RUC_TEXT = "20100000001"
GOOD_URL = "https://portal.example.pe/x"
PUBLIC_ADDRESS = "93.184.216.34"
SENDER = "asistente"


class FakeResolver(Resolver):
    """A ``Resolver`` that answers the fixed public address, whatever the host is."""

    async def resolve(self, host: str) -> list[str]:
        """Return the fixed public address."""
        return [PUBLIC_ADDRESS]


@pytest_asyncio.fixture(loop_scope="session")
async def clean_teach_tables(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> AsyncIterator[None]:
    """Start each test from empty taught links and outbox rows."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM taught_links")
        await conn.execute("DELETE FROM outbox")
    yield


def _portal_context(*, teach: bool = True) -> CommandContext:
    """Return a context with the example profiles, teaching wired when asked."""
    setup = TeachSetup(profiles=load_config(EXAMPLE).profiles, resolver=FakeResolver()) if teach else None
    return CommandContext(admins=frozenset(), timezone=TIMEZONE, reprocess_min_interval_s=0, teach=setup)


def _portal_message(text: str) -> InboundMessage:
    """Build a canonical message from the sender profile."""
    return InboundMessage(
        channel="whatsapp",
        external_id=f"ext-{uuid4().hex}",
        profile_id=SENDER,
        sender_role="owner",
        text=text,
        attachments=[],
        received_at=datetime.now(UTC),
    )


async def _portal_run(
    conn: AsyncConnection[Any],
    name: str,
    arg: str,
    context: CommandContext,
) -> None:
    """Run ``/name arg`` for the sender in the caller's transaction."""
    await handle_command(conn, _portal_message(f"/{name} {arg}".strip()), name, arg, context=context)


async def _portal_outbox(
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


async def _link_count(conn: AsyncConnection[Any]) -> int:
    """Return how many taught links exist."""
    cur = await conn.execute("SELECT count(*) FROM taught_links")
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("SELECT count over taught_links yielded no row")
    return int(row[0])


@pytest.mark.usefixtures("clean_teach_tables")
async def test_portal_proposes_the_link_and_asks_the_owner(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.4: a well-formed /portal stores a proposed link and queues BR-S06 to the sender."""
    async with db_pool.connection() as conn:
        await _portal_run(conn, "portal", f"{ISSUER_RUC_TEXT} {GOOD_URL}", _portal_context())
        link = await taught_links.get(conn, ISSUER_RUC)
        sender_rows = await _portal_outbox(conn, SENDER)

    assert link == TaughtLink(ISSUER_RUC, GOOD_URL, SENDER, "proposed")
    assert sender_rows == [(MESSAGES["BR-S06"], "BR-S06", [])]


@pytest.mark.usefixtures("clean_teach_tables")
async def test_portal_message_through_intake_stores_a_proposed_link(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.4: /portal through handle_inbound stores a proposed taught_links row."""
    setup = TeachSetup(profiles=load_config(EXAMPLE).profiles, resolver=FakeResolver())
    roles = IntakeRoles(teach=setup)
    msg = _portal_message(f"/portal {ISSUER_RUC_TEXT} {GOOD_URL}")

    async with db_pool.connection() as conn:
        await handle_inbound(
            conn,
            msg,
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
            roles=roles,
        )
        link = await taught_links.get(conn, ISSUER_RUC)

    assert link == TaughtLink(ISSUER_RUC, GOOD_URL, SENDER, "proposed")


@pytest.mark.usefixtures("clean_teach_tables")
async def test_portal_bad_ruc_answers_the_usage_line_and_stores_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.4: a RUC that is not 11 digits answers HELP_PORTAL and stores nothing."""
    async with db_pool.connection() as conn:
        await _portal_run(conn, "portal", f"123 {GOOD_URL}", _portal_context())
        sender_rows = await _portal_outbox(conn, SENDER)
        links = await _link_count(conn)

    assert sender_rows == [(HELP_PORTAL, None, [])]
    assert links == 0


@pytest.mark.usefixtures("clean_teach_tables")
async def test_portal_without_url_answers_the_usage_line(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.4: /portal with a RUC but no URL answers HELP_PORTAL."""
    async with db_pool.connection() as conn:
        await _portal_run(conn, "portal", ISSUER_RUC_TEXT, _portal_context())
        sender_rows = await _portal_outbox(conn, SENDER)

    assert sender_rows == [(HELP_PORTAL, None, [])]


@pytest.mark.usefixtures("clean_teach_tables")
async def test_ayuda_lists_the_portal_command(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-10.1: /ayuda lists /portal next to the other commands."""
    async with db_pool.connection() as conn:
        await _portal_run(conn, "ayuda", "", _portal_context())
        sender_rows = await _portal_outbox(conn, SENDER)

    assert sender_rows == [(help_text(False), None, [])]
    assert HELP_PORTAL in sender_rows[0][0]


@pytest.mark.usefixtures("clean_teach_tables")
async def test_portal_with_teaching_off_answers_the_help_and_stores_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-11.4: /portal with no TeachSetup answers the help and stores nothing."""
    async with db_pool.connection() as conn:
        await _portal_run(conn, "portal", f"{ISSUER_RUC_TEXT} {GOOD_URL}", _portal_context(teach=False))
        sender_rows = await _portal_outbox(conn, SENDER)
        links = await _link_count(conn)

    assert sender_rows == [(help_text(False), None, [])]
    assert links == 0


@pytest_asyncio.fixture
async def clean_gaps(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> AsyncIterator[None]:
    """Start a gap command test from empty gaps and invoice_gaps (S8.9, D139)."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM invoice_gaps")
        await conn.execute("DELETE FROM gaps")
    yield


async def _run_as(
    conn: AsyncConnection[Any],
    profile_id: str,
    text: str,
    *,
    is_admin: bool,
) -> list[tuple[str, str | None]]:
    """Run ``text`` for profile_id, admin or not, and return its replies (S8.9)."""
    admins = frozenset({profile_id}) if is_admin else frozenset()
    context = CommandContext(admins=admins, timezone=TIMEZONE, reprocess_min_interval_s=0)
    parsed = parse_command(text)
    assert parsed is not None
    name, arg = parsed
    await handle_command(conn, _message(profile_id, text), name, arg, context=context)
    return await _replies(conn, profile_id)


@pytest.mark.usefixtures("clean_gaps")
async def test_admin_gaps_lists_each_gap_with_its_issue_link(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """S8.9: an admin's /gaps lists two open gaps, the reported one with its issue link."""
    profile_id = _profile()
    reported_fp = f"portal_missing:{uuid4().hex}"
    open_fp = f"parser_miss:{uuid4().hex}"
    async with db_pool.connection() as conn, conn.transaction():
        await upsert_gap(conn, PORTAL_MISSING, reported_fp, GAP_SUBJECT, None)
        await upsert_gap(conn, PARSER_MISS, open_fp, GAP_SUBJECT, None)
        await mark_reported(conn, reported_fp, GAP_ISSUE_URL)
    async with db_pool.connection() as conn, conn.transaction():
        replies = await _run_as(conn, profile_id, "/gaps", is_admin=True)

    reported = GAPS_LINE.format(
        fingerprint=reported_fp,
        kind=PORTAL_MISSING.kind,
        subject=GAP_SUBJECT,
        count=1,
        issue=GAPS_ISSUE.format(issue_url=GAP_ISSUE_URL),
    )
    opened = GAPS_LINE.format(
        fingerprint=open_fp,
        kind=PARSER_MISS.kind,
        subject=GAP_SUBJECT,
        count=1,
        issue="",
    )
    lines = replies[0][0].splitlines()
    assert lines[0] == GAPS_HEADER
    assert set(lines[1:]) == {reported, opened}


@pytest.mark.usefixtures("clean_gaps")
async def test_gaps_with_none_open_answers_gaps_empty(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """S8.9: /gaps with no open gap answers GAPS_EMPTY."""
    profile_id = _profile()
    async with db_pool.connection() as conn, conn.transaction():
        replies = await _run_as(conn, profile_id, "/gaps", is_admin=True)

    assert replies == [(GAPS_EMPTY, None)]


@pytest.mark.usefixtures("clean_gaps")
async def test_gap_ignorar_hides_the_gap_from_the_next_listing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """S8.9: /gap ignorar <fp> ignores the gap and a second /gaps omits it."""
    profile_id = _profile()
    fingerprint = f"portal_missing:{uuid4().hex}"
    async with db_pool.connection() as conn, conn.transaction():
        await upsert_gap(conn, PORTAL_MISSING, fingerprint, GAP_SUBJECT, None)
    async with db_pool.connection() as conn, conn.transaction():
        ignored = await _run_as(conn, profile_id, f"/gap ignorar {fingerprint}", is_admin=True)
    async with db_pool.connection() as conn, conn.transaction():
        listed = await _run_as(conn, profile_id, "/gaps", is_admin=True)

    assert ignored == [(GAP_IGNORED.format(fingerprint=fingerprint), None)]
    assert listed[-1] == (GAPS_EMPTY, None)


@pytest.mark.usefixtures("clean_gaps")
async def test_non_admin_gaps_answers_like_an_unknown_command(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """S8.9: a non-admin's /gaps gets the same answer an unknown command gets."""
    profile_id = _profile()
    async with db_pool.connection() as conn, conn.transaction():
        replies = await _run_as(conn, profile_id, "/gaps", is_admin=False)

    assert replies == [(_help(), None)]


@pytest.mark.usefixtures("clean_gaps")
async def test_ayuda_lists_the_gap_commands_to_an_admin_only(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """S8.9: /ayuda shows the gap help lines to an admin and hides them from others."""
    admin_id = _profile()
    other_id = _profile()
    async with db_pool.connection() as conn, conn.transaction():
        admin_help = await _run_as(conn, admin_id, "/ayuda", is_admin=True)
        other_help = await _run_as(conn, other_id, "/ayuda", is_admin=False)

    assert HELP_GAPS in admin_help[0][0]
    assert HELP_GAP in admin_help[0][0]
    assert HELP_GAPS not in other_help[0][0]
    assert HELP_GAP not in other_help[0][0]
