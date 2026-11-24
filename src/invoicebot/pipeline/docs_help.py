"""The actions behind BR-F13's buttons (D189)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from psycopg import AsyncConnection

from invoicebot.channels.base import ButtonPress, InboundMessage
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.portal_offer import ask_portal
from invoicebot.pipeline.questions import RULE_DOCS_NEEDED
from invoicebot.rules.messages_es import MESSAGES
from invoicebot.states import FETCHING_DOCS, MANUAL_REQUIRED
from invoicebot.store.invoices import load_reading, set_state


async def send_pdf(
    conn: AsyncConnection[Any],
    invoice_id: int,
    msg: InboundMessage,
    _press: ButtonPress,
    _laptop_flows: frozenset[str],
) -> tuple[Path, ...]:
    """Move the invoice to MANUAL_REQUIRED and ask the sender for the PDF (BR-F13, D189)."""
    await set_state(conn, invoice_id, MANUAL_REQUIRED, rule_id=RULE_DOCS_NEEDED, expected=FETCHING_DOCS)
    await queue_message(
        conn,
        Addressee(msg.channel, msg.profile_id),
        MESSAGES["BR-F13_upload"],
        invoice_id=invoice_id,
        rule_id=RULE_DOCS_NEEDED,
    )
    return ()


async def wait_mail(
    conn: AsyncConnection[Any],
    invoice_id: int,
    msg: InboundMessage,
    _press: ButtonPress,
    _laptop_flows: frozenset[str],
) -> tuple[Path, ...]:
    """Tell the sender the bot keeps waiting for the mail, changing no state (BR-F13, D189)."""
    await queue_message(
        conn,
        Addressee(msg.channel, msg.profile_id),
        MESSAGES["BR-F13_wait"],
        invoice_id=invoice_id,
        rule_id=RULE_DOCS_NEEDED,
    )
    return ()


async def teach_portal(
    conn: AsyncConnection[Any],
    invoice_id: int,
    msg: InboundMessage,
    _press: ButtonPress,
    _laptop_flows: frozenset[str],
) -> tuple[Path, ...]:
    """Offer the issuer's portal as BR-F14, or ask for it as BR-F02 (BR-F13, BR-F14, BR-F02, D189)."""
    reading = await load_reading(conn, invoice_id)
    if reading is None:
        raise RuntimeError(f"invoice {invoice_id} has no reading")
    await ask_portal(conn, Addressee(msg.channel, msg.profile_id), invoice_id, reading)
    return ()
