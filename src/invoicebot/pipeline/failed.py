"""Tell the sender and the admins that an invoice's job died (BR-S03, D190)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from psycopg import AsyncConnection

from invoicebot.channels.base import Button
from invoicebot.channels.buttons import command_button_id
from invoicebot.config.models import Profile
from invoicebot.pipeline.outgoing import Addressee, queue_admin_alert, queue_message
from invoicebot.rules.messages_es import BUTTON_RETRY, MESSAGES
from invoicebot.store.invoices import load_invoice_head

#: Rule that tells the sender the job died (BR-S03).
RULE_DEAD = "BR-S03"
#: Rule that alerts the admins about the dead job (BR-S03).
RULE_DEAD_ADMIN = "BR-S03_admin"
#: How many characters of the failure's first word the admin alert carries (BR-S03).
ERROR_CODE_MAX_CHARS = 40


async def notify_dead(
    conn: AsyncConnection[Any],
    invoice_id: int,
    step: str,
    error: str,
    profiles: Mapping[str, Profile],
) -> None:
    """Tell the sender and the admins that invoice_id's ``step`` job died (BR-S03, D190).

    Queues the sender's notice with [Reintentar] and the admins' alert in the
    caller's transaction; an unknown invoice queues nothing.
    """
    head = await load_invoice_head(conn, invoice_id)
    if head is None:
        return
    await queue_message(
        conn,
        Addressee(head.origin_channel, head.profile_id),
        MESSAGES[RULE_DEAD].format(id=invoice_id),
        invoice_id=invoice_id,
        rule_id=RULE_DEAD,
        buttons=[_retry_button(invoice_id)],
    )
    await queue_admin_alert(
        conn,
        profiles,
        RULE_DEAD_ADMIN,
        MESSAGES[RULE_DEAD_ADMIN].format(
            step=step,
            id=invoice_id,
            error_code=_error_code(error),
        ),
    )


def _retry_button(invoice_id: int) -> Button:
    """Return BR-S03's [Reintentar] button, which runs ``/reprocesar <invoice_id>`` (D190)."""
    return Button(id=command_button_id("reprocesar", str(invoice_id)), title=BUTTON_RETRY)


def _error_code(error: str) -> str:
    """Return the first word of ``error``, at most 40 characters (BR-S03)."""
    words = error.split(maxsplit=1)
    return words[0][:ERROR_CODE_MAX_CHARS] if words else ""
