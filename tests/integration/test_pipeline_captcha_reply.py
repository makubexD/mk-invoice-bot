"""Integration tests for the sender's typed captcha answer (D209, BR-F18, BR-F19, BR-F20)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import InboundMessage
from invoicebot.fetchers.browser.protocol import CaptchaForm
from invoicebot.pipeline.captcha_reply import answer
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.steps import FETCH_STEP
from invoicebot.rules.messages_es import MESSAGES
from invoicebot.store import captchas
from invoicebot.store.invoices import create_invoice, set_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

PORTAL_KEY = "synthetic_portal"
PORTAL_URL = "https://portal.ejemplo.pe/consulta"
SENDER = "asistente"
OTHER_RULE = "BR-F14"


def _form() -> CaptchaForm:
    return CaptchaForm(
        page_url=PORTAL_URL,
        action_url=f"{PORTAL_URL}/enviar",
        fields=(("serie", "F001"),),
        captcha_name="captcha",
        submit_name="ok",
        submit_value="Consultar",
    )


def _text(text: str) -> InboundMessage:
    return InboundMessage(
        channel="whatsapp",
        external_id=f"ext-{uuid4().hex}",
        profile_id=SENDER,
        sender_role="owner",
        text=text,
        received_at=datetime.now(UTC),
    )


async def _waiting(
    pool: AsyncConnectionPool[AsyncConnection[Any]], *, timeout_s: float = 300, rule: str = "BR-F18"
) -> int:
    """Create an invoice waiting on a captcha the sender was asked under ``rule``."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, f"reply-{uuid4().hex}", "whatsapp")
        await set_state(conn, invoice_id, "FETCHING_DOCS")
        await set_state(conn, invoice_id, "WAITING_CAPTCHA", rule_id="BR-F18", expected="FETCHING_DOCS")
        await captchas.put(conn, invoice_id, PORTAL_KEY, _form().model_dump(mode="json"), "x.png", timeout_s)
        await queue_message(
            conn, Addressee("whatsapp", SENDER), "captcha", invoice_id=invoice_id, rule_id=rule
        )
    return invoice_id


async def _answer(pool: AsyncConnectionPool[AsyncConnection[Any]], msg: InboundMessage) -> bool:
    async with pool.connection() as conn, conn.transaction():
        return await answer(conn, SENDER, msg)


async def _facts(
    pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int
) -> tuple[str, str | None, int, list[str]]:
    """Return state, stored answer, fetch job count and the invoice's outbox rule ids."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT state FROM invoices WHERE id = %s", (invoice_id,))
        row = await cur.fetchone()
        assert row is not None
        pending = await captchas.get(conn, invoice_id)
        cur = await conn.execute(
            "SELECT count(*) FROM jobs WHERE invoice_id = %s AND step = %s", (invoice_id, FETCH_STEP)
        )
        jobs = await cur.fetchone()
        assert jobs is not None
        cur = await conn.execute(
            "SELECT payload_json->>'rule_id' FROM outbox WHERE invoice_id = %s ORDER BY id", (invoice_id,)
        )
        rules = [str(r[0]) for r in await cur.fetchall()]
    return str(row[0]), None if pending is None else pending.answer, int(jobs[0]), rules


async def test_answer_letters_resume_the_fetch(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """D209: BR-F18's text of letters and digits is stored and the fetch step resumes."""
    invoice_id = await _waiting(db_pool)
    assert await _answer(db_pool, _text("  AB12 "))
    assert await _facts(db_pool, invoice_id) == ("FETCHING_DOCS", "AB12", 1, ["BR-F18"])


async def test_answer_after_a_refusal_resumes_too(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """D209: BR-F20's new image is answered like BR-F18's."""
    invoice_id = await _waiting(db_pool, rule="BR-F20")
    assert await _answer(db_pool, _text("zz9"))
    assert (await _facts(db_pool, invoice_id))[:3] == ("FETCHING_DOCS", "zz9", 1)


async def test_answer_of_the_wrong_shape_asks_again(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D209: a text that is not letters and digits sends BR-F20_shape and keeps waiting."""
    invoice_id = await _waiting(db_pool)
    assert await _answer(db_pool, _text("ab 12 x"))
    assert await _facts(db_pool, invoice_id) == ("WAITING_CAPTCHA", None, 0, ["BR-F18", "BR-F20_shape"])


async def test_answer_after_a_shape_nudge_resumes_the_fetch(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D209: after BR-F20_shape, the next text of letters and digits still answers the ask."""
    invoice_id = await _waiting(db_pool)
    assert await _answer(db_pool, _text("ab 12 x"))
    assert await _answer(db_pool, _text("AB12"))
    assert (await _facts(db_pool, invoice_id))[:3] == ("FETCHING_DOCS", "AB12", 1)


async def test_answer_after_expiry_sends_br_f19(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """D209: an answer to an expired ask gets BR-F19 and the state is unchanged."""
    invoice_id = await _waiting(db_pool, timeout_s=0)
    assert await _answer(db_pool, _text("AB12"))
    assert await _facts(db_pool, invoice_id) == ("WAITING_CAPTCHA", None, 0, ["BR-F18", "BR-F19"])
    async with db_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT payload_json->>'text' FROM outbox WHERE invoice_id = %s ORDER BY id DESC LIMIT 1",
            (invoice_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == MESSAGES["BR-F19"]


async def test_answer_when_another_question_is_newest_is_not_handled(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D209: a text is not a captcha answer when the newest question is another rule's."""
    invoice_id = await _waiting(db_pool, rule=OTHER_RULE)
    assert not await _answer(db_pool, _text("AB12"))
    assert await _facts(db_pool, invoice_id) == ("WAITING_CAPTCHA", None, 0, [OTHER_RULE])


async def test_answer_without_text_is_not_handled(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """D209: a photo has no text, so it is not a captcha answer."""
    invoice_id = await _waiting(db_pool)
    photo = _text("")
    assert not await _answer(db_pool, photo)
    assert (await _facts(db_pool, invoice_id))[:3] == ("WAITING_CAPTCHA", None, 0)
