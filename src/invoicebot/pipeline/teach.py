"""BR-S06: a portal link taught in chat waits for the owner's approval (R-10.2, R-11.4).

:func:`teach` validates the sender's link with the request guard, stores it as
``proposed`` and asks every admin to approve or reject it. An approved link is
never replaced, an invalid one repeats BR-F02, and a newer, different link
replaces the one that waits and asks the admins again, while the same link again
only thanks the sender. :func:`handle_press` applies the owner's [Aprobar] or
[Rechazar] decision and puts an approved link on the gap's issue (D118); a press
on a replaced proposal answers BR-S06_stale (D192).
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
from invoicebot.gaps.kinds import GAP_KINDS, fingerprint
from invoicebot.net.guard import Resolver
from invoicebot.net.taught_url import check_taught_url
from invoicebot.pipeline.failed import RULE_DEAD
from invoicebot.pipeline.outgoing import (
    RULE_STALE_BUTTON,
    Addressee,
    queue_admin_alert,
    queue_message,
)
from invoicebot.pipeline.questions import RULE_DOCS_NEEDED
from invoicebot.queue import outbox
from invoicebot.rules.messages_es import BUTTON_APPROVE, BUTTON_REJECT, MESSAGES, UNKNOWN_ISSUER
from invoicebot.states import FETCHING_DOCS
from invoicebot.store import taught_links
from invoicebot.store.gaps import get_gap, set_hint
from invoicebot.store.invoices import load_invoice_head
from invoicebot.store.taught_links import TaughtLink

logger = logging.getLogger(__name__)

#: The sender's message and the rule that owns the proposal (BR-S06).
RULE_TEACH = "BR-S06"
#: The question an invalid link repeats (BR-F02, R-11.4).
RULE_ASK_PORTAL = "BR-F02"
#: The answer when the sender does not know the issuer's portal, while the invoice still
#: fetches its documents; also the rule of both answers (BR-F02, R-11.4, D190).
RULE_DONT_KNOW = "BR-F02_dont_know"
#: The answer when someone must add the issuer's portal by hand (BR-F02, D190).
MESSAGE_DONT_KNOW_MANUAL = "BR-F02_dont_know_manual"
#: Rules whose messages are never the question a typed answer or a BR-F14 press waits
#: on: BR-S02 answers a stale press, and BR-F13's buttons are gated by the invoice's
#: state, so a BR-F13 sent for another invoice must not hide a pending BR-F02 (D189).
#: BR-F02's "no sé" answer and BR-S03's notice ask nothing either, so a URL typed after
#: them still answers the pending BR-F02 (D190).
NOT_QUESTIONS = frozenset({RULE_STALE_BUTTON, RULE_DOCS_NEEDED, RULE_DONT_KNOW, RULE_DEAD})
#: The admin's answer after [Aprobar] (BR-S06, D118).
RULE_APPROVED = "BR-S06_approved"
#: The admin's answer after [Rechazar] (BR-S06, D118).
RULE_REJECTED = "BR-S06_rejected"
#: The pressing admin's answer when a newer link replaced the proposal (BR-S06, D192).
RULE_DECISION_STALE = "BR-S06_stale"
#: The proposer's notice when the owner approves their link (BR-S06, D190).
RULE_SENDER_APPROVED = "BR-S06_sender_approved"
#: The proposer's notice when the owner rejects their link (BR-S06, D190).
RULE_SENDER_REJECTED = "BR-S06_sender_rejected"
#: A link that waits for the owner's decision (BR-S06).
STATE_PROPOSED = "proposed"
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

    An approved link only thanks the sender; an invalid one raises ``TaughtUrlError``.
    A newer, different link replaces the one waiting for the owner and asks the admins
    again, while the same link again only thanks the sender. The caller owns the
    transaction, so the link and both messages commit together.
    """
    existing = await taught_links.get(conn, ruc_emisor)
    if existing is not None and existing.state == STATE_APPROVED:
        await queue_message(conn, ctx.sender, MESSAGES[RULE_TEACH], rule_id=RULE_TEACH)
        return
    url = await check_taught_url(raw_url, ctx.resolver)
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


async def dont_know_reply(
    conn: AsyncConnection[Any],
    addressee: Addressee,
    invoice_id: int,
) -> None:
    """Answer [No sé] with the text that is true for the invoice's state (BR-F02, D190).

    An invoice still fetching its documents answers ``BR-F02_dont_know``; in any other
    state someone must add the issuer's portal, so it answers ``BR-F02_dont_know_manual``.
    The caller owns the transaction.
    """
    head = await load_invoice_head(conn, invoice_id)
    key = RULE_DONT_KNOW if head is not None and head.state == FETCHING_DOCS else MESSAGE_DONT_KNOW_MANUAL
    await queue_message(
        conn,
        addressee,
        MESSAGES[key],
        invoice_id=invoice_id,
        rule_id=RULE_DONT_KNOW,
    )


