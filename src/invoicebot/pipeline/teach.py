"""BR-S06: a portal link taught in chat waits for the owner's approval (R-10.2, R-11.4).

:func:`teach` validates the sender's link with the request guard, stores it as
``proposed`` and asks every admin to approve or reject it. An approved link is
never replaced, an invalid one repeats BR-F02, and a second proposal while one
is waiting thanks the sender without asking the owner again. :func:`answer`
routes a free-text reply to BR-F02 (one ``https`` URL or "no sé") into
:func:`teach` or the ``BR-F02_dont_know`` answer (R-11.4).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection

from invoicebot.channels.base import Button, InboundMessage
from invoicebot.config.models import Profile
from invoicebot.net.guard import Resolver
from invoicebot.net.taught_url import TaughtUrlError, check_taught_url, is_dont_know, looks_like_url
from invoicebot.pipeline.outgoing import (
    RULE_STALE_BUTTON,
    Addressee,
    queue_admin_alert,
    queue_message,
)
from invoicebot.queue import outbox
from invoicebot.rules.messages_es import BUTTON_APPROVE, BUTTON_REJECT, MESSAGES, UNKNOWN_ISSUER
from invoicebot.store import taught_links
from invoicebot.store.invoices import load_reading

#: The sender's message and the rule that owns the proposal (BR-S06).
RULE_TEACH = "BR-S06"
#: The question an invalid link repeats (BR-F02, R-11.4).
RULE_ASK_PORTAL = "BR-F02"
#: The answer when the sender does not know the issuer's portal (BR-F02, R-11.4).
RULE_DONT_KNOW = "BR-F02_dont_know"
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


async def answer(
    conn: AsyncConnection[Any],
    setup: TeachSetup,
    msg: InboundMessage,
) -> bool:
    """Answer BR-F02 with the sender's free text, or do nothing and return False (R-11.4).

    Only a sender whose newest question is BR-F02 may answer it, and only with one
    ``https`` URL or "no sé". A URL proposes the link for that question's invoice;
    "no sé" stores nothing and answers BR-F02_dont_know. Returns True when the text
    was handled, so the caller does not run the usual edit/override paths.
    """
    question = await outbox.last_question(conn, msg.profile_id, RULE_STALE_BUTTON)
    if question is None or question[0] != RULE_ASK_PORTAL:
        return False
    if is_dont_know(msg.text):
        await queue_message(
            conn,
            Addressee(msg.channel, msg.profile_id),
            MESSAGES[RULE_DONT_KNOW],
            rule_id=RULE_DONT_KNOW,
        )
        return True
    if not looks_like_url(msg.text):
        return False
    reading = await load_reading(conn, question[1])
    if reading is None:
        return False
    ruc_emisor = reading.value("ruc_emisor")
    if ruc_emisor is None:
        return False
    await teach(
        conn,
        TeachContext(
            profiles=setup.profiles,
            sender=Addressee(msg.channel, msg.profile_id),
            resolver=setup.resolver,
        ),
        int(ruc_emisor),
        reading.value("razon_social"),
        msg.text,
    )
    return True


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
