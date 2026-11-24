"""Ask the sender for the issuer's portal, offering a URL from the ticket first (BR-F14, BR-F02, D189)."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

from psycopg import AsyncConnection

from invoicebot.channels.base import Button, InboundMessage
from invoicebot.extract.fields import Reading
from invoicebot.pipeline.outgoing import Addressee, queue_message, queue_stale
from invoicebot.pipeline.questions import (
    BUTTON_DONT_KNOW_SUFFIX,
    dont_know_button,
    offer_press_id,
)
from invoicebot.pipeline.questions import OFFER_BUTTON_PREFIX as OFFER_BUTTON_PREFIX
from invoicebot.pipeline.teach import (
    NOT_QUESTIONS,
    RULE_ASK_PORTAL,
    TeachContext,
    TeachSetup,
    dont_know_reply,
    teach,
)
from invoicebot.queue import outbox
from invoicebot.rules.messages_es import (
    BUTTON_NO,
    BUTTON_YES,
    MESSAGES,
    UNKNOWN_ISSUER,
)
from invoicebot.store.invoices import load_invoice_head, load_reading
from invoicebot.sunat_origins import is_sunat_host

logger = logging.getLogger(__name__)

#: The rule that owns BR-F14's offer (D189).
RULE_PORTAL_OFFER = "BR-F14"
BUTTON_YES_SUFFIX = "yes"
BUTTON_NO_SUFFIX = "no"
#: The whole id of a BR-F14 press: ``u<invoice_id>:yes``, ``u<invoice_id>:no`` or ``u<invoice_id>:dk``.
_PRESS_PATTERN = (
    rf"{OFFER_BUTTON_PREFIX}([0-9]{{1,9}})"
    rf":({BUTTON_YES_SUFFIX}|{BUTTON_NO_SUFFIX}|{BUTTON_DONT_KNOW_SUFFIX})"
)


def offer_url(reading: Reading) -> str | None:
    """Return the URL BR-F14 offers: the printed one, else a QR link outside SUNAT (D189).

    A QR link on SUNAT's own host is the generic query page, not the issuer's portal (D189).
    """
    if reading.printed_url is not None:
        return reading.printed_url
    if reading.document_url is None or _is_sunat_url(reading.document_url):
        return None
    return reading.document_url


async def ask_portal(
    conn: AsyncConnection[Any],
    addressee: Addressee,
    invoice_id: int,
    reading: Reading,
) -> None:
    """Offer the reading's URL as BR-F14, or ask BR-F02 when there is none (BR-F14, BR-F02).

    BR-F02's question is queued against ``invoice_id`` so the sender's typed answer
    reaches :func:`invoicebot.pipeline.teach.answer`. The caller owns the transaction.
    """
    url = offer_url(reading)
    if url is None:
        await _ask_br_f02(conn, addressee, invoice_id, reading)
        return
    text = MESSAGES[RULE_PORTAL_OFFER].format(
        razon_social=reading.value("razon_social") or UNKNOWN_ISSUER,
        url=url,
    )
    await queue_message(
        conn,
        addressee,
        text,
        invoice_id=invoice_id,
        rule_id=RULE_PORTAL_OFFER,
        buttons=_offer_buttons(invoice_id),
    )


async def handle_press(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    setup: TeachSetup,
) -> None:
    """Answer BR-F14's [Sí], [No] or [No sé] for the sender's newest question (BR-F14, BR-F02, D189, D190).

    Only a press for the sender's own invoice whose newest question is that invoice's
    BR-F14 counts; [No sé] also counts when the newest question is that invoice's BR-F02.
    A press on the sender's own invoice whose question no longer applies is stale and
    answered BR-S02; anything else is logged and changes nothing. [Sí] teaches the
    offered URL through BR-S06, [No] asks BR-F02, and [No sé] answers the text its
    state calls for. The caller owns the transaction.
    """
    match = None if msg.button_id is None else re.fullmatch(_PRESS_PATTERN, msg.button_id)
    if match is None:
        _log_ignored(msg)
        return
    invoice_id = int(match.group(1))
    head = await load_invoice_head(conn, invoice_id)
    if head is None or head.profile_id != msg.profile_id:
        _log_ignored(msg)
        return
    addressee = Addressee(msg.channel, msg.profile_id)
    question = await outbox.last_question(conn, msg.profile_id, NOT_QUESTIONS)
    action = match.group(2)
    answered = (
        (RULE_PORTAL_OFFER, RULE_ASK_PORTAL) if action == BUTTON_DONT_KNOW_SUFFIX else (RULE_PORTAL_OFFER,)
    )
    if question is None or question[0] not in answered or question[1] != invoice_id:
        await queue_stale(conn, addressee, invoice_id, head.state)
        return
    reading = await load_reading(conn, invoice_id)
    if reading is None:
        _log_ignored(msg)
        return
    if action == BUTTON_YES_SUFFIX:
        await _teach_offer(conn, setup, addressee, reading)
    elif action == BUTTON_NO_SUFFIX:
        await _ask_br_f02(conn, addressee, invoice_id, reading)
    else:
        await dont_know_reply(conn, addressee, invoice_id)


async def _teach_offer(
    conn: AsyncConnection[Any],
    setup: TeachSetup,
    addressee: Addressee,
    reading: Reading,
) -> None:
    """Teach the URL BR-F14 offered, recomputed from the stored reading (BR-S06, D189)."""
    url = offer_url(reading)
    ruc_emisor = reading.value("ruc_emisor")
    if url is None or ruc_emisor is None:
        logger.info(
            "ignored BR-F14 offer without a URL or issuer", extra={"profile_id": addressee.profile_id}
        )
        return
    await teach(
        conn,
        TeachContext(profiles=setup.profiles, sender=addressee, resolver=setup.resolver),
        int(ruc_emisor),
        reading.value("razon_social"),
        url,
    )


async def _ask_br_f02(
    conn: AsyncConnection[Any],
    addressee: Addressee,
    invoice_id: int,
    reading: Reading,
) -> None:
    """Ask BR-F02 for the issuer with a [No sé] button, or queue nothing without a RUC (BR-F02, D190)."""
    ruc_emisor = reading.value("ruc_emisor")
    if ruc_emisor is None:
        logger.info("ignored BR-F02 question without an issuer", extra={"profile_id": addressee.profile_id})
        return
    text = MESSAGES[RULE_ASK_PORTAL].format(
        razon_social=reading.value("razon_social") or UNKNOWN_ISSUER,
        ruc_emisor=ruc_emisor,
    )
    await queue_message(
        conn,
        addressee,
        text,
        invoice_id=invoice_id,
        rule_id=RULE_ASK_PORTAL,
        buttons=(dont_know_button(invoice_id),),
    )


def _offer_buttons(invoice_id: int) -> tuple[Button, Button, Button]:
    """Return BR-F14's [Sí], [No] and [No sé] buttons for one invoice (D189)."""
    return (
        Button(id=offer_press_id(invoice_id, BUTTON_YES_SUFFIX), title=BUTTON_YES),
        Button(id=offer_press_id(invoice_id, BUTTON_NO_SUFFIX), title=BUTTON_NO),
        dont_know_button(invoice_id),
    )


def _is_sunat_url(url: str) -> bool:
    """Return whether ``url``'s host is SUNAT's or one of its subdomains (D189)."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    return host is not None and is_sunat_host(host)


def _log_ignored(msg: InboundMessage) -> None:
    """Log a BR-F14 press that changes nothing (D189)."""
    logger.info("ignored BR-F14 press", extra={"profile_id": msg.profile_id})
