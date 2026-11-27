"""Take the sender's typed captcha answer and resume the fetch with it (D209)."""

from __future__ import annotations

import re
from typing import Any

from psycopg import AsyncConnection

from invoicebot.channels.base import InboundMessage
from invoicebot.fetchers.base import FetchReason
from invoicebot.fetchers.browser.protocol import CAPTCHA_ANSWER_PATTERN
from invoicebot.pipeline.captcha import RULE_ASK, RULE_ASK_AGAIN
from invoicebot.pipeline.captcha_timeout import RULE_ID as RULE_TIMED_OUT
from invoicebot.pipeline.manual_message import PORTAL_FETCHER
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.steps import FETCH_STEP
from invoicebot.pipeline.teach import NOT_QUESTIONS
from invoicebot.queue import outbox
from invoicebot.queue.jobs import enqueue
from invoicebot.rules.messages_es import MESSAGES
from invoicebot.states import FETCHING_DOCS, WAITING_CAPTCHA
from invoicebot.store import captchas
from invoicebot.store.fetch_attempts import list_attempts
from invoicebot.store.invoices import load_invoice_head, lock_invoice, set_state

#: BR-F19's text rule: the captcha already expired (D209).
RULE_EXPIRED = "BR-F19"
#: BR-F20's text when the typed answer is not letters and digits (D209).
RULE_BAD_SHAPE = "BR-F20_shape"
#: The newest question that still waits on an answer; a BR-F20_shape nudge keeps the ask open.
_ASKED_RULES = (RULE_ASK, RULE_ASK_AGAIN, RULE_BAD_SHAPE)


async def answer(conn: AsyncConnection[Any], profile_id: str, msg: InboundMessage) -> bool:
    """Store the text answering the sender's newest BR-F18 or BR-F20 captcha, and resume the fetch (D209).

    Only while that invoice is ``WAITING_CAPTCHA``. A text of one to twelve letters and
    digits is stored; a live ask moves the invoice to ``FETCHING_DOCS`` and queues the
    fetch step, an expired one gets BR-F19. Any other text gets BR-F20_shape and the
    invoice keeps waiting. After the timeout's BR-F05 a late answer of letters and digits
    gets BR-F19 too; any other text is not handled. Returns True when the text was
    handled. The caller owns the transaction.
    """
    question = await outbox.last_question(conn, profile_id, NOT_QUESTIONS)
    if question is None or not msg.text:
        return False
    rule_id, invoice_id = question
    if rule_id not in _ASKED_RULES and rule_id != RULE_TIMED_OUT:
        return False
    # The invoice lock first, as the timeout takes it, so the two never deadlock on the captcha row.
    await lock_invoice(conn, invoice_id)
    to = Addressee(msg.channel, profile_id)
    text = msg.text.strip()
    if rule_id == RULE_TIMED_OUT:
        return await _answer_late(conn, to, invoice_id, text)
    return await _answer_live(conn, to, invoice_id, text)


async def _answer_live(conn: AsyncConnection[Any], to: Addressee, invoice_id: int, text: str) -> bool:
    """Handle a text answering a captcha ask that still waits (BR-F18, BR-F20)."""
    head = await load_invoice_head(conn, invoice_id)
    if head is None or head.state != WAITING_CAPTCHA:
        return False
    if re.fullmatch(CAPTCHA_ANSWER_PATTERN, text) is None:
        await queue_message(conn, to, MESSAGES[RULE_BAD_SHAPE], invoice_id=invoice_id, rule_id=RULE_BAD_SHAPE)
        return True
    if await captchas.set_answer(conn, invoice_id, text):
        await set_state(conn, invoice_id, FETCHING_DOCS, rule_id=RULE_ASK, expected=WAITING_CAPTCHA)
        await enqueue(conn, invoice_id, FETCH_STEP)
    else:
        await _queue_expired(conn, to, invoice_id)
    return True


async def _answer_late(conn: AsyncConnection[Any], to: Addressee, invoice_id: int, text: str) -> bool:
    """Handle a captcha-looking text after the timeout's BR-F05: BR-F19, nothing else changes (D209)."""
    if re.fullmatch(CAPTCHA_ANSWER_PATTERN, text) is None:
        return False
    portal_reasons = [
        reason for fetcher, reason in await list_attempts(conn, invoice_id) if fetcher == PORTAL_FETCHER
    ]
    if not portal_reasons or portal_reasons[-1] != FetchReason.CAPTCHA_TIMEOUT:
        return False
    await _queue_expired(conn, to, invoice_id)
    return True


async def _queue_expired(conn: AsyncConnection[Any], to: Addressee, invoice_id: int) -> None:
    await queue_message(conn, to, MESSAGES[RULE_EXPIRED], invoice_id=invoice_id, rule_id=RULE_EXPIRED)
