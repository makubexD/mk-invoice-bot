"""The status answer for one invoice, shared by ``/estado`` and the [Ver] button (S4.9, D188)."""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection

from invoicebot.queue import outbox
from invoicebot.rules.messages_es import STATUS, STATUS_RULE, state_desc
from invoicebot.store.invoices import get_state_rule


async def status_text(conn: AsyncConnection[Any], invoice_id: int) -> str:
    """Return the invoice's state and, when it waits on a rule, that rule's last text.

    The second line repeats the rule's newest stored question; an invoice whose rule left
    no stored text gets only the state line, and the rule id is never shown (S4.9, R-11.1,
    D189). The caller has already checked that the sender may see the invoice (R-10.1).

    Raises:
        RuntimeError: the invoice does not exist.
    """
    current = await get_state_rule(conn, invoice_id)
    if current is None:
        raise RuntimeError(f"invoice {invoice_id} does not exist")
    state, rule_id = current
    text = STATUS.format(id=invoice_id, state_desc=state_desc(state))
    if rule_id is None:
        return text
    last = await outbox.last_text(conn, invoice_id, rule_id)
    if last is None:
        return text
    return f"{text}\n{STATUS_RULE.format(text=last)}"
