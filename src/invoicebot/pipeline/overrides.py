"""Reason and description overrides from chat (R-7.4, D70, D90).

A ``motivo=…`` or ``descripcion=…`` line in a photo's caption or in a text message
overrides the Excel template's defaults. A text message goes to the invoice it quotes,
else to the sender's most recent invoice whose batch is not ``sent``. A later override
on a prepared batch moves the invoice back to ``DELIVERING`` so the deliver job rebuilds
the Excel and the draft in the same folder.
"""

from collections.abc import Mapping
from typing import Any

from psycopg import AsyncConnection

from invoicebot.channels.base import InboundMessage
from invoicebot.pipeline.actions import (
    BATCHED_STATE,
    DONE_STATE,
    PREPARED_BATCH_STATE,
    SENT_BATCH_STATE,
    send_stale,
)
from invoicebot.pipeline.deliver import DELIVER_STEP, DELIVERING_STATE
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.queue.jobs import enqueue
from invoicebot.rules.messages_es import OVERRIDES_SAVED
from invoicebot.store.flow_b import get_batch_state, load_flow_data, merge_flow_data
from invoicebot.store.invoices import find_override_target, get_state_rule, set_state
from invoicebot.text import has_unsafe_char

#: The longest value an override accepts (R-12.8's untrusted-text cap).
MAX_OVERRIDE_CHARS = 120
#: The only keys a sender may override (D70).
OVERRIDE_KEYS = frozenset({"motivo", "descripcion"})


def parse_overrides(text: str) -> dict[str, str]:
    """Return the overrides in text: ``motivo`` and ``descripcion``, keys case-insensitive.

    The value runs to the end of its line and is trimmed. A line without ``=``, a
    value longer than :data:`MAX_OVERRIDE_CHARS` or one with an unsafe character is
    ignored (that key is dropped), as is every other key.
    """
    overrides: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        name = key.strip().lower()
        if name not in OVERRIDE_KEYS:
            continue
        clean = value.strip()
        if not clean or len(clean) > MAX_OVERRIDE_CHARS or has_unsafe_char(clean):
            continue
        overrides[name] = clean
    return overrides


async def apply_overrides(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    overrides: Mapping[str, str],
) -> int | None:
    """Store overrides on their target invoice in the caller's transaction (D70, D90).

    The target is the quoted message's invoice of the same profile, else the profile's
    most recent invoice not ``CANCELLED``/``FAILED`` whose batch is not ``sent``. A target
    that is already ``DONE`` or whose batch is ``sent`` gets BR-S02 and changes nothing.
    Otherwise the overrides merge into the invoice's flow data; a ``BATCHED`` invoice
    with a ``prepared`` batch moves back to ``DELIVERING`` with a ``deliver`` job, and the
    sender gets a one-line confirmation. Returns the invoice id, or None with no target.
    """
    invoice_id = await edit_target(conn, msg)
    if invoice_id is None:
        return None
    current = await get_state_rule(conn, invoice_id)
    if current is None:
        raise RuntimeError(f"invoice {invoice_id} does not exist")
    state, _ = current
    batch_state = await get_batch_state(conn, invoice_id)
    if state == DONE_STATE or batch_state == SENT_BATCH_STATE:
        await send_stale(conn, msg, invoice_id)
        return invoice_id
    merged = {**_stored_overrides(await load_flow_data(conn, invoice_id)), **overrides}
    await merge_flow_data(conn, invoice_id, {"overrides": merged})
    if state == BATCHED_STATE and batch_state == PREPARED_BATCH_STATE:
        await set_state(conn, invoice_id, DELIVERING_STATE)
        await enqueue(conn, invoice_id, DELIVER_STEP)
    await _confirm(conn, msg, invoice_id)
    return invoice_id


async def edit_target(conn: AsyncConnection[Any], msg: InboundMessage) -> int | None:
    """Return the invoice the message's edit or override applies to, or None (D90, D97)."""
    reply_to = None if msg.reply_to_external_id is None else (msg.channel, msg.reply_to_external_id)
    return await find_override_target(conn, msg.profile_id, reply_to)


def _stored_overrides(flow_data: Mapping[str, Any]) -> dict[str, str]:
    """Return the invoice's existing overrides so a later one adds to them (R-7.4)."""
    raw = flow_data.get("overrides")
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


async def _confirm(conn: AsyncConnection[Any], msg: InboundMessage, invoice_id: int) -> None:
    """Queue the one-line confirmation for an override in the caller's transaction."""
    await queue_message(
        conn,
        Addressee(msg.channel, msg.profile_id),
        OVERRIDES_SAVED.format(id=invoice_id),
        invoice_id=invoice_id,
    )
