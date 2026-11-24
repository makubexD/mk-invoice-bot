"""The status answer for one invoice, shared by ``/estado`` and the [Ver] button (S4.9, D188)."""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection

from invoicebot.queue import outbox
from invoicebot.rules.messages_es import STATUS, STATUS_RULE, STATUS_RULE_ONLY, state_desc
from invoicebot.store.invoices import get_state_rule


async def status_text(conn: AsyncConnection[Any], invoice_id: int) -> str:
    """Return the invoice's state and, when it waits on a rule, that rule's last text.

    The second line names the rule and repeats its newest stored question, or just the
    rule when the invoice has no stored text (S4.9, R-11.1). The caller has already
    checked that the sender may see the invoice (R-10.1).

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
    line = (
        STATUS_RULE.format(rule_id=rule_id, text=last)
        if last is not None
        else STATUS_RULE_ONLY.format(rule_id=rule_id)
    )
    return f"{text}\n{line}"
