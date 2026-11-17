"""BR-S06: a portal link taught in chat waits for the owner's approval (R-10.2, R-11.4).

:func:`teach` validates the sender's link with the request guard, stores it as
``proposed`` and asks every admin to approve or reject it. An approved link is
never replaced, an invalid one repeats BR-F02, and a second proposal while one
is waiting thanks the sender without asking the owner again. :func:`answer`
routes a free-text reply to BR-F02 (one ``https`` URL or "no sé") into
:func:`teach` or the ``BR-F02_dont_know`` answer (R-11.4). :func:`handle_press`
applies the owner's [Aprobar] or [Rechazar] decision and puts an approved link
on the gap's issue (D118).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection

from invoicebot.channels.base import Button, InboundMessage
from invoicebot.config.models import Profile
from invoicebot.gaps.kinds import fingerprint
from invoicebot.net.guard import Resolver
from invoicebot.net.taught_url import TaughtUrlError, check_taught_url, is_dont_know, looks_like_url
from invoicebot.pipeline.fetch import PORTAL_MISSING
from invoicebot.pipeline.outgoing import (
    RULE_STALE_BUTTON,
    Addressee,
    queue_admin_alert,
    queue_message,
)
from invoicebot.queue import outbox
from invoicebot.rules.messages_es import BUTTON_APPROVE, BUTTON_REJECT, MESSAGES, UNKNOWN_ISSUER
from invoicebot.store import taught_links
from invoicebot.store.gaps import get_gap, set_hint
from invoicebot.store.invoices import load_reading

logger = logging.getLogger(__name__)

#: The sender's message and the rule that owns the proposal (BR-S06).
RULE_TEACH = "BR-S06"
#: The question an invalid link repeats (BR-F02, R-11.4).
RULE_ASK_PORTAL = "BR-F02"
#: The answer when the sender does not know the issuer's portal (BR-F02, R-11.4).
RULE_DONT_KNOW = "BR-F02_dont_know"
#: The admin's answer after [Aprobar] (BR-S06, D118).
RULE_APPROVED = "BR-S06_approved"
#: The admin's answer after [Rechazar] (BR-S06, D118).
RULE_REJECTED = "BR-S06_rejected"
#: An approved link is used as it is and is never replaced (BR-S06).
STATE_APPROVED = "approved"
#: BR-S06's button ids: ``t<ruc>v<version>:ok`` and ``t<ruc>v<version>:no`` (D118, D137).
BUTTON_ID_PREFIX = "t"
BUTTON_VERSION_INFIX = "v"
BUTTON_OK_SUFFIX = "ok"
BUTTON_NO_SUFFIX = "no"
#: The whole id of a BR-S06 decision press: ``t<ruc>v<version>:ok`` or ``t<ruc>v<version>:no`` (D118, D137).
_PRESS_PATTERN = (
    rf"{BUTTON_ID_PREFIX}([0-9]{{11}}){BUTTON_VERSION_INFIX}([0-9]{{1,9}})"
    rf":({BUTTON_OK_SUFFIX}|{BUTTON_NO_SUFFIX})"
)


@dataclass(frozen=True)
class TeachSetup:
    """What teaching a link needs from the runtime: admin profiles, the resolver and the ops repo.

    ``github_repo`` is the issue tracker an approved link is commented onto; None means
    GitHub is disabled, so the link only reaches a pending ``create_issue`` row.
    """

    profiles: Mapping[str, Profile]
    resolver: Resolver
    github_repo: str | None = None


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
    if proposed is not None:
        await queue_admin_alert(
            conn,
            ctx.profiles,
            RULE_TEACH,
            _owner_text(ctx, razon_social, ruc_emisor, url),
            buttons=_decision_buttons(ruc_emisor, proposed),
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


async def handle_press(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    setup: TeachSetup,
) -> None:
    """Decide a proposed portal link from the owner's [Aprobar] or [Rechazar] (BR-S06, D118).

    Only an admin profile's press counts: a malformed id or any other profile is logged
    and changes nothing. A press with no proposed link, such as a second
    press, changes nothing either. Approval stores the link as the PORTAL_MISSING gap's
    hint and puts it on the gap's issue, as a comment when the issue exists and in the
    pending ``create_issue`` row's fields when it does not. Every decision answers the
    pressing admin with BR-S06_approved or BR-S06_rejected. The caller owns the
    transaction.
    """
    match = None if msg.button_id is None else re.fullmatch(_PRESS_PATTERN, msg.button_id)
    profile = setup.profiles.get(msg.profile_id)
    if match is None or profile is None or not profile.admin:
        logger.info("ignored BR-S06 press", extra={"profile_id": msg.profile_id})
        return
    ruc_emisor = int(match.group(1))
    version = int(match.group(2))
    approved = match.group(3) == BUTTON_OK_SUFFIX
    link = await taught_links.decide(conn, ruc_emisor, version, approved)
    if link is None:
        return
    if approved:
        await _record_hint(conn, setup, ruc_emisor, link.url)
    rule_id = RULE_APPROVED if approved else RULE_REJECTED
    await queue_message(
        conn,
        Addressee(msg.channel, msg.profile_id),
        MESSAGES[rule_id].format(ruc_emisor=ruc_emisor),
        rule_id=rule_id,
    )


async def _record_hint(
    conn: AsyncConnection[Any],
    setup: TeachSetup,
    ruc_emisor: int,
    url: str,
) -> None:
    """Store an approved link as the portal gap's hint and put it on the gap's issue (D118).

    A pending ``create_issue`` row takes the hint in its fields, so it reaches the issue
    body. When that row was already picked up, an enabled GitHub repo gets a ``comment``
    row instead, which resolves the issue at send time (D118).
    """
    gap_fingerprint = fingerprint(PORTAL_MISSING, str(ruc_emisor))
    await set_hint(conn, gap_fingerprint, url)
    gap = await get_gap(conn, gap_fingerprint)
    if gap is None:
        return
    payload: dict[str, str] = {"op": "comment", "fingerprint": gap_fingerprint, "hint_url": url}
    if gap.issue_url is not None:
        payload["issue_url"] = gap.issue_url
    elif await outbox.set_pending_issue_hint(conn, gap_fingerprint, url) > 0 or gap.raised_at is None:
        return
    if setup.github_repo is not None:
        await outbox.add(conn, "github", setup.github_repo, payload)


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


def _decision_buttons(ruc_emisor: int, version: int) -> tuple[Button, Button]:
    """Return BR-S06's [Aprobar] and [Rechazar] buttons for one proposal version (D118, D137)."""
    base = f"{BUTTON_ID_PREFIX}{ruc_emisor}{BUTTON_VERSION_INFIX}{version}"
    return (
        Button(id=f"{base}:{BUTTON_OK_SUFFIX}", title=BUTTON_APPROVE),
        Button(id=f"{base}:{BUTTON_NO_SUFFIX}", title=BUTTON_REJECT),
    )
