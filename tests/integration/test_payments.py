"""Integration tests for the ``/pagado`` command (R-7.12, D107).

Commands run through :func:`handle_command` against real Postgres; every test uses its
own profile, so no outbox row, proposal or invoice leaks between them.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import ChannelName, InboundMessage
from invoicebot.extract.fields import Field, Reading
from invoicebot.pipeline.commands import CommandContext, PaymentFlow, handle_command
from invoicebot.rules.messages_es import (
    BUTTON_NO,
    BUTTON_YES,
    HELP_PAGADO,
    MESSAGES,
    PAYMENT_LINE,
    PAYMENT_SET_LABEL,
)
from invoicebot.states import DONE
from invoicebot.store.flow_b import open_batch, set_batch_state
from invoicebot.store.invoices import create_invoice, save_reading, set_flow, set_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

CHANNEL: ChannelName = "whatsapp"
#: The owner's timezone, for the dates the command prints (R-7.12).
TIMEZONE = "America/Lima"
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
    old_label = PAYMENT_SET_LABEL.format(fecha_min="01/11", n=2, ids=f"#{old_a} #{old_b}")
    new_label = PAYMENT_SET_LABEL.format(fecha_min="03/11", n=2, ids=f"#{new_a} #{new_b}")
    assert replies == [
        (
            MESSAGES["BR-P02"].format(monto="100.00"),
            "BR-P02",
            [
                _button(f"p{proposal_id}:pay:0", old_label),
                _button(f"p{proposal_id}:pay:1", new_label),
                _button(f"p{proposal_id}:pay_no", BUTTON_NO),
            ],
        )
    ]


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
