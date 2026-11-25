"""Ask for the issuer's portal, offering a URL from the ticket first (BR-F14, BR-F02, BR-F15, D191)."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

from psycopg import AsyncConnection

from invoicebot.channels.base import InboundMessage
from invoicebot.extract.fields import Reading
from invoicebot.net.taught_url import MAX_TAUGHT_URL_CHARS, TaughtUrlError, check_taught_url
from invoicebot.pipeline.outgoing import Addressee, queue_message, queue_stale
from invoicebot.pipeline.portal_guess import repair_url
from invoicebot.pipeline.questions import (
    BUTTON_DONT_KNOW_SUFFIX,
    BUTTON_FIX_SUFFIX,
    BUTTON_YES_SUFFIX,
    ask_portal_buttons,
    dont_know_button,
    offer_buttons,
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
from invoicebot.rules.messages_es import MESSAGES, UNKNOWN_ISSUER
from invoicebot.store import portal_candidates, taught_links
from invoicebot.store.invoices import load_invoice_head, load_reading
from invoicebot.sunat_origins import is_sunat_host

logger = logging.getLogger(__name__)

#: The rule that owns BR-F14's offer (D189).
RULE_PORTAL_OFFER = "BR-F14"
#: The rule that asks the sender to type the portal's address (BR-F15, D191).
RULE_WRITE_PORTAL = "BR-F15"
#: BR-F15's message when the sender's address cannot be taught (BR-F15, D191).
MESSAGE_BR_F15_RETRY = "BR-F15_retry"
#: The questions each press answers when its rule is the invoice's newest (BR-F14, BR-F02, BR-F15, D191).
_ANSWERED_QUESTIONS = {
    BUTTON_YES_SUFFIX: (RULE_PORTAL_OFFER,),
    BUTTON_FIX_SUFFIX: (RULE_PORTAL_OFFER, RULE_ASK_PORTAL),
    BUTTON_DONT_KNOW_SUFFIX: (RULE_PORTAL_OFFER, RULE_ASK_PORTAL, RULE_WRITE_PORTAL),
}
#: The whole id of a BR-F14, BR-F02 or BR-F15 press: ``u<invoice_id>:yes|fix|dk`` (D191).
_PRESS_PATTERN = (
    rf"{OFFER_BUTTON_PREFIX}([0-9]{{1,9}})"
    rf":({BUTTON_YES_SUFFIX}|{BUTTON_FIX_SUFFIX}|{BUTTON_DONT_KNOW_SUFFIX})"
)


def offer_url(reading: Reading, known_hosts: frozenset[str]) -> str | None:
    """Return the URL BR-F14 offers: the printed one, the repaired strip, else the QR link (D191).

    The strip under ``CONSULT… EN`` is repaired against the approved hosts when it was
    read; one that repairs to nothing leaves the QR link to offer. A QR link on SUNAT's
    own host is the generic query page, not the issuer's portal, and one longer than a
    teachable URL could never be stored as the candidate, so neither is offered.
    """
    if reading.printed_url is not None:
        return reading.printed_url
    if reading.url_strip is not None:
        repaired = repair_url(reading.url_strip, reading.value("razon_social"), known_hosts)
        if repaired is not None:
            return repaired
    document_url = reading.document_url
    if document_url is None or len(document_url) > MAX_TAUGHT_URL_CHARS or _is_sunat_url(document_url):
        return None
    return document_url


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
    url = offer_url(reading, await taught_links.approved_hosts(conn))
    if url is None:
        await _ask_br_f02(conn, addressee, invoice_id, reading)
        return
    await offer_candidate(conn, addressee, invoice_id, reading, url)


async def offer_candidate(
    conn: AsyncConnection[Any],
    addressee: Addressee,
    invoice_id: int,
    reading: Reading,
    url: str,
) -> None:
    """Store ``url`` as the invoice's candidate, then offer it as BR-F14 (BR-F14, D191).

    The candidate is stored first so [Sí] teaches exactly the URL the sender saw.
    The caller owns the transaction.
    """
    await portal_candidates.save(conn, invoice_id, url)
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
        buttons=offer_buttons(invoice_id),
    )


async def handle_press(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    setup: TeachSetup,
) -> None:
    """Answer BR-F14's [Sí], [✏️ Corregir] or [No sé] for the newest question (BR-F14, BR-F02, D191).

    Only a press for the sender's own invoice counts, and only while the invoice's newest
    question is the one that press answers: [Sí] on BR-F14; [✏️ Corregir] on BR-F14 or
    BR-F02; [No sé] on BR-F14, BR-F02 or BR-F15. A press on the sender's own invoice whose
    question no longer applies is stale and answered BR-S02; anything else is logged and
    changes nothing. [Sí] teaches the offered URL through BR-S06, [✏️ Corregir] asks
    BR-F15, and [No sé] answers the text its state calls for. The caller owns the
    transaction.
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
    if question is None or question[0] not in _ANSWERED_QUESTIONS[action] or question[1] != invoice_id:
        await queue_stale(conn, addressee, invoice_id, head.state)
        return
    reading = await load_reading(conn, invoice_id)
    if reading is None:
        _log_ignored(msg)
        return
    if action == BUTTON_YES_SUFFIX:
        await _teach_offer(conn, setup, addressee, invoice_id, reading)
    elif action == BUTTON_FIX_SUFFIX:
        await _ask_br_f15(conn, addressee, invoice_id, reading)
    else:
        await dont_know_reply(conn, addressee, invoice_id)


