"""BR-D10's text for a filed and sent invoice (D195)."""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection

from invoicebot.rules.messages_es import DONE_RECIPIENT_UNKNOWN, MESSAGES
from invoicebot.store.flow_b import load_flow_data
from invoicebot.store.invoices import load_reading

#: The flow-data key that holds the mail's To addresses (D195).
MAIL_TO_KEY = "mail_to"


async def done_text(conn: AsyncConnection[Any], invoice_id: int) -> str:
    """Return BR-D10 for the invoice: its serie-numero, folder and the mail's recipients."""
    flow_data = await load_flow_data(conn, invoice_id)
    reading = await load_reading(conn, invoice_id)
    if reading is None:
        raise RuntimeError(f"invoice {invoice_id} has no reading to report as done")
    mail_to = flow_data.get(MAIL_TO_KEY)
    if isinstance(mail_to, list) and mail_to and all(isinstance(a, str) for a in mail_to):
        to = ", ".join(mail_to)
    else:
        to = DONE_RECIPIENT_UNKNOWN
    return MESSAGES["BR-D10"].format(
        serie=reading.value("serie"),
        numero=reading.value("numero"),
        folder=flow_data["folder"],
        to=to,
    )
