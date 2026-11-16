"""Integration tests for the ``/pagado`` command (R-7.12, D107).

Commands run through :func:`handle_command` against real Postgres; every test uses its
own profile, so no outbox row, proposal or invoice leaks between them.
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import Button, ChannelName, InboundMessage
from invoicebot.channels.whatsapp.payloads import buttons_payload
from invoicebot.extract.fields import Field, Reading
from invoicebot.pipeline.commands import CommandContext, PaymentFlow, handle_command
from invoicebot.pipeline.intake import handle_inbound
from invoicebot.pipeline.steps import FOLDER_STATUS_STEP
from invoicebot.rules.messages_es import (
    BUTTON_NO,
    BUTTON_YES,
    HELP_PAGADO,
    MESSAGES,
    PAYMENT_LINE,
    PAYMENT_SET_LINE,
    state_desc,
)
from invoicebot.states import DONE
from invoicebot.store.flow_b import open_batch, set_batch_state
from invoicebot.store.invoices import create_invoice, save_reading, set_flow, set_state
from invoicebot.store.payments import record_payment

pytestmark = pytest.mark.asyncio(loop_scope="session")

CHANNEL: ChannelName = "whatsapp"
#: The owner's timezone, for the dates the command prints (R-7.12).
TIMEZONE = "America/Lima"
#: The queue limit a press goes through intake with; no press reaches back-pressure.
MAX_QUEUED = 10
#: The day a test pays; today is later, so the year is the current one (R-7.12).
PAID_ON = date(2026, 11, 15)
#: An invoice id no test creates.
UNKNOWN_INVOICE_ID = 999_999_999


def _unique(prefix: str) -> str:
    """Return an id no other test uses, so tests cannot interfere."""
    return f"{prefix}-{uuid4().hex}"


def _flow() -> PaymentFlow:
    """Return a payment flow with its own id and no reimbursement cap."""
    return PaymentFlow(flow_id=_unique("flow"), cap=None)


def _reading(total: str) -> Reading:
    """Return a synthetic reading whose only amount is the invoice total."""
    return Reading(
        fields={"total": Field(value=total, source="qr", confidence=1.0)},
        document_url=None,
    )


def _message(profile_id: str, arg: str) -> InboundMessage:
    """Build a canonical ``/pagado`` text message for profile_id."""
    return InboundMessage(
        channel=CHANNEL,
        external_id=f"ext-{uuid4().hex}",
        profile_id=profile_id,
        sender_role="owner",
        text=f"/pagado {arg}",
        attachments=[],
        received_at=datetime.now(UTC),
    )


async def _invoice(
    conn: AsyncConnection[Any],
    profile_id: str,
    flow_id: str,
    total: str,
    *,
    created_at: datetime | None = None,
) -> int:
    """Create a DONE, sent invoice in flow_id whose total is total."""
    invoice_id = await create_invoice(conn, profile_id, CHANNEL)
    await set_flow(conn, invoice_id, flow_id)
    await save_reading(conn, invoice_id, _reading(total))
    await set_state(conn, invoice_id, DONE)
    batch_id = await open_batch(conn, invoice_id, flow_id, "prepared")
    await set_batch_state(conn, batch_id, "sent")
    if created_at is not None:
        await conn.execute("UPDATE invoices SET created_at = %s WHERE id = %s", (created_at, invoice_id))
    return invoice_id


async def _run(
    conn: AsyncConnection[Any],
    profile_id: str,
    arg: str,
    payment_flow: PaymentFlow | None,
) -> None:
    """Run ``/pagado arg`` for profile_id in the caller's transaction."""
    context = CommandContext(admins=frozenset(), timezone=TIMEZONE, payment_flow=payment_flow)
    await handle_command(conn, _message(profile_id, arg), "pagado", arg, context=context)


async def _replies(
    conn: AsyncConnection[Any],
    profile_id: str,
) -> list[tuple[str, str | None, list[dict[str, str]]]]:
    """Return the profile's outbox ``(text, rule_id, buttons)`` rows, oldest first."""
    cur = await conn.execute(
        """
        SELECT payload_json->>'text', payload_json->>'rule_id', payload_json->'buttons'
        FROM outbox
        WHERE "to" = %s
        ORDER BY id
        """,
        (profile_id,),
    )
    rows = await cur.fetchall()
    return [(str(text), rule_id, cast(list[dict[str, str]], buttons)) for text, rule_id, buttons in rows]


