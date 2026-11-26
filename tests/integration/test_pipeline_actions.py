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
from invoicebot.channels.buttons import button_id
from invoicebot.config.models import MediaMaxBytes
from invoicebot.extract.fields import Field, Reading
from invoicebot.pipeline.actions import INVALID_OK_ACTION, send_not_confirmed
from invoicebot.pipeline.done import MAIL_TO_KEY
from invoicebot.pipeline.handlers import MEGABYTE
from invoicebot.pipeline.intake import IntakeRoles, handle_inbound
from invoicebot.pipeline.unreadable import unreadable_text
from invoicebot.rules.messages_es import (
    BUTTON_ARRIVED,
    BUTTON_CANCEL,
    BUTTON_EDIT,
    BUTTON_SELF_SEND,
    BUTTON_VIA_LAPTOP,
    CANCELLED_BY_USER,
    EDIT_PROMPT,
    MESSAGES,
    STATUS,
    state_desc,
)
from invoicebot.store.flow_b import (
    get_batch_state,
    load_flow_data,
    merge_flow_data,
    open_batch,
    set_batch_state,
)
from invoicebot.store.invoices import (
    FileRecord,
    add_file,
    create_invoice,
    get_file_path,
    save_reading,
    set_flow,
    set_state,
)
from invoicebot.store.mail_jobs import add_mail_job

pytestmark = pytest.mark.asyncio(loop_scope="session")

MAX_QUEUED = 10
MEDIA_MAX_BYTES = MediaMaxBytes(image=10 * MEGABYTE, pdf=10 * MEGABYTE, xml=MEGABYTE, zip=10 * MEGABYTE)
#: The owner timezone, for the dates a command prints (S11.4).
TIMEZONE = "America/Lima"
FLOW = "work_microsoft"
#: A flow whose modes are [inbox_rule, laptop_outlook] (D109).
LAPTOP_FLOW = "inbox_rule_laptop"
CONFIRMING = "CONFIRMING"
CONFIRM_INVALID = "CONFIRM_INVALID"
EXTRACT_FAILED = "EXTRACT_FAILED"
ROUTING = "ROUTING"
PENDING_USER = "PENDING_USER"
FETCHING_DOCS = "FETCHING_DOCS"
DELIVERING = "DELIVERING"
BATCHED = "BATCHED"
DONE = "DONE"
CANCELLED = "CANCELLED"
RULE_UNREADABLE = "BR-E01"
RULE_ASK = "BR-R03"
RULE_BUYER = "BR-R04"
RULE_BRAND = "BR-R09"
RULE_CAP = "BR-D06"
RULE_BATCHED = "BR-D07"
RULE_SENT = "BR-D08"
RULE_DONE = "BR-D10"
RULE_NOT_CONFIRMED = "BR-D11"
RULE_INBOX = "BR-D13"
RULE_ANNULLED = "BR-V02"
RULE_NOT_AUTHORIZED = "BR-V06"
FLOW_CHOICES = ("flow_a", "flow_b")
#: The folder and synthetic serie/numero BR-D10 and BR-D11 name.
FOLDER = "2026-11-Support_1_LaLena"
SERIE = "F001"
NUMERO = "15160"
MAIL_TO = ["rrhh@example.com"]
DONE_TEXT = MESSAGES["BR-D10"].format(serie=SERIE, numero=NUMERO, folder=FOLDER, to="rrhh@example.com")


def _field(value: str) -> Field:
    return Field(value=value, source="qr", confidence=1.0)


def _reading() -> Reading:
    """Build a synthetic reading with the serie and numero BR-D11 names."""
    return Reading(fields={"serie": _field(SERIE), "numero": _field(NUMERO)}, document_url=None)


def _partial_reading() -> Reading:
    """Build a BR-E01 reading missing ``fecha_emision`` and ``total`` (D188)."""
    return Reading(
        fields={
            "ruc_emisor": _field("20000000001"),
            "tipo": _field("01"),
            "serie": _field("F001"),
            "numero": _field("123"),
        },
        document_url=None,
    )


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


