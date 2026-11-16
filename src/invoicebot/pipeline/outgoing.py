"""The one function the pipeline uses to queue a chat message (R-1.8, R-11.1).

:func:`queue_message` writes the outbox row in the caller's transaction, so the
message exists exactly when the state change that produced it commits. It also
holds BR-S02's text: the button-reply path and the action path both answer a
stale press, and this module is the one they can share without a cycle.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from psycopg import AsyncConnection

from invoicebot.channels.base import Button, ChannelName, OutboundMessage
from invoicebot.config.models import Profile
from invoicebot.queue import outbox
from invoicebot.rules.messages_es import MESSAGES, TOKEN_REASON_DESC, state_desc
from invoicebot.store.ops_state import claim

#: Rule that tells the sender a button or override no longer applies (BR-S02).
RULE_STALE_BUTTON = "BR-S02"

#: BR-D05's alert ceiling: at most one per day.
TOKEN_ALERT_EVERY = timedelta(hours=24)

#: Rule and rate-limit key of the GitHub token alert (BR-D05).
RULE_GITHUB_ALERT = "BR-D05"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Addressee:
    """Who a message goes to: the channel and the profile, which is its own recipient id."""

    channel: ChannelName
    profile_id: str


async def queue_message(
    conn: AsyncConnection[Any],
    to: Addressee,
    text: str,
    *,
    invoice_id: int | None = None,
    rule_id: str | None = None,
    buttons: Sequence[Button] = (),
) -> int:
    """Queue one chat message in the caller's transaction and return its outbox id.

    ``invoice_id`` links the row to the invoice it talks about, so a parked
    invoice's question can be sent again (R-3.7). A profile is addressed by its
    own id, so ``recipient`` is ``profile_id``.
    """
    message = OutboundMessage(
        channel=to.channel,
        recipient=to.profile_id,
        profile_id=to.profile_id,
        rule_id=rule_id,
        text=text,
        buttons=list(buttons),
    )
    return await outbox.add(
        conn, to.channel, to.profile_id, message.model_dump(mode="json"), invoice_id=invoice_id
    )


async def queue_stale(
    conn: AsyncConnection[Any],
    to: Addressee,
    invoice_id: int,
    state: str,
) -> None:
    """Queue BR-S02 for a press or override the pipeline refuses (BR-S02).

    One home for BR-S02's text, shared by the button-reply and action paths.
    """
    await queue_message(
        conn,
        to,
        invoice_id=invoice_id,
        rule_id=RULE_STALE_BUTTON,
        text=MESSAGES[RULE_STALE_BUTTON].format(id=invoice_id, state_desc=state_desc(state)),
    )


async def queue_admin_alert(
    conn: AsyncConnection[Any],
    profiles: Mapping[str, Profile],
    rule_id: str,
    text: str,
) -> None:
    """Queue one rule's admin alert for every admin profile on its preferred channel."""
    for profile_id, profile in profiles.items():
        if profile.admin:
            await _notify_admin(conn, profile_id, profile, rule_id, text)


async def _notify_admin(
    conn: AsyncConnection[Any],
    profile_id: str,
    profile: Profile,
    rule_id: str,
    text: str,
) -> None:
    """Queue a rule's admin text for one admin profile on its preferred channel, if it has one."""
    channel = profile.preferred_channel
    if channel is None:
        # One unreachable admin must not undo the state change or the other alerts.
        logger.warning("admin alert skipped: no preferred_channel", extra={"rule_id": rule_id})
        return
    await queue_message(conn, Addressee(channel, profile_id), text, rule_id=rule_id)


async def queue_rate_limited_alert(
    conn: AsyncConnection[Any],
    profiles: Mapping[str, Profile],
    key: str,
    every: timedelta,
    rule_id: str,
    text: str,
) -> bool:
    """Queue a rule's admin alert only when its window elapsed, and report whether it did.

    The rate limit lives in ``ops_state`` and uses the database clock, so every
    process sharing the database sees the same window (BR-O01, BR-D05).
    """
    if not await claim(conn, key, every):
        return False
    await queue_admin_alert(conn, profiles, rule_id, text)
    return True


async def queue_token_error_alert(
    conn: AsyncConnection[Any],
    profiles: Mapping[str, Profile],
    repo: str,
    reason: str,
) -> bool:
    """Queue BR-D05's admin alert at most once a day (BR-D05)."""
    text = MESSAGES[RULE_GITHUB_ALERT].format(repo=repo, reason_desc=TOKEN_REASON_DESC[reason])
    return await queue_rate_limited_alert(
        conn, profiles, RULE_GITHUB_ALERT, TOKEN_ALERT_EVERY, RULE_GITHUB_ALERT, text
    )
