"""Inbound message persistence and R-1.5 attachment-to-invoice matching."""

from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from invoicebot.channels.base import ChannelName, InboundMessage
from invoicebot.extract.fields import reading_from_json

# The states in which R-1.5 accepts a document for an invoice.
# shortcut: D101 — CONFIRMING joins when read-more lands.
OPEN_STATES = ("MANUAL_REQUIRED",)


async def store_inbound(conn: AsyncConnection[Any], msg: InboundMessage) -> bool:
    """Store msg once per (channel, external_id) and report whether it was new.

    R-1.8: a redelivered webhook is ignored rather than stored twice. The caller
    owns the transaction; this function never commits.
    """
    kind = msg.attachments[0].kind if msg.attachments else "text"
    cur = await conn.execute(
        """
        INSERT INTO messages (channel, external_id, profile_id, kind, received_at, payload_json)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (channel, external_id) DO NOTHING
        """,
        (
            msg.channel,
            msg.external_id,
            msg.profile_id,
            kind,
            msg.received_at,
            Jsonb(msg.model_dump(mode="json")),
        ),
    )
    return cur.rowcount == 1


async def link_message_to_invoice(
    conn: AsyncConnection[Any], channel: ChannelName, external_id: str, invoice_id: int
) -> None:
    """Point a stored message at the invoice it quotes, for later R-1.5 matching."""
    await conn.execute(
        "UPDATE messages SET invoice_id = %s WHERE channel = %s AND external_id = %s",
        (invoice_id, channel, external_id),
    )


async def find_open_invoice_for_attachment(
    conn: AsyncConnection[Any], profile_id: str, reply_to: tuple[ChannelName, str] | None
) -> int | None:
    """Find the invoice a document from profile_id belongs to (R-1.5, D102).

    The quoted invoice wins when it is still open and belongs to the same
    profile, on any channel. The quote may be of an inbound message or of a bot
    message stored in the outbox (D102). Without a resolved quote, the profile's
    open invoice matches only when it is the only one: with several open
    invoices nothing is guessed and ``None`` is returned so the sender can be
    asked.
    """
    if reply_to is not None:
        channel, external_id = reply_to
        invoice_id = await _find_open_invoice_for_inbound(conn, profile_id, channel, external_id)
        if invoice_id is None:
            invoice_id = await _find_open_invoice_for_outbox(conn, profile_id, channel, external_id)
        if invoice_id is not None:
            return invoice_id
    invoices = await list_open_invoices(conn, profile_id)
    return invoices[0][0] if len(invoices) == 1 else None


async def _find_open_invoice_for_inbound(
    conn: AsyncConnection[Any], profile_id: str, channel: ChannelName, external_id: str
) -> int | None:
    """Return the open invoice an inbound message was linked to, if any (R-1.5, D102)."""
    cur = await conn.execute(
        """
        SELECT i.id
        FROM messages AS m
        JOIN invoices AS i ON i.id = m.invoice_id
        WHERE m.channel = %s
          AND m.external_id = %s
          AND i.profile_id = %s
          AND i.state = ANY(%s)
        """,
        (channel, external_id, profile_id, list(OPEN_STATES)),
    )
    row = await cur.fetchone()
    return None if row is None else int(row[0])


async def _find_open_invoice_for_outbox(
    conn: AsyncConnection[Any], profile_id: str, channel: ChannelName, external_id: str
) -> int | None:
    """Return the open invoice a bot message was about, if any (R-1.5, D102)."""
    cur = await conn.execute(
        """
        SELECT i.id
        FROM outbox AS o
        JOIN invoices AS i ON i.id = o.invoice_id
        WHERE o.channel = %s
          AND o.channel_message_id = %s
          AND i.profile_id = %s
          AND i.state = ANY(%s)
        """,
        (channel, external_id, profile_id, list(OPEN_STATES)),
    )
    row = await cur.fetchone()
    return None if row is None else int(row[0])


async def list_open_invoices(
    conn: AsyncConnection[Any], profile_id: str
) -> list[tuple[int, str | None, str | None]]:
    """Return the profile's open invoices as ``(id, serie, numero)``, newest first (D102).

    ``serie`` and ``numero`` come from each invoice's stored reading and are
    ``None`` when the invoice has no reading or the reading lacks that field.
    The caller owns the transaction; this function never commits.
    """
    cur = await conn.execute(
        """
        SELECT id, fields_json
        FROM invoices
        WHERE profile_id = %s AND state = ANY(%s)
        ORDER BY created_at DESC, id DESC
        """,
        (profile_id, list(OPEN_STATES)),
    )
    invoices: list[tuple[int, str | None, str | None]] = []
    for invoice_id, fields_json in await cur.fetchall():
        reading = None if fields_json is None else reading_from_json(fields_json)
        invoices.append(
            (
                int(invoice_id),
                None if reading is None else reading.value("serie"),
                None if reading is None else reading.value("numero"),
            )
        )
    return invoices
