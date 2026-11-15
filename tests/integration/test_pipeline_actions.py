"""Integration tests for button actions and their intake path (R-1.8, R-3.5, R-7.8, R-7.13)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import InboundMessage
from invoicebot.channels.whatsapp.payloads import button_id
from invoicebot.pipeline.intake import handle_inbound
from invoicebot.rules.messages_es import CANCELLED_BY_USER, EDIT_PROMPT, MESSAGES, state_desc
from invoicebot.store.flow_b import get_batch_state, load_flow_data, merge_flow_data, open_batch
from invoicebot.store.invoices import FileRecord, add_file, create_invoice, get_file_path, set_flow, set_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

MAX_QUEUED = 10
FLOW = "work_microsoft"
CONFIRMING = "CONFIRMING"
ROUTING = "ROUTING"
PENDING_USER = "PENDING_USER"
FETCHING_DOCS = "FETCHING_DOCS"
DELIVERING = "DELIVERING"
BATCHED = "BATCHED"
DONE = "DONE"
RULE_ASK = "BR-R03"
RULE_BUYER = "BR-R04"
RULE_BRAND = "BR-R09"
RULE_CAP = "BR-D06"
RULE_BATCHED = "BR-D07"
RULE_SENT = "BR-D08"
FLOW_CHOICES = ("flow_a", "flow_b")


def _profile() -> str:
    """Return a profile id no other test uses, so tests cannot interfere."""
    return f"press-{uuid4().hex}"


def _press(
    invoice_id: int,
    action: str,
    profile_id: str,
    choice: int | None = None,
    menu: int | None = None,
) -> InboundMessage:
    """Build a canonical button press for the invoice, on menu 0 unless another is given."""
    if choice is not None and menu is None:
        menu = 0
    return InboundMessage(
        channel="whatsapp",
        external_id=f"ext-{uuid4().hex}",
        profile_id=profile_id,
        sender_role="owner",
        button_id=button_id(invoice_id, action, choice, menu),
        received_at=datetime.now(UTC),
    )


async def _confirming_invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    rule_id: str,
    *,
    choices: tuple[str, ...] | None = None,
    menu: int = 0,
    answers: dict[str, Any] | None = None,
) -> int:
    """Create a CONFIRMING invoice waiting on rule_id, with the offered choices and menu."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await set_flow(conn, invoice_id, FLOW)
        flow_data: dict[str, Any] = {}
        if choices is not None:
            flow_data["choices"] = list(choices)
            flow_data["menu"] = menu
        if answers is not None:
            flow_data["answers"] = answers
        if flow_data:
            await merge_flow_data(conn, invoice_id, flow_data)
        await set_state(conn, invoice_id, CONFIRMING, rule_id=rule_id)
    return invoice_id


async def _park(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    invoice_id: int,
    waiting_state: str,
    waiting_rule_id: str,
) -> None:
    """Move the invoice to PENDING_USER, storing the state and rule it waited in (D94)."""
    async with pool.connection() as conn, conn.transaction():
        await set_state(conn, invoice_id, PENDING_USER, rule_id="BR-S08")
        await conn.execute(
            "UPDATE invoices SET waiting_state = %s, waiting_rule_id = %s WHERE id = %s",
            (waiting_state, waiting_rule_id, invoice_id),
        )


