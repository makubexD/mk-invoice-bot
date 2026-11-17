"""Parks an invoice until its account signs in (BR-D01).

A sink that needs a token the account has not granted yet raises ``AuthRequired``; the caller
hands the invoice here, in its own transaction, so it moves to ``PENDING_AUTH``, its sender gets
BR-D01 and the admins get BR-D01's reauthorization alert.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from psycopg import AsyncConnection

from invoicebot.config.models import Profile
from invoicebot.pipeline.outgoing import Addressee, queue_admin_alert, queue_message
from invoicebot.rules.messages_es import MESSAGES
from invoicebot.store.invoices import InvoiceHead, set_state

#: BR-D01: the rule that parks an invoice until the account signs in (R-7.9).
RULE_AUTH = "BR-D01"
#: BR-D01's admin-only alert, with the script the admin runs (BR-D01).
RULE_AUTH_ADMIN = "BR-D01_admin"
#: Where a missing token leaves the invoice until the account signs in (BR-D01).
PENDING_AUTH_STATE = "PENDING_AUTH"
#: The provider names the sender reads, by slug (BR-D01).
PROVIDER_NAMES: Mapping[str, str] = {"microsoft": "Microsoft", "google": "Google"}


async def park_for_auth(
    conn: AsyncConnection[Any],
    head: InvoiceHead,
    invoice_id: int,
    provider: str,
    account: str,
    admins: Mapping[str, Profile],
) -> None:
    """Move the invoice to ``PENDING_AUTH`` and alert its sender and admins (BR-D01).

    Runs in the caller's transaction and never commits.
    """
    await set_state(conn, invoice_id, PENDING_AUTH_STATE, rule_id=RULE_AUTH)
    await queue_message(
        conn,
        Addressee(head.origin_channel, head.profile_id),
        MESSAGES[RULE_AUTH].format(provider=PROVIDER_NAMES[provider]),
        invoice_id=invoice_id,
        rule_id=RULE_AUTH,
        buttons=[],
    )
    await queue_admin_alert(
        conn,
        admins,
        RULE_AUTH_ADMIN,
        MESSAGES[RULE_AUTH_ADMIN].format(provider=provider, account=account),
    )
