"""Integration tests for chat commands, ``/ayuda`` and invoice scoping (R-10.1, BR-S07).

Commands are reached through :func:`handle_inbound` and :func:`resolve_target`, against
real Postgres; every test uses its own profile, so no outbox row leaks between them.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import Button, InboundMessage
from invoicebot.pipeline.actions import cancel_invoice
from invoicebot.pipeline.commands import COMMANDS, parse_command, resolve_target
from invoicebot.pipeline.intake import handle_inbound
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.rules.messages_es import (
    BUTTON_CANCEL,
    CANCEL_LATE,
    CANCELLED_BY_USER,
    COMMAND_NEEDS_ID,
    HELP_AYUDA,
    HELP_HEADER,
    MESSAGES,
    PENDING_BATCH_LINE,
    PENDING_HEADER,
    PENDING_LINE,
    PENDING_MORE,
    PENDING_NO_ISSUER,
    PENDING_NONE,
    REPROCESS_NOT_ALLOWED,
    REPROCESS_STARTED,
    STATUS,
    STATUS_RULE,
    STATUS_RULE_ONLY,
    state_desc,
)
from invoicebot.states import StateMoved
from invoicebot.store.flow_b import get_batch_state, open_batch, set_batch_state
from invoicebot.store.invoices import (
    InvoiceHead,
    create_invoice,
    load_invoice_head,
    set_flow,
    set_state,
)

MAX_QUEUED = 10
#: An id no test creates, so BR-S07's unknown-id answer is deterministic.
UNKNOWN_INVOICE_ID = 999_999_999
#: The stored question text a BR-R03 invoice's ``/estado`` repeats (S4.9).
QUESTION_TEXT = "¿En qué flujo lo registro?"
#: The flow an invoice must have before ``/reprocesar`` restarts its fetch (D96).
FLOW = "work_microsoft"


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
    """Return the help text: the header and each command's line, in command order."""
    return "\n".join([HELP_HEADER, *(command.help for command in COMMANDS.values())])


async def test_ayuda_replies_with_the_command_list(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-10.1: /ayuda is stored and answered with the header and /ayuda's own line."""
    profile_id = _profile()
    msg = _message(profile_id, "/ayuda")

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
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
        await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        replies = await _replies(conn, profile_id)

    assert replies == [(_help(), None)]


async def test_unknown_command_answers_with_the_help(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-10.1: an unknown command name answers the same help as /ayuda."""
    profile_id = _profile()
    msg = _message(profile_id, "/foo")

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
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
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
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
        await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
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
        await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
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
        await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
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
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
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
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
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
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
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
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        head = await load_invoice_head(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        replies = await _replies(conn, profile_id)

    assert result.status == "stored"
    assert head is not None
    assert head.state == "FETCHING_DOCS"
    assert jobs == ["fetch"]
    assert replies == [(REPROCESS_STARTED.format(id=invoice_id), None)]


async def test_reprocesar_on_a_failed_batch_redelivers(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D96: /reprocesar on a BATCHED invoice with a failed batch queues deliver again."""
    profile_id = _profile()
    invoice_id = await _failed_batch_invoice(db_pool, profile_id)
    msg = _message(profile_id, f"/reprocesar {invoice_id}")

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
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
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
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
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        head = await load_invoice_head(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        replies = await _replies(conn, profile_id)

    assert result.status == "stored"
    assert head is not None
    assert head.state == "FAILED"
    assert jobs == []
    assert replies == [(expected, None)]


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
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
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
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
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
    msg = _message(profile_id, "/pendientes")
    line = PENDING_LINE.format(
        id=invoice_id,
        issuer="Ferretería El Sol",
        total="45.50",
        state_desc=state_desc("CONFIRMING"),
    )
    expected = f"{PENDING_HEADER}\n{line}"

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
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
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        replies = await _replies(conn, profile_id)
        prepared_at = await _updated_at(conn, invoice_id)

    line = PENDING_BATCH_LINE.format(
        id=invoice_id,
        issuer="Ferretería El Sol",
        prepared_at=prepared_at,
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
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
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
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
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
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        replies = await _replies(conn, profile_id)

    assert result.status == "stored"
    assert replies == [(expected, None)]
