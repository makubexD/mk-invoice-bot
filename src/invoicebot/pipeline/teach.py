"""BR-S06: a portal link taught in chat waits for the owner's approval (R-10.2, R-11.4).

:func:`teach` validates the sender's link with the request guard, stores it as
``proposed`` and asks every admin to approve or reject it. An approved link is
never replaced, an invalid one repeats BR-F02, and a second proposal while one
is waiting thanks the sender without asking the owner again.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection

from invoicebot.channels.base import Button
from invoicebot.config.models import Profile
from invoicebot.net.guard import Resolver
from invoicebot.net.taught_url import TaughtUrlError, check_taught_url
from invoicebot.pipeline.outgoing import Addressee, queue_admin_alert, queue_message
from invoicebot.rules.messages_es import BUTTON_APPROVE, BUTTON_REJECT, MESSAGES, UNKNOWN_ISSUER
from invoicebot.store import taught_links

#: The sender's message and the rule that owns the proposal (BR-S06).
RULE_TEACH = "BR-S06"
#: The question an invalid link repeats (BR-F02, R-11.4).
RULE_ASK_PORTAL = "BR-F02"
#: An approved link is used as it is and is never replaced (BR-S06).
STATE_APPROVED = "approved"
#: BR-S06's button ids: ``t<ruc>:ok`` and ``t<ruc>:no`` (D118).
BUTTON_ID_PREFIX = "t"
BUTTON_OK_SUFFIX = "ok"
BUTTON_NO_SUFFIX = "no"


@dataclass(frozen=True)
class TeachSetup:
    """What teaching a link needs from the runtime: the admin profiles and the DNS resolver."""

    profiles: Mapping[str, Profile]
    resolver: Resolver


@dataclass(frozen=True)
class TeachContext:
    """Who teaches, who decides and how a taught link is validated."""

    profiles: Mapping[str, Profile]
    sender: Addressee
    resolver: Resolver


async def teach(
    conn: AsyncConnection[Any],
    ctx: TeachContext,
    ruc_emisor: int,
    razon_social: str | None,
    raw_url: str,
) -> None:
    """Propose a taught portal link and ask the owner to approve it (BR-S06).

    An approved link only thanks the sender; an invalid one repeats BR-F02.
    The caller owns the transaction, so the link and both messages commit together.
    """
    existing = await taught_links.get(conn, ruc_emisor)
    if existing is not None and existing.state == STATE_APPROVED:
        await queue_message(conn, ctx.sender, MESSAGES[RULE_TEACH], rule_id=RULE_TEACH)
        return
    try:
        url = await check_taught_url(raw_url, ctx.resolver)
    except TaughtUrlError:
        await queue_message(
            conn,
            ctx.sender,
            MESSAGES[RULE_ASK_PORTAL].format(
                razon_social=razon_social or UNKNOWN_ISSUER, ruc_emisor=ruc_emisor
            ),
            rule_id=RULE_ASK_PORTAL,
        )
        return
    proposed = await taught_links.propose(conn, ruc_emisor, url, ctx.sender.profile_id)
    await queue_message(conn, ctx.sender, MESSAGES[RULE_TEACH], rule_id=RULE_TEACH)
    if proposed:
        await queue_admin_alert(
            conn,
            ctx.profiles,
            RULE_TEACH,
            _owner_text(ctx, razon_social, ruc_emisor, url),
            buttons=_decision_buttons(ruc_emisor),
        )


def _owner_text(
    ctx: TeachContext,
    razon_social: str | None,
    ruc_emisor: int,
    url: str,
) -> str:
    """Return BR-S06's owner message for one proposal."""
    return MESSAGES["BR-S06_owner"].format(
        sender=ctx.profiles[ctx.sender.profile_id].name,
        razon_social=razon_social or UNKNOWN_ISSUER,
        ruc_emisor=ruc_emisor,
        url=url,
    )


def _decision_buttons(ruc_emisor: int) -> tuple[Button, Button]:
    """Return BR-S06's [Aprobar] and [Rechazar] buttons (D118)."""
    return (
        Button(id=f"{BUTTON_ID_PREFIX}{ruc_emisor}:{BUTTON_OK_SUFFIX}", title=BUTTON_APPROVE),
        Button(id=f"{BUTTON_ID_PREFIX}{ruc_emisor}:{BUTTON_NO_SUFFIX}", title=BUTTON_REJECT),
    )
