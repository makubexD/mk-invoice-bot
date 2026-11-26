"""BR-D10's text for a filed and sent invoice (D195)."""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection

from invoicebot.extract.fields import Reading
from invoicebot.rules.messages_es import DONE_RECIPIENT_UNKNOWN, MESSAGES
from invoicebot.store.flow_b import load_flow_data
from invoicebot.store.invoices import load_reading

#: BR-D10: the invoice is filed and its mail was accepted (spec 06, D195).
RULE_DONE = "BR-D10"

#: The flow-data key that holds the mail's To addresses (D195).
MAIL_TO_KEY = "mail_to"

#: The flow-data key that holds the [serie, numero] the mail was built from, XML gaps filled (R-7.4).
MAIL_INVOICE_KEY = "mail_invoice"


async def done_text(conn: AsyncConnection[Any], invoice_id: int) -> str:
    """Return BR-D10 for the invoice: its serie-numero, folder and the mail's recipients.

    The serie-numero is the one ``deliver`` saved with the mail; an older batch reads the photo.
    """
    flow_data = await load_flow_data(conn, invoice_id)
    reading = await load_reading(conn, invoice_id)
    if reading is None:
        raise RuntimeError(f"invoice {invoice_id} has no reading to report as done")
    mail_to = flow_data.get(MAIL_TO_KEY)
    if isinstance(mail_to, list) and mail_to and all(isinstance(a, str) for a in mail_to):
        to = ", ".join(mail_to)
    else:
        to = DONE_RECIPIENT_UNKNOWN
    serie, numero = _serie_numero(flow_data.get(MAIL_INVOICE_KEY), reading)
    return MESSAGES[RULE_DONE].format(serie=serie, numero=numero, folder=flow_data["folder"], to=to)


def _serie_numero(saved: object, reading: Reading) -> tuple[str | None, str | None]:
    """Return the saved [serie, numero] pair, or the photo's when none was saved (R-7.4)."""
    if isinstance(saved, list) and len(saved) == 2 and all(isinstance(v, str) for v in saved):
        return saved[0], saved[1]
    return reading.value("serie"), reading.value("numero")