def _text(profile_id: str, text: str) -> InboundMessage:
    """Build a canonical text message for profile_id."""
    return InboundMessage(
        channel="whatsapp",
        external_id=f"ext-{uuid4().hex}",
        profile_id=profile_id,
        sender_role="owner",
        text=text,
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


async def _confirm_invalid_invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]], profile_id: str, rule_id: str
) -> int:
    """Create a CONFIRM_INVALID invoice waiting on rule_id (BR-V02, BR-V06)."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await set_flow(conn, invoice_id, FLOW)
        await set_state(conn, invoice_id, CONFIRM_INVALID, rule_id=rule_id)
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


async def _extract_failed_invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    reading: Reading,
) -> int:
    """Create an EXTRACT_FAILED invoice waiting on BR-E01 with its partial reading (D188)."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await save_reading(conn, invoice_id, reading)
        await set_state(conn, invoice_id, EXTRACT_FAILED, rule_id=RULE_UNREADABLE)
    return invoice_id


async def _invoice_in(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    state: str,
    rule_id: str | None = None,
) -> int:
    """Create an invoice in state, waiting on rule_id when one is given."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await set_flow(conn, invoice_id, FLOW)
        await set_state(conn, invoice_id, state, rule_id=rule_id)
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


async def _done_invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    *,
    batch_state: str | None = None,
    with_mail_job: bool = False,
    flow: str = FLOW,
) -> int:
    """Create a DONE BR-D13 invoice with a reading and folder, optionally a batch and mail job."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await save_reading(conn, invoice_id, _reading())
        await set_flow(conn, invoice_id, flow)
        await set_state(conn, invoice_id, DONE, rule_id=RULE_INBOX)
        await merge_flow_data(conn, invoice_id, {"folder": FOLDER, MAIL_TO_KEY: MAIL_TO})
        if batch_state is not None:
            batch_id = await open_batch(conn, invoice_id, FLOW, "prepared")
            await set_batch_state(conn, batch_id, batch_state)
        if with_mail_job:
            await add_mail_job(conn, uuid4().hex, invoice_id)
    return invoice_id


async def _prepared_laptop_invoice(pool: AsyncConnectionPool[AsyncConnection[Any]], profile_id: str) -> int:
    """Create a BATCHED laptop invoice whose batch is prepared, with a reading and folder."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await save_reading(conn, invoice_id, _reading())
        await set_flow(conn, invoice_id, FLOW)
        await set_state(conn, invoice_id, BATCHED, rule_id=RULE_BATCHED)
        await merge_flow_data(conn, invoice_id, {"folder": FOLDER, MAIL_TO_KEY: MAIL_TO})
        await open_batch(conn, invoice_id, FLOW, "prepared")
    return invoice_id


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


async def _messages(conn: AsyncConnection[Any], profile_id: str) -> list[tuple[str | None, str, Any]]:
    """Return the outbox's ``(rule_id, text, buttons)`` rows for profile_id, oldest first."""
    cur = await conn.execute(
        """
        SELECT payload_json->>'rule_id', payload_json->>'text', payload_json->'buttons'
        FROM outbox
        WHERE "to" = %s
        ORDER BY id
        """,
        (profile_id,),
    )
    return [(rule_id, str(text), buttons) for rule_id, text, buttons in await cur.fetchall()]