async def handle_press(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    setup: TeachSetup,
) -> None:
    """Decide a proposed portal link from the owner's [Aprobar] or [Rechazar] (BR-S06, D118).

    Only an admin profile's press counts: a malformed id or any other profile is logged
    and changes nothing. A press on a proposal a newer link replaced answers
    BR-S06_stale; any other press with no proposed link at its version, such as a
    second press, changes nothing either. Approval stores the link as the
    PORTAL_MISSING gap's hint and puts it on the gap's issue, as a comment when the
    issue exists and in the pending ``create_issue`` row's fields when it does not.
    Every decision answers the pressing admin with BR-S06_approved or BR-S06_rejected,
    and tells the proposer when someone else pressed and the proposer has a preferred
    channel. The caller owns the transaction.
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
        await _answer_stale_press(conn, msg, ruc_emisor)
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
    await _notify_proposer(conn, setup, link, msg.profile_id, ruc_emisor, approved)


async def _answer_stale_press(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    ruc_emisor: int,
) -> None:
    """Tell the pressing admin a newer link replaced the proposal they pressed (BR-S06, D192).

    Only a link that still waits for the owner answers; a decided link, or none, stays
    silent as a second press always did. The caller owns the transaction.
    """
    link = await taught_links.get(conn, ruc_emisor)
    if link is None or link.state != STATE_PROPOSED:
        return
    await queue_message(
        conn,
        Addressee(msg.channel, msg.profile_id),
        MESSAGES[RULE_DECISION_STALE].format(ruc_emisor=ruc_emisor),
        rule_id=RULE_DECISION_STALE,
    )


async def _notify_proposer(
    conn: AsyncConnection[Any],
    setup: TeachSetup,
    link: TaughtLink,
    pressing_profile_id: str,
    ruc_emisor: int,
    approved: bool,
) -> None:
    """Tell a proposer someone else decided their link, if it has a preferred channel (BR-S06, D190).

    A proposer who pressed the button already got the decision, an unknown profile
    has no channel, and a profile without ``preferred_channel`` is unreachable, so
    each of those queues nothing. The caller owns the transaction.
    """
    if link.profile_id == pressing_profile_id:
        return
    proposer = setup.profiles.get(link.profile_id)
    channel = None if proposer is None else proposer.preferred_channel
    if channel is None:
        return
    sender_rule_id = RULE_SENDER_APPROVED if approved else RULE_SENDER_REJECTED
    await queue_message(
        conn,
        Addressee(channel, link.profile_id),
        MESSAGES[sender_rule_id].format(ruc_emisor=ruc_emisor),
        rule_id=sender_rule_id,
    )


async def _record_hint(
    conn: AsyncConnection[Any],
    setup: TeachSetup,
    ruc_emisor: int,
    url: str,
) -> None:
    """Store an approved link as the portal gap's hint and put it on the gap's issue (D118).

    One of three outcomes: a pending ``create_issue`` row takes the hint in its fields,
    so it reaches the issue body; a gap whose issue URL is known gets a ``comment`` row
    with that URL; otherwise nothing is queued and the hint waits on the gap for the
    next ``create_issue``.

    The outbox row comes first: the sender holds a ``create_issue`` row while its issue
    callback updates the gap, so locking the gap before waiting on that row would hang
    both on separate connections, where Postgres sees no deadlock. Once the wait ends,
    the gap read sees the issue URL the callback committed.
    """
    gap_fingerprint = fingerprint(GAP_KINDS["PORTAL_MISSING"].kind, str(ruc_emisor))
    hint_in_pending_issue = await outbox.set_pending_issue_hint(conn, gap_fingerprint, url) > 0
    await set_hint(conn, gap_fingerprint, url)
    gap = await get_gap(conn, gap_fingerprint)
    if hint_in_pending_issue or gap is None or gap.issue_url is None or setup.github_repo is None:
        return
    await outbox.add(
        conn,
        "github",
        setup.github_repo,
        {
            "op": "comment",
            "fingerprint": gap_fingerprint,
            "issue_url": gap.issue_url,
            "hint_url": url,
        },
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


def _decision_buttons(ruc_emisor: int, version: int) -> tuple[Button, Button]:
    """Return BR-S06's [Aprobar] and [Rechazar] buttons for one proposal version (D118, D137)."""
    base = f"{BUTTON_ID_PREFIX}{ruc_emisor}{BUTTON_VERSION_INFIX}{version}"
    return (
        Button(id=f"{base}:{BUTTON_OK_SUFFIX}", title=BUTTON_APPROVE),
        Button(id=f"{base}:{BUTTON_NO_SUFFIX}", title=BUTTON_REJECT),
    )
