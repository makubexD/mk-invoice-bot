"""Take the sender's typed captcha answer and resume the fetch with it (D209)."""

from __future__ import annotations

import re
from typing import Any

from psycopg import AsyncConnection

from invoicebot.channels.base import InboundMessage
from invoicebot.fetchers.browser.protocol import CAPTCHA_ANSWER_PATTERN
from invoicebot.pipeline.captcha import RULE_ASK, RULE_ASK_AGAIN
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.steps import FETCH_STEP
from invoicebot.pipeline.teach import NOT_QUESTIONS
from invoicebot.queue import outbox
from invoicebot.queue.jobs import enqueue
from invoicebot.rules.messages_es import MESSAGES
from invoicebot.states import FETCHING_DOCS, WAITING_CAPTCHA
from invoicebot.store import captchas
from invoicebot.store.invoices import load_invoice_head, set_state

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
    invoice keeps waiting. Returns True when the text was handled. The caller owns the
    transaction.
    """
    question = await outbox.last_question(conn, profile_id, NOT_QUESTIONS)
    if question is None or question[0] not in _ASKED_RULES or not msg.text:
        return False
    invoice_id = question[1]
    head = await load_invoice_head(conn, invoice_id)
    if head is None or head.state != WAITING_CAPTCHA:
        return False
    to = Addressee(msg.channel, profile_id)
    text = msg.text.strip()
    if re.fullmatch(CAPTCHA_ANSWER_PATTERN, text) is None:
        await queue_message(conn, to, MESSAGES[RULE_BAD_SHAPE], invoice_id=invoice_id, rule_id=RULE_BAD_SHAPE)
        return True
    if await captchas.set_answer(conn, invoice_id, text):
        await set_state(conn, invoice_id, FETCHING_DOCS, rule_id=RULE_ASK, expected=WAITING_CAPTCHA)
        await enqueue(conn, invoice_id, FETCH_STEP)
    else:
        await queue_message(conn, to, MESSAGES[RULE_EXPIRED], invoice_id=invoice_id, rule_id=RULE_EXPIRED)
    return True