async def test_continue_stores_the_cap_answer_and_reroutes(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-D06: [Continuar] stores cap_ok, sets ROUTING and queues a route job."""
    profile_id = _profile()
    invoice_id = await _waiting_invoice(db_pool, profile_id)
    msg = _press(invoice_id, "continue", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
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
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
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
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
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
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
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
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
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
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
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
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
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
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
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
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
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
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
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
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
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
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
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
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
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
        await handle_inbound(
            conn,
            _press(invoice_id, "yes", profile_id),
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
        )

    async with db_pool.connection() as conn, conn.transaction():
        await merge_flow_data(conn, invoice_id, {"choices": list(FLOW_CHOICES), "menu": 0})
        await set_state(conn, invoice_id, CONFIRMING, rule_id=RULE_ASK)

    async with db_pool.connection() as conn:
        await handle_inbound(
            conn,
            _press(invoice_id, "pick", profile_id, choice=0),
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
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
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
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
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
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
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
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
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        invoice = await _invoice_row(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id is None
    assert invoice == (DONE, None)
    assert jobs == []
    assert texts == [("BR-S02", MESSAGES["BR-S02"].format(id=invoice_id, state_desc=state_desc(DONE)))]


async def test_edit_on_an_extract_failed_invoice_lists_the_missing_fields(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D188, BR-E01: [Editar] pins the invoice and re-asks its missing fields."""
    profile_id = _profile()
    reading = _partial_reading()
    invoice_id = await _extract_failed_invoice(db_pool, profile_id, reading)
    msg = _press(invoice_id, "edit", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        invoice = await _invoice_row(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        messages = await _messages(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert invoice == (EXTRACT_FAILED, RULE_UNREADABLE)
    assert flow_data["editing"] is True
    assert jobs == []
    assert messages == [
        (
            RULE_UNREADABLE,
            unreadable_text(invoice_id, reading),
            [
                {"id": button_id(invoice_id, "edit"), "title": BUTTON_EDIT},
                {"id": button_id(invoice_id, "cancel"), "title": BUTTON_CANCEL},
            ],
        )
    ]


async def test_edit_on_a_bad_ruc_invoice_missing_fields_lists_them(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D188, BR-E01: [Editar] on a failed invoice still missing fields re-asks them, whatever its rule."""
    profile_id = _profile()
    reading = _partial_reading()
    async with db_pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await save_reading(conn, invoice_id, reading)
        await set_state(conn, invoice_id, EXTRACT_FAILED, rule_id="BR-E02")

    async with db_pool.connection() as conn:
        await handle_inbound(
            conn,
            _press(invoice_id, "edit", profile_id),
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
        )
        texts = await _texts(conn, profile_id)

    assert texts == [(RULE_UNREADABLE, unreadable_text(invoice_id, reading))]


async def test_status_press_keeps_a_pending_edit(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D188, D90: a [Ver #{id}] press only answers, so the invoice stays the sender's edit target."""
    profile_id = _profile()
    invoice_id = await _extract_failed_invoice(db_pool, profile_id, _partial_reading())

    async with db_pool.connection() as conn:
        for action in ("edit", "status"):
            await handle_inbound(
                conn,
                _press(invoice_id, action, profile_id),
                max_queued_per_sender=MAX_QUEUED,
                media_max_bytes=MEDIA_MAX_BYTES,
                timezone=TIMEZONE,
            )
        flow_data = await load_flow_data(conn, invoice_id)

    assert flow_data["editing"] is True


async def test_cancel_on_an_extract_failed_invoice_cancels_it(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D188, R-3.4: [Cancelar] on the BR-E01 question moves the invoice to CANCELLED."""
    profile_id = _profile()
    invoice_id = await _extract_failed_invoice(db_pool, profile_id, _partial_reading())
    msg = _press(invoice_id, "cancel", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        invoice = await _invoice_row(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert invoice == ("CANCELLED", RULE_CAP)
    assert jobs == []
    assert texts == [(None, CANCELLED_BY_USER.format(id=invoice_id))]


async def test_edit_on_a_parked_br_e01_invoice_resumes_and_lists_the_missing_fields(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D188, R-3.7: [Editar] on a parked BR-E01 invoice resumes EXTRACT_FAILED and re-asks."""
    profile_id = _profile()
    reading = _partial_reading()
    invoice_id = await _extract_failed_invoice(db_pool, profile_id, reading)
    await _park(db_pool, invoice_id, EXTRACT_FAILED, RULE_UNREADABLE)
    msg = _press(invoice_id, "edit", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        invoice = await _invoice_row(conn, invoice_id)
        waiting = await _waiting_row(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        messages = await _messages(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert invoice == (EXTRACT_FAILED, RULE_UNREADABLE)
    assert waiting == (None, None)
    assert flow_data["editing"] is True
    assert jobs == []
    assert messages == [
        (
            RULE_UNREADABLE,
            unreadable_text(invoice_id, reading),
            [
                {"id": button_id(invoice_id, "edit"), "title": BUTTON_EDIT},
                {"id": button_id(invoice_id, "cancel"), "title": BUTTON_CANCEL},
            ],
        )
    ]


async def test_cancel_in_delivering_is_stale(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S02: [Cancelar] in DELIVERING answers BR-S02 and changes nothing."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, DELIVERING)
    msg = _press(invoice_id, "cancel", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
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
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
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


async def test_sent_enqueues_one_folder_status_job(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """R-7.11: [Ya lo envié] leaves exactly one folder_status job for the invoice."""
    profile_id = _profile()
    invoice_id, _ = await _batched_invoice(db_pool, profile_id, tmp_path)
    msg = _press(invoice_id, "sent", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        jobs = await _jobs(conn, invoice_id)

    assert result.status == "stored"
    assert jobs == ["folder_status"]


async def test_a_second_sent_press_is_stale_and_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """BR-S02: [Ya lo envié] again on the DONE invoice answers BR-S02 and changes nothing."""
    profile_id = _profile()
    invoice_id, _ = await _batched_invoice(db_pool, profile_id, tmp_path)
    async with db_pool.connection() as conn:
        await handle_inbound(
            conn,
            _press(invoice_id, "sent", profile_id),
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
        )

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn,
            _press(invoice_id, "sent", profile_id),
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
        )
        invoice = await _invoice_row(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert invoice == (DONE, RULE_SENT)
    assert batch_state == "sent"
    assert jobs == ["folder_status"]
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
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
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
        first = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        second = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
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


async def test_not_arrived_with_a_laptop_flow_sends_br_d11_with_three_buttons(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.8: [No llegó] on a flow listing laptop_outlook offers [Por la laptop] without a mail job."""
    profile_id = _profile()
    invoice_id = await _done_invoice(db_pool, profile_id, flow=LAPTOP_FLOW)
    msg = _press(invoice_id, "not_arrived", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn,
            msg,
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
            roles=IntakeRoles(reprocess_min_interval_s=0, laptop_flows=frozenset({LAPTOP_FLOW})),
        )
        invoice = await _invoice_row(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        messages = await _messages(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert invoice == (DONE, RULE_INBOX)
    assert jobs == []
    assert messages == [
        (
            RULE_NOT_CONFIRMED,
            MESSAGES[RULE_NOT_CONFIRMED].format(serie=SERIE, numero=NUMERO),
            [
                {"id": button_id(invoice_id, "via_laptop"), "title": BUTTON_VIA_LAPTOP},
                {"id": button_id(invoice_id, "self_send"), "title": BUTTON_SELF_SEND},
                {"id": button_id(invoice_id, "arrived"), "title": BUTTON_ARRIVED},
            ],
        )
    ]


async def test_not_arrived_without_a_laptop_flow_sends_two_buttons(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.8: [No llegó] on a flow without laptop_outlook sends no [Por la laptop], even with a mail job."""
    profile_id = _profile()
    invoice_id = await _done_invoice(db_pool, profile_id, with_mail_job=True)
    msg = _press(invoice_id, "not_arrived", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn,
            msg,
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
            roles=IntakeRoles(reprocess_min_interval_s=0, laptop_flows=frozenset({"another_flow"})),
        )
        invoice = await _invoice_row(conn, invoice_id)
        messages = await _messages(conn, profile_id)

    assert result.status == "stored"
    assert invoice == (DONE, RULE_INBOX)
    assert messages == [
        (
            RULE_NOT_CONFIRMED,
            MESSAGES[RULE_NOT_CONFIRMED].format(serie=SERIE, numero=NUMERO),
            [
                {"id": button_id(invoice_id, "self_send"), "title": BUTTON_SELF_SEND},
                {"id": button_id(invoice_id, "arrived"), "title": BUTTON_ARRIVED},
            ],
        )
    ]


async def test_arrived_completes_a_prepared_batch_with_br_d10(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-D10, D195: [Ya llegó] on a prepared laptop batch sends it, files DONE and queues folder_status."""
    profile_id = _profile()
    invoice_id = await _prepared_laptop_invoice(db_pool, profile_id)
    msg = _press(invoice_id, "arrived", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        invoice = await _invoice_row(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        messages = await _messages(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert invoice == (DONE, RULE_DONE)
    assert batch_state == "sent"
    assert jobs == ["folder_status"]
    assert messages == [(RULE_DONE, DONE_TEXT, [])]


async def test_arrived_on_a_prepared_batch_deletes_the_photo(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """R-9.7: [Ya llegó] that completes a prepared batch clears and deletes the photo."""
    profile_id = _profile()
    invoice_id, photo = await _batched_invoice(db_pool, profile_id, tmp_path)
    async with db_pool.connection() as conn, conn.transaction():
        await save_reading(conn, invoice_id, _reading())
        await merge_flow_data(conn, invoice_id, {"folder": FOLDER, MAIL_TO_KEY: MAIL_TO})
    msg = _press(invoice_id, "arrived", profile_id)

    async with db_pool.connection() as conn:
        await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        photo_path = await get_file_path(conn, invoice_id, "photo")

    assert photo_path is None
    assert not photo.exists()


async def test_arrived_on_a_sent_batch_only_answers_br_d10(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-D10, D195: [Ya llegó] on a sent batch changes no state and only sends BR-D10's text."""
    profile_id = _profile()
    invoice_id = await _done_invoice(db_pool, profile_id, batch_state="sent")
    msg = _press(invoice_id, "arrived", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        invoice = await _invoice_row(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        messages = await _messages(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert invoice == (DONE, RULE_INBOX)
    assert batch_state == "sent"
    assert jobs == []
    assert messages == [(RULE_DONE, DONE_TEXT, [])]


async def test_via_laptop_enqueues_one_resend_job_and_changes_no_state(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D109: [Por la laptop] while BR-D11 is the last question enqueues the resend (R-7.14)."""
    profile_id = _profile()
    invoice_id = await _done_invoice(db_pool, profile_id, batch_state="sent")
    laptop_flows = frozenset({FLOW})
    async with db_pool.connection() as conn, conn.transaction():
        await send_not_confirmed(conn, invoice_id, laptop_flows)
    msg = _press(invoice_id, "via_laptop", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn,
            msg,
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
            roles=IntakeRoles(reprocess_min_interval_s=0, laptop_flows=laptop_flows),
        )
        invoice = await _invoice_row(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert invoice == (DONE, RULE_INBOX)
    assert batch_state == "sent"
    assert jobs == ["resend_laptop"]
    assert texts == [
        (RULE_NOT_CONFIRMED, MESSAGES[RULE_NOT_CONFIRMED].format(serie=SERIE, numero=NUMERO)),
    ]


async def test_via_laptop_on_a_flow_without_laptop_outlook_answers_br_s02(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D109: [Por la laptop] on a flow without laptop_outlook answers BR-S02 and enqueues nothing."""
    profile_id = _profile()
    invoice_id = await _done_invoice(db_pool, profile_id, batch_state="sent")
    laptop_flows = frozenset({"another_flow"})
    async with db_pool.connection() as conn, conn.transaction():
        await send_not_confirmed(conn, invoice_id, laptop_flows)
    msg = _press(invoice_id, "via_laptop", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn,
            msg,
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
            roles=IntakeRoles(reprocess_min_interval_s=0, laptop_flows=laptop_flows),
        )
        invoice = await _invoice_row(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert invoice == (DONE, RULE_INBOX)
    assert jobs == []
    assert texts == [
        (RULE_NOT_CONFIRMED, MESSAGES[RULE_NOT_CONFIRMED].format(serie=SERIE, numero=NUMERO)),
        ("BR-S02", MESSAGES["BR-S02"].format(id=invoice_id, state_desc=state_desc(DONE))),
    ]


async def test_via_laptop_after_br_d10_answers_br_s02(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S02: [Por la laptop] after the invoice got BR-D10 answers BR-S02 and enqueues nothing."""
    profile_id = _profile()
    invoice_id = await _done_invoice(db_pool, profile_id, batch_state="sent")
    laptop_flows = frozenset({FLOW})
    async with db_pool.connection() as conn, conn.transaction():
        await send_not_confirmed(conn, invoice_id, laptop_flows)
    async with db_pool.connection() as conn:
        await handle_inbound(
            conn,
            _press(invoice_id, "arrived", profile_id),
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
        )
    msg = _press(invoice_id, "via_laptop", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn,
            msg,
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
            roles=IntakeRoles(reprocess_min_interval_s=0, laptop_flows=laptop_flows),
        )
        invoice = await _invoice_row(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert invoice == (DONE, RULE_INBOX)
    assert jobs == []
    assert texts == [
        (RULE_NOT_CONFIRMED, MESSAGES[RULE_NOT_CONFIRMED].format(serie=SERIE, numero=NUMERO)),
        (RULE_DONE, DONE_TEXT),
        ("BR-S02", MESSAGES["BR-S02"].format(id=invoice_id, state_desc=state_desc(DONE))),
    ]


async def test_self_send_enqueues_one_resend_job_and_changes_no_state(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.8: [Lo envío yo] enqueues one resend_eml job and changes no invoice or batch state."""
    profile_id = _profile()
    invoice_id = await _prepared_laptop_invoice(db_pool, profile_id)
    async with db_pool.connection() as conn, conn.transaction():
        await send_not_confirmed(conn, invoice_id, frozenset())
    msg = _press(invoice_id, "self_send", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        invoice = await _invoice_row(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert invoice == (BATCHED, RULE_BATCHED)
    assert batch_state == "prepared"
    assert jobs == ["resend_eml"]
    assert texts == [
        (RULE_NOT_CONFIRMED, MESSAGES[RULE_NOT_CONFIRMED].format(serie=SERIE, numero=NUMERO)),
    ]


async def test_self_send_after_br_d10_answers_br_s02(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S02: [Lo envío yo] after BR-D10 answers BR-S02."""
    profile_id = _profile()
    invoice_id = await _done_invoice(db_pool, profile_id)
    async with db_pool.connection() as conn, conn.transaction():
        await send_not_confirmed(conn, invoice_id, frozenset())
    async with db_pool.connection() as conn:
        await handle_inbound(
            conn,
            _press(invoice_id, "arrived", profile_id),
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
        )
    msg = _press(invoice_id, "self_send", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        invoice = await _invoice_row(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert invoice == (DONE, RULE_INBOX)
    assert jobs == []
    assert texts == [
        (RULE_NOT_CONFIRMED, MESSAGES[RULE_NOT_CONFIRMED].format(serie=SERIE, numero=NUMERO)),
        (RULE_DONE, DONE_TEXT),
        ("BR-S02", MESSAGES["BR-S02"].format(id=invoice_id, state_desc=state_desc(DONE))),
    ]


async def test_invalid_ok_on_br_v02_sets_fetching_docs_with_a_fetch_job(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-V02: [Sí] on the annulled question moves the invoice to FETCHING_DOCS and queues a fetch job."""
    profile_id = _profile()
    invoice_id = await _confirm_invalid_invoice(db_pool, profile_id, RULE_ANNULLED)
    msg = _press(invoice_id, INVALID_OK_ACTION, profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        invoice = await _invoice_row(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert invoice == (FETCHING_DOCS, RULE_ANNULLED)
    assert jobs == ["fetch"]
    assert texts == []


async def test_invalid_ok_on_br_v06_sets_fetching_docs_with_a_fetch_job(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-V06: [Sí] on the unauthorized question moves the invoice to FETCHING_DOCS and queues a fetch job."""
    profile_id = _profile()
    invoice_id = await _confirm_invalid_invoice(db_pool, profile_id, RULE_NOT_AUTHORIZED)
    msg = _press(invoice_id, INVALID_OK_ACTION, profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        invoice = await _invoice_row(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert invoice == (FETCHING_DOCS, RULE_NOT_AUTHORIZED)
    assert jobs == ["fetch"]
    assert texts == []


async def test_invalid_cancel_on_br_v02_cancels_the_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-V02: [Cancelar] on the annulled question cancels the invoice."""
    profile_id = _profile()
    invoice_id = await _confirm_invalid_invoice(db_pool, profile_id, RULE_ANNULLED)
    msg = _press(invoice_id, "cancel", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        invoice = await _invoice_row(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert invoice == ("CANCELLED", RULE_CAP)
    assert jobs == []
    assert texts == [(None, CANCELLED_BY_USER.format(id=invoice_id))]


async def test_invalid_ok_on_a_fetching_docs_invoice_is_stale(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S02: [Sí] on an invoice already FETCHING_DOCS answers BR-S02 and changes nothing."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, FETCHING_DOCS)
    msg = _press(invoice_id, INVALID_OK_ACTION, profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        invoice = await _invoice_row(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id is None
    assert invoice == (FETCHING_DOCS, None)
    assert jobs == []
    assert texts == [
        ("BR-S02", MESSAGES["BR-S02"].format(id=invoice_id, state_desc=state_desc(FETCHING_DOCS)))
    ]


async def test_status_press_answers_like_the_estado_command(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D188: a [Ver #{id}] press answers the same text as /estado {id}."""
    profile_id = _profile()
    invoice_id = await _waiting_invoice(db_pool, profile_id)
    expected = STATUS.format(id=invoice_id, state_desc=state_desc(CONFIRMING))

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn,
            _press(invoice_id, "status", profile_id),
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
        )
        jobs = await _jobs(conn, invoice_id)
        pressed = await _texts(conn, profile_id)

    async with db_pool.connection() as conn:
        await handle_inbound(
            conn,
            _text(profile_id, f"/estado {invoice_id}"),
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
        )
        both = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert jobs == []
    assert pressed == [(None, expected)]
    assert both == [(None, expected), (None, expected)]


@pytest.mark.parametrize("state", [CANCELLED, DONE])
async def test_status_press_answers_a_terminal_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    state: str,
) -> None:
    """D188: [Ver #{id}] is answered in CANCELLED and DONE, where other actions are stale."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, state, RULE_CAP)
    msg = _press(invoice_id, "status", profile_id)

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        invoice = await _invoice_row(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)

    assert result.status == "stored"
    assert result.invoice_id == invoice_id
    assert invoice == (state, RULE_CAP)
    assert jobs == []
    assert texts == [(None, STATUS.format(id=invoice_id, state_desc=state_desc(state)))]


async def test_status_press_from_another_profile_sends_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D188, R-10.1: another profile's [Ver #{id}] resolves to None and queues no outbox row."""
    owner = _profile()
    other = _profile()
    invoice_id = await _invoice_in(db_pool, owner, DONE)
    msg = _press(invoice_id, "status", other)

    async with db_pool.connection() as conn:
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
        invoice = await _invoice_row(conn, invoice_id)
        owner_texts = await _texts(conn, owner)
        other_texts = await _texts(conn, other)

    assert result.status == "stored"
    assert result.invoice_id is None
    assert invoice == (DONE, None)
    assert owner_texts == []
    assert other_texts == []


async def _review_invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]], profile_id: str, batch_state: str = "review"
) -> int:
    """Create a BATCHED BR-D19 invoice whose batch is in ``batch_state`` (D194)."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await save_reading(conn, invoice_id, _reading())
        await set_flow(conn, invoice_id, FLOW)
        await set_state(conn, invoice_id, BATCHED, rule_id="BR-D19")
        batch_id = await open_batch(conn, invoice_id, FLOW, "review")
        await set_batch_state(conn, batch_id, batch_state)
    return invoice_id


async def _press_once(
    pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int, action: str, profile_id: str
) -> None:
    """Handle one press of ``action`` on the invoice in its own connection."""
    async with pool.connection() as conn:
        await handle_inbound(
            conn,
            _press(invoice_id, action, profile_id),
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
        )


async def test_send_on_a_review_batch_runs_the_delivery_again(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D194, BR-D21: [Enviar] marks the mail reviewed, moves the invoice to DELIVERING and queues deliver."""
    profile_id = _profile()
    invoice_id = await _review_invoice(db_pool, profile_id)

    await _press_once(db_pool, invoice_id, "send", profile_id)

    async with db_pool.connection() as conn:
        invoice = await _invoice_row(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        flow_data = await load_flow_data(conn, invoice_id)
        texts = await _texts(conn, profile_id)
    assert invoice == ("DELIVERING", "BR-D21")
    assert jobs == ["deliver"]
    assert flow_data["reviewed"] is True
    assert texts == [("BR-D21", MESSAGES["BR-D21"].format(serie=SERIE, numero=NUMERO))]


async def test_a_second_send_press_is_stale_and_queues_no_second_job(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D194, BR-S02: [Enviar] again while the invoice is DELIVERING answers BR-S02 and adds no job."""
    profile_id = _profile()
    invoice_id = await _review_invoice(db_pool, profile_id)
    await _press_once(db_pool, invoice_id, "send", profile_id)

    await _press_once(db_pool, invoice_id, "send", profile_id)

    async with db_pool.connection() as conn:
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)
    assert jobs == ["deliver"]
    assert [rule_id for rule_id, _ in texts] == ["BR-D21", "BR-S02"]


async def test_hold_on_a_review_batch_offers_send_again_and_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D194, BR-D20: [Cancelar] answers BR-D20 with one [Enviar]; the invoice and batch stay as they are."""
    profile_id = _profile()
    invoice_id = await _review_invoice(db_pool, profile_id)

    await _press_once(db_pool, invoice_id, "hold", profile_id)

    async with db_pool.connection() as conn:
        invoice = await _invoice_row(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        messages = await _messages(conn, profile_id)
    assert invoice == (BATCHED, "BR-D19")
    assert batch_state == "review"
    assert jobs == []
    assert messages == [
        (
            "BR-D20",
            MESSAGES["BR-D20"].format(serie=SERIE, numero=NUMERO),
            [{"id": button_id(invoice_id, "send"), "title": "Enviar"}],
        )
    ]


async def test_send_on_a_prepared_batch_is_stale(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D194, BR-S02: [Enviar] on a batch that is not under review answers BR-S02 and changes nothing."""
    profile_id = _profile()
    invoice_id = await _review_invoice(db_pool, profile_id, batch_state="prepared")

    await _press_once(db_pool, invoice_id, "send", profile_id)

    async with db_pool.connection() as conn:
        invoice = await _invoice_row(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)
    assert invoice == (BATCHED, "BR-D19")
    assert jobs == []
    assert [rule_id for rule_id, _ in texts] == ["BR-S02"]


async def test_hold_on_a_prepared_batch_is_stale(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D194, BR-S02: [Cancelar] on a batch that is not under review answers BR-S02 and changes nothing."""
    profile_id = _profile()
    invoice_id = await _review_invoice(db_pool, profile_id, batch_state="prepared")

    await _press_once(db_pool, invoice_id, "hold", profile_id)

    async with db_pool.connection() as conn:
        invoice = await _invoice_row(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        texts = await _texts(conn, profile_id)
    assert invoice == (BATCHED, "BR-D19")
    assert batch_state == "prepared"
    assert jobs == []
    assert [rule_id for rule_id, _ in texts] == ["BR-S02"]