async def _invoice_in(pool: AsyncConnectionPool[AsyncConnection[Any]], profile_id: str, state: str) -> int:
    """Create an invoice in state, which needs no rule id."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await set_flow(conn, invoice_id, FLOW)
        await set_state(conn, invoice_id, state)
    return invoice_id


async def _waiting_invoice(pool: AsyncConnectionPool[AsyncConnection[Any]], profile_id: str) -> int:
    """Create a BR-D06 invoice in CONFIRMING and return its id."""
    return await _confirming_invoice(pool, profile_id, RULE_CAP)


async def _batched_invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    tmp_path: Path,
) -> tuple[int, Path]:
    """Create a BATCHED invoice with a prepared batch and one synthetic photo."""
    photo = tmp_path / f"{uuid4().hex}.jpg"
    photo.write_bytes(b"\xff\xd8\xff synthetic photo\n")
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await set_flow(conn, invoice_id, FLOW)
        await set_state(conn, invoice_id, BATCHED, rule_id=RULE_BATCHED)
        await open_batch(conn, invoice_id, FLOW, "prepared")
        await add_file(
            conn,
            invoice_id,
            FileRecord(
                kind="photo",
                local_path=str(photo),
                remote_ref=None,
                sha256=hashlib.sha256(photo.read_bytes()).hexdigest(),
                size=photo.stat().st_size,
            ),
        )
    return invoice_id, photo


async def _invoice_row(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str, str | None]:
    """Return the invoice's ``(state, rule_id)``."""
    cur = await conn.execute("SELECT state, rule_id FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0]), None if row[1] is None else str(row[1])


async def _waiting_row(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str | None, str | None]:
    """Return the invoice's ``(waiting_state, waiting_rule_id)``."""
    cur = await conn.execute(
        "SELECT waiting_state, waiting_rule_id FROM invoices WHERE id = %s",
        (invoice_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0], row[1]


async def _jobs(conn: AsyncConnection[Any], invoice_id: int) -> list[str]:
    """Return the steps of the invoice's queued jobs, in insertion order."""
    cur = await conn.execute("SELECT step FROM jobs WHERE invoice_id = %s ORDER BY id", (invoice_id,))
    return [str(step) for (step,) in await cur.fetchall()]


async def _texts(conn: AsyncConnection[Any], profile_id: str) -> list[tuple[str | None, str]]:
    """Return the outbox's ``(rule_id, text)`` rows for profile_id, oldest first."""
    cur = await conn.execute(
        """
        SELECT payload_json->>'rule_id', payload_json->>'text'
        FROM outbox
        WHERE "to" = %s
        ORDER BY id
        """,
        (profile_id,),
    )
    return [(rule_id, str(text)) for rule_id, text in await cur.fetchall()]


async def test_continue_stores_the_cap_answer_and_reroutes(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-D06: [Continuar] stores cap_ok, sets ROUTING and queues a route job."""
    profile_id = _profile()
    invoice_id = await _waiting_invoice(db_pool, profile_id)
    msg = _press(invoice_id, "continue", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert flow_data["answers"]["cap_ok"] is True
    assert invoice == (ROUTING, RULE_CAP)
    assert jobs == ["route"]
    assert texts == []


async def test_pick_stores_the_chosen_flow_and_reroutes(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-R03: [pick] choice 1 stores the second candidate, sets ROUTING and queues a route job."""
    profile_id = _profile()
    invoice_id = await _confirming_invoice(db_pool, profile_id, RULE_ASK, choices=FLOW_CHOICES)
    msg = _press(invoice_id, "pick", profile_id, choice=1)

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert flow_data["answers"]["flow"] == FLOW_CHOICES[1]
    assert invoice == (ROUTING, RULE_ASK)
    assert jobs == ["route"]
    assert texts == []


async def test_pick_on_a_buyer_question_is_stale(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S02: [pick] on a BR-R04 invoice answers BR-S02 and changes nothing."""
    profile_id = _profile()
    invoice_id = await _confirming_invoice(db_pool, profile_id, RULE_BUYER)
    msg = _press(invoice_id, "pick", profile_id, choice=0)

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert invoice == (CONFIRMING, RULE_BUYER)
    assert "answers" not in flow_data
    assert jobs == []
    assert texts == [("BR-S02", MESSAGES["BR-S02"].format(id=invoice_id, state_desc=state_desc(CONFIRMING)))]


async def test_pick_on_a_parked_br_r03_invoice_resumes_and_reroutes(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D94: a [pick] on an invoice parked from BR-R03 unparks it and routes with answers.flow."""
    profile_id = _profile()
    invoice_id = await _confirming_invoice(db_pool, profile_id, RULE_ASK, choices=FLOW_CHOICES)
    await _park(db_pool, invoice_id, CONFIRMING, RULE_ASK)
    msg = _press(invoice_id, "pick", profile_id, choice=1)

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        waiting = await _waiting_row(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert flow_data["answers"]["flow"] == FLOW_CHOICES[1]
    assert invoice == (ROUTING, RULE_ASK)
    assert waiting == (None, None)
    assert jobs == ["route"]
    assert texts == []


async def test_pick_out_of_range_on_a_parked_br_r03_invoice_stays_parked(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D94, BR-S02: an out-of-range [pick] on an invoice parked from BR-R03 leaves it parked."""
    profile_id = _profile()
    invoice_id = await _confirming_invoice(db_pool, profile_id, RULE_ASK, choices=FLOW_CHOICES)
    await _park(db_pool, invoice_id, CONFIRMING, RULE_ASK)
    msg = _press(invoice_id, "pick", profile_id, choice=7)

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        waiting = await _waiting_row(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id is None
    assert invoice == (PENDING_USER, "BR-S08")
    assert waiting == (CONFIRMING, RULE_ASK)
    assert "answers" not in flow_data
    assert jobs == []
    assert texts == [
        ("BR-S02", MESSAGES["BR-S02"].format(id=invoice_id, state_desc=state_desc(PENDING_USER)))
    ]


async def test_pick_from_another_menu_on_a_parked_br_r03_invoice_stays_parked(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D94, BR-S02: a [pick] from another menu on a parked BR-R03 invoice leaves it parked."""
    profile_id = _profile()
    invoice_id = await _confirming_invoice(db_pool, profile_id, RULE_ASK, choices=FLOW_CHOICES)
    await _park(db_pool, invoice_id, CONFIRMING, RULE_ASK)
    msg = _press(invoice_id, "pick", profile_id, choice=0, menu=5)

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        waiting = await _waiting_row(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id is None
    assert invoice == (PENDING_USER, "BR-S08")
    assert waiting == (CONFIRMING, RULE_ASK)
    assert "answers" not in flow_data
    assert jobs == []
    assert texts == [
        ("BR-S02", MESSAGES["BR-S02"].format(id=invoice_id, state_desc=state_desc(PENDING_USER)))
    ]


async def test_pick_in_range_on_a_parked_br_r03_invoice_resumes(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D94, BR-S02: an in-range [pick] on a parked BR-R03 invoice resumes it and stores the flow."""
    profile_id = _profile()
    invoice_id = await _confirming_invoice(db_pool, profile_id, RULE_ASK, choices=FLOW_CHOICES)
    await _park(db_pool, invoice_id, CONFIRMING, RULE_ASK)
    msg = _press(invoice_id, "pick", profile_id, choice=0)

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        waiting = await _waiting_row(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert flow_data["answers"]["flow"] == FLOW_CHOICES[0]
    assert invoice == (ROUTING, RULE_ASK)
    assert waiting == (None, None)
    assert jobs == ["route"]


async def test_yes_on_a_parked_br_r03_invoice_is_stale_and_stays_parked(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S02: a [Sí] on an invoice parked from BR-R03 answers BR-S02 and leaves it parked."""
    profile_id = _profile()
    invoice_id = await _confirming_invoice(db_pool, profile_id, RULE_ASK)
    await _park(db_pool, invoice_id, CONFIRMING, RULE_ASK)
    msg = _press(invoice_id, "yes", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        waiting = await _waiting_row(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id is None
    assert invoice == (PENDING_USER, "BR-S08")
    assert waiting == (CONFIRMING, RULE_ASK)
    assert jobs == []
    assert texts == [
        ("BR-S02", MESSAGES["BR-S02"].format(id=invoice_id, state_desc=state_desc(PENDING_USER)))
    ]


@pytest.mark.parametrize(("action", "buyer_ok"), [("yes", True), ("change", False)])
async def test_yes_and_change_store_the_buyer_answer(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    action: str,
    buyer_ok: bool,
) -> None:
    """BR-R04: [Sí] stores buyer_ok true and [Cambiar] stores it false, then re-routes."""
    profile_id = _profile()
    invoice_id = await _confirming_invoice(db_pool, profile_id, RULE_BUYER)
    msg = _press(invoice_id, action, profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)

    assert result.status == "stored"
    assert flow_data["answers"]["buyer_ok"] is buyer_ok
    assert invoice == (ROUTING, RULE_BUYER)
    assert jobs == ["route"]


async def test_brand_with_an_out_of_range_choice_is_stale(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-R09: [brand] choice 5 of 2 answers BR-S02 and changes nothing."""
    profile_id = _profile()
    invoice_id = await _confirming_invoice(db_pool, profile_id, RULE_BRAND, choices=FLOW_CHOICES)
    msg = _press(invoice_id, "brand", profile_id, choice=5)

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert invoice == (CONFIRMING, RULE_BRAND)
    assert "answers" not in flow_data
    assert jobs == []
    assert texts == [("BR-S02", MESSAGES["BR-S02"].format(id=invoice_id, state_desc=state_desc(CONFIRMING)))]


async def test_brand_from_an_older_menu_is_stale(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.5, BR-S02: [brand] on menu 0 when menu 1 was asked answers BR-S02 and changes nothing."""
    profile_id = _profile()
    invoice_id = await _confirming_invoice(db_pool, profile_id, RULE_BRAND, choices=FLOW_CHOICES, menu=1)
    msg = _press(invoice_id, "brand", profile_id, choice=0, menu=0)

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert invoice == (CONFIRMING, RULE_BRAND)
    assert "answers" not in flow_data
    assert jobs == []
    assert texts == [("BR-S02", MESSAGES["BR-S02"].format(id=invoice_id, state_desc=state_desc(CONFIRMING)))]


async def test_brand_from_the_current_menu_is_taken(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.5, BR-S02: [brand] on the menu just asked stores its choice and re-routes."""
    profile_id = _profile()
    invoice_id = await _confirming_invoice(db_pool, profile_id, RULE_BRAND, choices=FLOW_CHOICES, menu=1)
    msg = _press(invoice_id, "brand", profile_id, choice=0, menu=1)

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert flow_data["answers"]["comercio"] == FLOW_CHOICES[0]
    assert invoice == (ROUTING, RULE_BRAND)
    assert jobs == ["route"]
    assert texts == []


async def test_pick_from_an_older_menu_is_stale(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.5, BR-S02: [pick] on menu 0 of the BR-R09 invoice answers BR-S02 and changes nothing."""
    profile_id = _profile()
    invoice_id = await _confirming_invoice(db_pool, profile_id, RULE_BRAND, choices=FLOW_CHOICES, menu=1)
    msg = _press(invoice_id, "pick", profile_id, choice=0, menu=0)

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert invoice == (CONFIRMING, RULE_BRAND)
    assert "answers" not in flow_data
    assert jobs == []
    assert texts == [("BR-S02", MESSAGES["BR-S02"].format(id=invoice_id, state_desc=state_desc(CONFIRMING)))]


async def test_an_answer_keeps_the_answers_stored_before_it(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D92: a later flow answer keeps the buyer_ok stored before it."""
    profile_id = _profile()
    invoice_id = await _confirming_invoice(db_pool, profile_id, RULE_BUYER)
    async with db_pool.connection() as conn:
        await handle_inbound(conn, _press(invoice_id, "yes", profile_id), max_queued_per_sender=MAX_QUEUED)

    async with db_pool.connection() as conn, conn.transaction():
        await merge_flow_data(conn, invoice_id, {"choices": list(FLOW_CHOICES), "menu": 0})
        await set_state(conn, invoice_id, CONFIRMING, rule_id=RULE_ASK)

    async with db_pool.connection() as conn:
        await handle_inbound(
            conn, _press(invoice_id, "pick", profile_id, choice=0), max_queued_per_sender=MAX_QUEUED
        )
        flow_data = await load_flow_data(conn, invoice_id)

    assert flow_data["answers"] == {"buyer_ok": True, "flow": FLOW_CHOICES[0]}


async def test_cancel_cancels_the_invoice_and_replies(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.13, BR-D06: [Cancelar] moves the invoice to CANCELLED and sends one reply."""
    profile_id = _profile()
    invoice_id = await _waiting_invoice(db_pool, profile_id)
    msg = _press(invoice_id, "cancel", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert invoice == ("CANCELLED", RULE_CAP)
    assert jobs == []
    assert texts == [(None, CANCELLED_BY_USER.format(id=invoice_id))]


async def test_cancel_in_fetching_docs_cancels_the_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-D06: [Cancelar] in FETCHING_DOCS moves the invoice to CANCELLED and replies."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, FETCHING_DOCS)
    msg = _press(invoice_id, "cancel", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert invoice == ("CANCELLED", RULE_CAP)
    assert jobs == []
    assert texts == [(None, CANCELLED_BY_USER.format(id=invoice_id))]


async def test_edit_in_fetching_docs_sends_the_edit_prompt(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.4: [Editar] in FETCHING_DOCS queues EDIT_PROMPT and changes no state."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, FETCHING_DOCS)
    msg = _press(invoice_id, "edit", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert invoice == (FETCHING_DOCS, None)
    assert jobs == []
    assert texts == [(None, EDIT_PROMPT)]


async def test_edit_in_done_is_stale(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.4, BR-S02: [Editar] in DONE answers BR-S02 and changes nothing."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, DONE)
    msg = _press(invoice_id, "edit", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id is None
    assert invoice == (DONE, None)
    assert jobs == []
    assert texts == [("BR-S02", MESSAGES["BR-S02"].format(id=invoice_id, state_desc=state_desc(DONE)))]


async def test_cancel_in_delivering_is_stale(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S02: [Cancelar] in DELIVERING answers BR-S02 and changes nothing."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, DELIVERING)
    msg = _press(invoice_id, "cancel", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id is None
    assert invoice == (DELIVERING, None)
    assert jobs == []
    assert texts == [("BR-S02", MESSAGES["BR-S02"].format(id=invoice_id, state_desc=state_desc(DELIVERING)))]


async def test_sent_closes_a_prepared_batch_and_deletes_the_photo(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """R-7.8, BR-D08: [Ya lo envié] marks the batch sent, the invoice DONE and deletes the photo."""
    profile_id = _profile()
    invoice_id, photo = await _batched_invoice(db_pool, profile_id, tmp_path)
    msg = _press(invoice_id, "sent", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        photo_path = await get_file_path(conn, invoice_id, "photo")
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert invoice == (DONE, RULE_SENT)
    assert batch_state == "sent"
    assert photo_path is None
    assert texts == [(RULE_SENT, MESSAGES[RULE_SENT])]
    assert not photo.exists()


async def test_a_second_sent_press_is_stale_and_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """BR-S02: [Ya lo envié] again on the DONE invoice answers BR-S02 and changes nothing."""
    profile_id = _profile()
    invoice_id, _ = await _batched_invoice(db_pool, profile_id, tmp_path)
    async with db_pool.connection() as conn:
        await handle_inbound(conn, _press(invoice_id, "sent", profile_id), max_queued_per_sender=MAX_QUEUED)

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, _press(invoice_id, "sent", profile_id), max_queued_per_sender=MAX_QUEUED
        )
        invoice = await _invoice_row(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert invoice == (DONE, RULE_SENT)
    assert batch_state == "sent"
    assert jobs == []
    assert texts == [
        (RULE_SENT, MESSAGES[RULE_SENT]),
        ("BR-S02", MESSAGES["BR-S02"].format(id=invoice_id, state_desc=state_desc(DONE))),
    ]


async def test_a_press_from_another_profile_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.5: another profile's button changes no state, job or reply."""
    owner = _profile()
    other = _profile()
    invoice_id = await _waiting_invoice(db_pool, owner)
    msg = _press(invoice_id, "continue", other)

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        owner_texts = await _texts(conn, owner)
        other_texts = await _texts(conn, other)

    assert result.status == "stored"
    assert result.invoice_id is None
    assert invoice == (CONFIRMING, RULE_CAP)
    assert jobs == []
    assert owner_texts == []
    assert other_texts == []


async def test_a_redelivered_press_acts_once(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """R-1.8: the same (channel, external_id) press twice acts once and stores one message."""
    profile_id = _profile()
    invoice_id, photo = await _batched_invoice(db_pool, profile_id, tmp_path)
    msg = _press(invoice_id, "sent", profile_id)

    async with db_pool.connection() as conn:
        first = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        second = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        invoice = await _invoice_row(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert first.status == "stored"
    assert second.status == "duplicate"
    assert second.invoice_id is None
    assert invoice == (DONE, RULE_SENT)
    assert batch_state == "sent"
    assert texts == [(RULE_SENT, MESSAGES[RULE_SENT])]
    assert not photo.exists()