async def _proposals(
    conn: AsyncConnection[Any],
    profile_id: str,
) -> list[tuple[int, date, Decimal, list[list[int]]]]:
    """Return the profile's stored payment proposals, oldest first."""
    cur = await conn.execute(
        """
        SELECT id, paid_on, amount, sets_json
        FROM payment_proposals
        WHERE profile_id = %s
        ORDER BY id
        """,
        (profile_id,),
    )
    rows = await cur.fetchall()
    return [
        (int(proposal_id), paid_on, amount, cast(list[list[int]], sets))
        for proposal_id, paid_on, amount, sets in rows
    ]


def _button(button_id: str, title: str) -> dict[str, str]:
    """Return one outbox button payload."""
    return {"id": button_id, "title": title}


def _press(profile_id: str, button_id: str) -> InboundMessage:
    """Build a canonical proposal button press for profile_id."""
    return InboundMessage(
        channel=CHANNEL,
        external_id=f"ext-{uuid4().hex}",
        profile_id=profile_id,
        sender_role="owner",
        button_id=button_id,
        received_at=datetime.now(UTC),
    )


async def _press_through_intake(conn: AsyncConnection[Any], msg: InboundMessage) -> None:
    """Send one button press through intake, as the webhook would."""
    await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED, timezone=TIMEZONE)


async def _proposal_for(
    conn: AsyncConnection[Any],
    profile_id: str,
    flow: PaymentFlow,
) -> tuple[int, tuple[int, ...]]:
    """Create two sent invoices, run ``/pagado`` and return the proposal and its only set."""
    await _invoice(conn, profile_id, flow.flow_id, "60.00")
    await _invoice(conn, profile_id, flow.flow_id, "40.00")
    await _run(conn, profile_id, "15/11 100.00", flow)
    proposals = await _proposals(conn, profile_id)
    assert len(proposals) == 1
    proposal_id, _paid_on, _amount, sets = proposals[0]
    assert len(sets) == 1
    return proposal_id, tuple(sets[0])


async def _proposal_state(conn: AsyncConnection[Any], proposal_id: int) -> str:
    """Return the stored state of one payment proposal."""
    cur = await conn.execute("SELECT state FROM payment_proposals WHERE id = %s", (proposal_id,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0])


async def _payment_count(conn: AsyncConnection[Any], profile_id: str) -> int:
    """Return how many payments the profile has recorded."""
    cur = await conn.execute("SELECT count(*) FROM payments WHERE profile_id = %s", (profile_id,))
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _payment_ids(
    conn: AsyncConnection[Any],
    invoice_ids: Sequence[int],
) -> list[int | None]:
    """Return each invoice's payment id, in the order given."""
    cur = await conn.execute(
        "SELECT id, payment_id FROM invoices WHERE id = ANY(%s)",
        (list(invoice_ids),),
    )
    by_id = {
        int(invoice_id): None if payment_id is None else int(payment_id)
        for invoice_id, payment_id in await cur.fetchall()
    }
    return [by_id[invoice_id] for invoice_id in invoice_ids]


async def _jobs(conn: AsyncConnection[Any], invoice_id: int) -> list[str]:
    """Return the steps of the invoice's queued jobs, in insertion order."""
    cur = await conn.execute("SELECT step FROM jobs WHERE invoice_id = %s ORDER BY id", (invoice_id,))
    return [str(step) for (step,) in await cur.fetchall()]


