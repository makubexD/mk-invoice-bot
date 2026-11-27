"""Saves the summary's inputs and posts the routed-invoice summary once (D206).

D206 moves the summary from the validate step to the end of the fetch step, so the
sender gets one message after the files are looked for. :func:`save_summary_inputs`
keeps what validate knows (the routing rule and the SUNAT lines) in the invoice's flow
data; :func:`post_summary` builds the text from the current reading, queues it and marks
it ``posted`` so a retry or a later step never sends a second one.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from psycopg import AsyncConnection

from invoicebot.channels.base import Button
from invoicebot.config.models import Config
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.questions import summary_buttons
from invoicebot.pipeline.summary import flow_line, summary_text
from invoicebot.rules.messages_es import SUMMARY_MOVING, SUMMARY_MOVING_QUIET, SUMMARY_TAP_HINT
from invoicebot.states import EDITABLE
from invoicebot.store.clock import owner_today
from invoicebot.store.flow_b import load_flow_data, merge_flow_data
from invoicebot.store.invoices import load_flow, load_invoice_head, load_reading, lock_invoice

#: The flow-data key that holds the saved inputs and the ``posted`` mark (D206).
SUMMARY_KEY = "summary"


def moving_line(buttons: Sequence[Button]) -> str:
    """Return the summary's last line: it invites a press only when it carries buttons (D189)."""
    return SUMMARY_MOVING if buttons else SUMMARY_MOVING_QUIET


async def save_summary_inputs(
    conn: AsyncConnection[Any], invoice_id: int, *, rule_id: str, extra_lines: Sequence[str]
) -> None:
    """Save the rule and the extra lines the summary will carry; does not commit (D206)."""
    await merge_flow_data(
        conn, invoice_id, {SUMMARY_KEY: {"rule_id": rule_id, "extra_lines": list(extra_lines)}}
    )


async def discard_summary_inputs(conn: AsyncConnection[Any], invoice_id: int) -> None:
    """Drop saved inputs a new validation made stale, so no summary carries them; does not commit (D206)."""
    await merge_flow_data(conn, invoice_id, {SUMMARY_KEY: None})


async def post_summary(
    conn: AsyncConnection[Any], cfg: Config, invoice_id: int, *, found_lines: Sequence[str] = ()
) -> bool:
    """Queue the summary in the caller's transaction and return True, or return False (D206).

    Nothing is queued when no inputs were saved or the summary was already ``posted``.
    The invoice row is locked first, so two transactions never both post it. The buttons
    follow D189 only while an edit is still accepted: a summary that goes out with the move
    to ``DELIVERING`` carries none, since [Corregir] and [Anular] are valid only until
    delivery starts (R-3.4). With no ``found_lines`` the text ends with the moving line
    (D189); with them it ends with those lines and, when it has buttons, the tap hint.
    """
    await lock_invoice(conn, invoice_id)
    saved = (await load_flow_data(conn, invoice_id)).get(SUMMARY_KEY)
    if not isinstance(saved, dict) or saved.get("posted"):
        return False
    invoice = await load_invoice_head(conn, invoice_id)
    reading = await load_reading(conn, invoice_id)
    flow_id = await load_flow(conn, invoice_id)
    flow = None if flow_id is None else cfg.flows.get(flow_id)
    if invoice is None or reading is None or flow is None:
        raise RuntimeError(f"invoice {invoice_id} has no reading or routed flow")
    rule_id = str(saved["rule_id"])
    buttons = summary_buttons(invoice_id, reading) if invoice.state in EDITABLE else []
    tail = _tail(found_lines, buttons)
    text = summary_text(
        reading,
        flow_line(flow, rule_id),
        await owner_today(conn, cfg.owner.timezone),
        cfg.extraction,
        (*saved["extra_lines"], *tail),
    )
    await queue_message(
        conn,
        Addressee(invoice.origin_channel, invoice.profile_id),
        invoice_id=invoice_id,
        rule_id=rule_id,
        text=text,
        buttons=buttons,
    )
    await merge_flow_data(conn, invoice_id, {SUMMARY_KEY: {**saved, "posted": True}})
    return True


def _tail(found_lines: Sequence[str], buttons: Sequence[Button]) -> tuple[str, ...]:
    """Return the lines that close the summary (D206)."""
    if not found_lines:
        return (moving_line(buttons),)
    return (*found_lines, SUMMARY_TAP_HINT) if buttons else tuple(found_lines)
