"""Inbound message persistence and R-1.5 attachment-to-invoice matching."""

from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from invoicebot.channels.base import ChannelName, InboundMessage

# The two states in which R-1.5 accepts a document for an invoice.
OPEN_STATES = ("MANUAL_REQUIRED", "CONFIRMING")


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
    """Find the invoice a document from profile_id belongs to (R-1.5).

    The quoted invoice wins when it is still open and belongs to the same
    profile; otherwise the profile's most recent open invoice does, on any
    channel. A quoted invoice of another profile never matches.
    """
    if reply_to is not None:
        channel, external_id = reply_to
        cur = await conn.execute(
            """
            SELECT i.id
            FROM messages AS m
            JOIN invoices AS i ON i.id = m.invoice_id
            WHERE m.channel = %s
              AND m.external_id = %s
              AND i.profile_id = %s
              AND i.state IN ('MANUAL_REQUIRED', 'CONFIRMING')
            """,
            (channel, external_id, profile_id),
        )
        row = await cur.fetchone()
        if row is not None:
            return int(row[0])
    cur = await conn.execute(
        """
        SELECT id
        FROM invoices
        WHERE profile_id = %s AND state IN ('MANUAL_REQUIRED', 'CONFIRMING')
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (profile_id,),
    )
    row = await cur.fetchone()
    return None if row is None else int(row[0])