async def test_command_amount_with_one_set_proposes_br_p01(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.12: one matching set answers BR-P01 and stores a proposal behind [Sí] [No]."""
    profile_id = _unique("profile")
    flow = _flow()
    async with db_pool.connection() as conn:
        first = await _invoice(conn, profile_id, flow.flow_id, "60.00")
        second = await _invoice(conn, profile_id, flow.flow_id, "40.00")
        await _run(conn, profile_id, "15/11 100.00", flow)
        replies = await _replies(conn, profile_id)
        proposals = await _proposals(conn, profile_id)

    assert len(proposals) == 1
    proposal_id, paid_on, amount, sets = proposals[0]
    assert (paid_on, amount, sets) == (PAID_ON, Decimal("100.00"), [[first, second]])
    text = MESSAGES["BR-P01"].format(monto="100.00", fecha="15/11", n=2, lista=f"#{first} #{second}")
    assert replies == [
        (
            text,
            "BR-P01",
            [
                _button(f"p{proposal_id}:pay:0", BUTTON_YES),
                _button(f"p{proposal_id}:pay_no", BUTTON_NO),
            ],
        )
    ]


async def test_command_amount_with_two_sets_lists_them_oldest_first_as_br_p02(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.12: two matching sets answer BR-P02 with one button each, oldest set first."""
    profile_id = _unique("profile")
    flow = _flow()
    older = datetime(2026, 11, 1, 15, 0, tzinfo=UTC)
    newer = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
    async with db_pool.connection() as conn:
        old_a = await _invoice(conn, profile_id, flow.flow_id, "60.00", created_at=older)
        old_b = await _invoice(conn, profile_id, flow.flow_id, "40.00", created_at=older)
        new_a = await _invoice(conn, profile_id, flow.flow_id, "70.00", created_at=newer)
        new_b = await _invoice(conn, profile_id, flow.flow_id, "30.00", created_at=newer)
        await _run(conn, profile_id, "15/11 100.00", flow)
        replies = await _replies(conn, profile_id)
        proposals = await _proposals(conn, profile_id)

    assert len(proposals) == 1
    proposal_id, _paid_on, amount, sets = proposals[0]
    assert amount == Decimal("100.00")
    assert sets == [[old_a, old_b], [new_a, new_b]]
    old_line = PAYMENT_SET_LINE.format(n=1, fecha_min="01/11", count=2, ids=f"#{old_a} #{old_b}")
    new_line = PAYMENT_SET_LINE.format(n=2, fecha_min="03/11", count=2, ids=f"#{new_a} #{new_b}")
    assert replies == [
        (
            MESSAGES["BR-P02"].format(monto="100.00", set_lines=f"{old_line}\n{new_line}"),
            "BR-P02",
            [
                _button(f"p{proposal_id}:pay:0", "Opción 1"),
                _button(f"p{proposal_id}:pay:1", "Opción 2"),
                _button(f"p{proposal_id}:pay_no", BUTTON_NO),
            ],
        )
    ]


async def test_br_p02_outbox_buttons_build_a_valid_whatsapp_payload(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.12: BR-P02's outbox buttons build a WhatsApp payload, so no title overflows Meta's limit."""
    profile_id = _unique("profile")
    flow = _flow()
    older = datetime(2026, 11, 1, 15, 0, tzinfo=UTC)
    newer = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
    async with db_pool.connection() as conn:
        await _invoice(conn, profile_id, flow.flow_id, "60.00", created_at=older)
        await _invoice(conn, profile_id, flow.flow_id, "40.00", created_at=older)
        await _invoice(conn, profile_id, flow.flow_id, "70.00", created_at=newer)
        await _invoice(conn, profile_id, flow.flow_id, "30.00", created_at=newer)
        await _run(conn, profile_id, "15/11 100.00", flow)
        replies = await _replies(conn, profile_id)

    body, rule_id, buttons = replies[0]
    assert rule_id == "BR-P02"
    payload = buttons_payload(
        profile_id, body, [Button(id=button["id"], title=button["title"]) for button in buttons]
    )
    assert len(payload["interactive"]["action"]["buttons"]) == len(buttons)


async def test_command_amount_without_a_matching_set_lists_pending_as_br_p03(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.12: an amount no set adds up to answers BR-P03 with the pending invoices."""
    profile_id = _unique("profile")
    flow = _flow()
    older = datetime(2026, 11, 1, 15, 0, tzinfo=UTC)
    async with db_pool.connection() as conn:
        first = await _invoice(conn, profile_id, flow.flow_id, "60.00", created_at=older)
        second = await _invoice(conn, profile_id, flow.flow_id, "40.00", created_at=older)
        await _run(conn, profile_id, "15/11 100.01", flow)
        replies = await _replies(conn, profile_id)
        proposals = await _proposals(conn, profile_id)

    lines = "\n".join(
        [
            PAYMENT_LINE.format(id=first, fecha="01/11", monto="60.00"),
            PAYMENT_LINE.format(id=second, fecha="01/11", monto="40.00"),
        ]
    )
    text = MESSAGES["BR-P03"].format(monto="100.01", fecha="15/11", invoice_lines=lines)
    assert replies == [(text, "BR-P03", [])]
    assert proposals == []


async def test_command_ids_naming_a_non_candidate_answers_br_p03(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.12: the #id form with an id that is not a candidate answers BR-P03."""
    profile_id = _unique("profile")
    flow = _flow()
    older = datetime(2026, 11, 1, 15, 0, tzinfo=UTC)
    async with db_pool.connection() as conn:
        first = await _invoice(conn, profile_id, flow.flow_id, "60.00", created_at=older)
        second = await _invoice(conn, profile_id, flow.flow_id, "40.00", created_at=older)
        await _run(conn, profile_id, f"15/11 #{first} #{UNKNOWN_INVOICE_ID}", flow)
        replies = await _replies(conn, profile_id)
        proposals = await _proposals(conn, profile_id)

    lines = "\n".join(
        [
            PAYMENT_LINE.format(id=first, fecha="01/11", monto="60.00"),
            PAYMENT_LINE.format(id=second, fecha="01/11", monto="40.00"),
        ]
    )
    text = MESSAGES["BR-P03"].format(monto="60.00", fecha="15/11", invoice_lines=lines)
    assert replies == [(text, "BR-P03", [])]
    assert proposals == []


async def test_command_ids_naming_candidates_proposes_their_sum_as_br_p01(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.12: the #id form naming candidates answers BR-P01 with their summed amount."""
    profile_id = _unique("profile")
    flow = _flow()
    async with db_pool.connection() as conn:
        first = await _invoice(conn, profile_id, flow.flow_id, "60.00")
        second = await _invoice(conn, profile_id, flow.flow_id, "40.00")
        await _run(conn, profile_id, f"15/11 #{second} #{first}", flow)
        replies = await _replies(conn, profile_id)
        proposals = await _proposals(conn, profile_id)

    assert len(proposals) == 1
    proposal_id, paid_on, amount, sets = proposals[0]
    assert (paid_on, amount, sets) == (PAID_ON, Decimal("100.00"), [[second, first]])
    text = MESSAGES["BR-P01"].format(monto="100.00", fecha="15/11", n=2, lista=f"#{second} #{first}")
    assert replies == [
        (
            text,
            "BR-P01",
            [
                _button(f"p{proposal_id}:pay:0", BUTTON_YES),
                _button(f"p{proposal_id}:pay_no", BUTTON_NO),
            ],
        )
    ]


async def test_command_never_matches_another_profiles_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.12: another profile's invoice is never a candidate, so its amount alone answers BR-P03."""
    owner_profile = _unique("profile")
    profile_id = _unique("profile")
    flow = _flow()
    async with db_pool.connection() as conn:
        await _invoice(conn, owner_profile, flow.flow_id, "100.00")
        await _run(conn, profile_id, "15/11 100.00", flow)
        replies = await _replies(conn, profile_id)
        proposals = await _proposals(conn, profile_id)

    text = MESSAGES["BR-P03"].format(monto="100.00", fecha="15/11", invoice_lines="")
    assert replies == [(text, "BR-P03", [])]
    assert proposals == []


async def test_command_without_a_payment_flow_answers_the_help(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.12: /pagado with no payment flow configured answers HELP_PAGADO and stores nothing."""
    profile_id = _unique("profile")
    async with db_pool.connection() as conn:
        await _run(conn, profile_id, "15/11 100.00", None)
        replies = await _replies(conn, profile_id)
        proposals = await _proposals(conn, profile_id)

    assert replies == [(HELP_PAGADO, None, [])]
    assert proposals == []


async def test_press_yes_records_the_payment_and_answers_br_p04(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.12: [Sí] stores one payment, marks its invoices paid and queues one folder job each."""
    profile_id = _unique("profile")
    flow = _flow()
    async with db_pool.connection() as conn:
        proposal_id, ids = await _proposal_for(conn, profile_id, flow)
        await _press_through_intake(conn, _press(profile_id, f"p{proposal_id}:pay:0"))
        state = await _proposal_state(conn, proposal_id)
        payments = await _payment_count(conn, profile_id)
        payment_ids = await _payment_ids(conn, ids)
        jobs = [await _jobs(conn, invoice_id) for invoice_id in ids]
        replies = await _replies(conn, profile_id)

    assert state == "confirmed"
    assert payments == 1
    assert payment_ids[0] is not None
    assert set(payment_ids) == {payment_ids[0]}
    assert jobs == [[FOLDER_STATUS_STEP], [FOLDER_STATUS_STEP]]
    assert replies[-1] == (MESSAGES["BR-P04"].format(n=2), "BR-P04", [])


async def test_press_no_closes_the_proposal_without_a_payment(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.12: [No] stores no payment, closes the proposal rejected and answers nothing more."""
    profile_id = _unique("profile")
    flow = _flow()
    async with db_pool.connection() as conn:
        proposal_id, ids = await _proposal_for(conn, profile_id, flow)
        await _press_through_intake(conn, _press(profile_id, f"p{proposal_id}:pay_no"))
        state = await _proposal_state(conn, proposal_id)
        payments = await _payment_count(conn, profile_id)
        jobs = [await _jobs(conn, invoice_id) for invoice_id in ids]
        rules = [rule_id for _text, rule_id, _buttons in await _replies(conn, profile_id)]

    assert state == "rejected"
    assert payments == 0
    assert jobs == [[], []]
    assert rules == ["BR-P01"]


async def test_press_yes_twice_answers_br_s02_and_keeps_one_payment(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.12: the same [Sí] pressed twice answers BR-S02 and records one payment."""
    profile_id = _unique("profile")
    flow = _flow()
    async with db_pool.connection() as conn:
        proposal_id, ids = await _proposal_for(conn, profile_id, flow)
        button = f"p{proposal_id}:pay:0"
        await _press_through_intake(conn, _press(profile_id, button))
        await _press_through_intake(conn, _press(profile_id, button))
        state = await _proposal_state(conn, proposal_id)
        payments = await _payment_count(conn, profile_id)
        jobs = [await _jobs(conn, invoice_id) for invoice_id in ids]
        replies = await _replies(conn, profile_id)

    expected = MESSAGES["BR-S02"].format(id=ids[0], state_desc=state_desc(DONE))
    assert state == "confirmed"
    assert payments == 1
    assert jobs == [[FOLDER_STATUS_STEP], [FOLDER_STATUS_STEP]]
    assert replies[-1] == (expected, "BR-S02", [])


async def test_press_from_another_profile_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.12: another profile's press on a proposal changes nothing and answers nothing."""
    owner_profile = _unique("profile")
    other_profile = _unique("profile")
    flow = _flow()
    async with db_pool.connection() as conn:
        proposal_id, ids = await _proposal_for(conn, owner_profile, flow)
        await _press_through_intake(conn, _press(other_profile, f"p{proposal_id}:pay:0"))
        state = await _proposal_state(conn, proposal_id)
        payments = await _payment_count(conn, owner_profile)
        jobs = [await _jobs(conn, invoice_id) for invoice_id in ids]
        replies = await _replies(conn, other_profile)

    assert state == "pending"
    assert payments == 0
    assert jobs == [[], []]
    assert replies == []


async def test_press_yes_after_an_invoice_was_paid_answers_br_s02(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.12: [Sí] when one of its invoices was paid meanwhile answers BR-S02 and stores nothing."""
    profile_id = _unique("profile")
    flow = _flow()
    async with db_pool.connection() as conn:
        proposal_id, ids = await _proposal_for(conn, profile_id, flow)
        async with conn.transaction():
            await record_payment(conn, flow.flow_id, profile_id, PAID_ON, Decimal("100.00"), [ids[0]])
        await _press_through_intake(conn, _press(profile_id, f"p{proposal_id}:pay:0"))
        state = await _proposal_state(conn, proposal_id)
        payments = await _payment_count(conn, profile_id)
        payment_ids = await _payment_ids(conn, ids)
        jobs = [await _jobs(conn, invoice_id) for invoice_id in ids]
        replies = await _replies(conn, profile_id)

    expected = MESSAGES["BR-S02"].format(id=ids[0], state_desc=state_desc(DONE))
    assert state == "pending"
    assert payments == 1
    assert payment_ids[1] is None
    assert jobs == [[], []]
    assert replies[-1] == (expected, "BR-S02", [])


async def test_press_yes_after_25_hours_answers_br_s02_and_expires(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.12: a proposal older than 24 h answers BR-S02 and is closed expired."""
    profile_id = _unique("profile")
    flow = _flow()
    async with db_pool.connection() as conn:
        proposal_id, ids = await _proposal_for(conn, profile_id, flow)
        async with conn.transaction():
            await conn.execute(
                "UPDATE payment_proposals SET created_at = now() - interval '25 hours' WHERE id = %s",
                (proposal_id,),
            )
        await _press_through_intake(conn, _press(profile_id, f"p{proposal_id}:pay:0"))
        state = await _proposal_state(conn, proposal_id)
        payments = await _payment_count(conn, profile_id)
        jobs = [await _jobs(conn, invoice_id) for invoice_id in ids]
        replies = await _replies(conn, profile_id)

    expected = MESSAGES["BR-S02"].format(id=ids[0], state_desc=state_desc(DONE))
    assert state == "expired"
    assert payments == 0
    assert jobs == [[], []]
    assert replies[-1] == (expected, "BR-S02", [])
