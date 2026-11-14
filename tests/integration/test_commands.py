"""Integration tests for chat commands, ``/ayuda`` and invoice scoping (R-10.1, BR-S07).

Commands are reached through :func:`handle_inbound` and :func:`resolve_target`, against
real Postgres; every test uses its own profile, so no outbox row leaks between them.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import InboundMessage
from invoicebot.pipeline.commands import COMMANDS, parse_command, resolve_target
from invoicebot.pipeline.intake import handle_inbound
from invoicebot.rules.messages_es import COMMAND_NEEDS_ID, HELP_AYUDA, HELP_HEADER, MESSAGES
from invoicebot.store.invoices import InvoiceHead, create_invoice

MAX_QUEUED = 10
#: An id no test creates, so BR-S07's unknown-id answer is deterministic.
UNKNOWN_INVOICE_ID = 999_999_999


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