async def _teach_offer(
    conn: AsyncConnection[Any],
    setup: TeachSetup,
    addressee: Addressee,
    invoice_id: int,
    reading: Reading,
) -> None:
    """Teach the candidate BR-F14 offered, asking BR-F15 again when it no longer passes (BR-S06, D191)."""
    url = await portal_candidates.get(conn, invoice_id)
    ruc_emisor = reading.value("ruc_emisor")
    if url is None or ruc_emisor is None:
        logger.info(
            "ignored BR-F14 offer without a URL or issuer", extra={"profile_id": addressee.profile_id}
        )
        return
    try:
        url = await check_taught_url(url, setup.resolver)
    except TaughtUrlError:
        await _ask_br_f15_retry(conn, addressee, invoice_id)
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
    """Ask BR-F02 with [✏️ Lo escribo] and [No sé], or queue nothing without a RUC (BR-F02, D191)."""
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
        buttons=ask_portal_buttons(invoice_id),
    )


async def _ask_br_f15(
    conn: AsyncConnection[Any],
    addressee: Addressee,
    invoice_id: int,
    reading: Reading,
) -> None:
    """Ask BR-F15 for the portal's address with a [No sé] button (BR-F15, D191)."""
    text = MESSAGES[RULE_WRITE_PORTAL].format(razon_social=reading.value("razon_social") or UNKNOWN_ISSUER)
    await queue_message(
        conn,
        addressee,
        text,
        invoice_id=invoice_id,
        rule_id=RULE_WRITE_PORTAL,
        buttons=[dont_know_button(invoice_id)],
    )


async def _ask_br_f15_retry(
    conn: AsyncConnection[Any],
    addressee: Addressee,
    invoice_id: int,
) -> None:
    """Ask BR-F15 again with a [No sé] button when the offered address was refused (BR-F15, D191)."""
    await queue_message(
        conn,
        addressee,
        MESSAGES[MESSAGE_BR_F15_RETRY],
        invoice_id=invoice_id,
        rule_id=RULE_WRITE_PORTAL,
        buttons=[dont_know_button(invoice_id)],
    )


def _is_sunat_url(url: str) -> bool:
    """Return whether ``url``'s host is SUNAT's or one of its subdomains (D189)."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    return host is not None and is_sunat_host(host)


def _log_ignored(msg: InboundMessage) -> None:
    """Log a BR-F14, BR-F02 or BR-F15 press that changes nothing (D191)."""
    logger.info("ignored portal press", extra={"profile_id": msg.profile_id})
