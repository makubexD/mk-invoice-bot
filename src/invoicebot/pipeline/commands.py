"""Chat commands: parse them, answer ``/ayuda`` and resolve the invoice they name (R-10).

A text starting with ``/`` is a command, handled before edits and overrides. A command
acts only on the sender profile's own invoices unless the sender is an admin; an id that
does not exist and one belonging to another profile get the same BR-S07 answer, so ids
cannot be probed (R-10.1). Everything here happens in the caller's transaction.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection

from invoicebot.channels.base import InboundMessage
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.rules.messages_es import (
    COMMAND_NEEDS_ID,
    HELP_AYUDA,
    HELP_HEADER,
    MESSAGES,
)
from invoicebot.store.invoices import InvoiceHead, load_invoice_head

#: Rule that answers an id the sender cannot see, whether it exists or not (BR-S07).
RULE_NOT_FOUND = "BR-S07"
#: The most digits an invoice id may have; any 18-digit number fits a BIGINT (R-12.8).
MAX_ID_DIGITS = 18

#: A command applied in the caller's transaction: its connection, sender, argument and
#: the admin profile ids (R-10.1).
type Handler = Callable[[AsyncConnection[Any], InboundMessage, str, frozenset[str]], Awaitable[None]]


@dataclass(frozen=True)
class Command:
    """One chat command: the handler it runs and the help line that describes it."""

    handler: Handler
    help: str


def parse_command(text: str) -> tuple[str, str] | None:
    """Return a leading ``/name`` and its argument, or None when the text is not a command.

    The name is lower-cased, so ``/AYUDA`` is ``/ayuda``; the argument is the rest of the
    text, stripped. A bare ``/`` names nothing and is not a command.
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped[1:].split(maxsplit=1)
    if not parts:
        return None
    return parts[0].lower(), parts[1].strip() if len(parts) == 2 else ""


async def handle_command(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    name: str,
    arg: str,
    *,
    admins: frozenset[str],
) -> None:
    """Run the named command in the caller's transaction; an unknown name answers the help."""
    command = COMMANDS.get(name)
    if command is None:
        await _reply(conn, msg, None, help_text())
        return
    await command.handler(conn, msg, arg, admins)


async def resolve_target(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    command: str,
    arg: str,
    admins: frozenset[str],
) -> tuple[int, InvoiceHead] | None:
    """Resolve the invoice a command names, queueing its refusal and returning None.

    ``arg`` is ``12`` or ``#12``; anything else asks for the id. An invoice that does not
    exist, or belongs to another profile while the sender is not an admin, gets the same
    BR-S07 answer (R-10.1).
    """
    invoice_id = _invoice_id(arg)
    if invoice_id is None:
        await _reply(conn, msg, None, COMMAND_NEEDS_ID.format(command=command))
        return None
    head = await load_invoice_head(conn, invoice_id)
    if head is None or (head.profile_id != msg.profile_id and msg.profile_id not in admins):
        await _reply(conn, msg, RULE_NOT_FOUND, MESSAGES[RULE_NOT_FOUND].format(id=invoice_id))
        return None
    return invoice_id, head


def help_text() -> str:
    """Return the help: the header followed by each command's line, in command order."""
    return "\n".join([HELP_HEADER, *(command.help for command in COMMANDS.values())])


def _invoice_id(arg: str) -> int | None:
    """Return the invoice id in ``arg`` (``12`` or ``#12``), or None when it is not one."""
    digits = arg.removeprefix("#")
    if not (digits.isascii() and digits.isdigit()) or len(digits) > MAX_ID_DIGITS:
        return None
    return int(digits)


async def _ayuda(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    _arg: str,
    _admins: frozenset[str],
) -> None:
    """Answer with every command and what it does (R-10.1)."""
    await _reply(conn, msg, None, help_text())


async def _reply(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    rule_id: str | None,
    text: str,
) -> None:
    """Queue ``text`` for the command's sender in the caller's transaction."""
    await queue_message(conn, Addressee(msg.channel, msg.profile_id), text, rule_id=rule_id)


#: The commands a text may name; the help lists them in this order.
COMMANDS: Mapping[str, Command] = {
    "ayuda": Command(_ayuda, HELP_AYUDA),
}
