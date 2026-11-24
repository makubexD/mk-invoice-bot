"""Park invoices whose question went unanswered and tell their senders (BR-S08, R-3.7).

A sweep moves every invoice that waited longer than the configured timeout from
``WAITING_FOR_USER`` to ``PENDING_USER`` and queues BR-S08 for it in the same
transaction, so a parked invoice never lacks its message.
"""

from __future__ import annotations

import logging

from invoicebot.channels.base import Button
from invoicebot.channels.buttons import command_button_id
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.rules.messages_es import BUTTON_PENDING, MESSAGES
from invoicebot.store.db import Pool
from invoicebot.store.invoices import RULE_PARKED, park_stale

logger = logging.getLogger(__name__)

#: Pause between two parking sweeps.
PARK_SWEEP_S = 300


async def park_stale_questions(pool: Pool, timeout_h: int) -> int:
    """Park every question older than ``timeout_h`` hours and queue BR-S08 (BR-S08).

    Returns the number of invoices parked. The state change and its message
    commit together, so a failed sweep parks nothing.
    """
    async with pool.connection() as conn, conn.transaction():
        refs = await park_stale(conn, timeout_h)
        for ref in refs:
            await queue_message(
                conn,
                Addressee(ref.origin_channel, ref.profile_id),
                MESSAGES[RULE_PARKED].format(id=ref.id),
                invoice_id=ref.id,
                rule_id=RULE_PARKED,
                buttons=[Button(id=command_button_id("pendientes"), title=BUTTON_PENDING)],
            )
    logger.info("invoices_parked", extra={"count": len(refs)})
    return len(refs)
