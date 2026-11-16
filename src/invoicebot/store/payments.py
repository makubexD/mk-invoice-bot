"""Payment persistence: match candidates, proposals and recorded payments (R-7.12, D107).

Every function takes the caller's connection and never commits: the caller owns
the transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final, cast

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from invoicebot.extract.fields import reading_from_json
from invoicebot.states import DONE

#: How far back, in days, a payment may match invoices (R-7.12).
CANDIDATE_WINDOW_DAYS: Final = 180

#: The most invoice candidates one payment match considers (R-7.12).
CANDIDATE_LIMIT: Final = 20

#: The states a payment proposal may be in (0009 CHECK).
PENDING: Final = "pending"
CONFIRMED: Final = "confirmed"
REJECTED: Final = "rejected"
EXPIRED: Final = "expired"

#: Every state a payment proposal may be closed to.
PROPOSAL_STATES: Final = (PENDING, CONFIRMED, REJECTED, EXPIRED)

_CENT = Decimal("0.01")


@dataclass(frozen=True)
class Candidate:
    """A sent, unpaid invoice the matcher may include in a payment (R-7.12)."""

    invoice_id: int
    created_at: datetime
    claimed_cents: int


@dataclass(frozen=True)
class Proposal:
    """A stored payment proposal: the amount and the invoice-id sets it offered (R-7.12)."""

    id: int
    profile_id: str
    flow_id: str
    paid_on: date
    amount: Decimal
    sets: tuple[tuple[int, ...], ...]
    state: str
    created_at: datetime


class AlreadyPaid(Exception):
    """An invoice named for a payment already belongs to another one (R-7.12)."""

    def __init__(self, invoice_ids: Sequence[int]) -> None:
        self.invoice_ids = tuple(sorted(set(invoice_ids)))
        super().__init__(f"invoices already paid: {list(self.invoice_ids)}")


def claimed_cents(total: Decimal, cap: Decimal | None) -> int:
    """Return ``min(total, cap)`` in whole cents, half up; ``None`` means no cap (R-7.13)."""
    capped = total if cap is None else min(total, cap)
    return int(capped.quantize(_CENT, rounding=ROUND_HALF_UP) * 100)


async def candidates(
    conn: AsyncConnection[Any],
    profile_id: str,
    flow_id: str,
    cap: Decimal | None,
) -> list[Candidate]:
    """Return the profile's sent, unpaid invoices of the last 180 days, oldest first (R-7.12).

    Each invoice's claimed amount is its stored total capped by ``cap`` (R-7.13). An
    invoice with no usable total cannot match an amount, so it is skipped. Does not commit.
    """
    cur = await conn.execute(
        """
        SELECT i.id, i.created_at, i.fields_json
        FROM invoices AS i
        JOIN batches AS b ON b.id = i.batch_id
        WHERE i.profile_id = %s
          AND i.flow_id = %s
          AND i.state = %s
          AND b.state = 'sent'
          AND i.payment_id IS NULL
          AND i.created_at > now() - make_interval(days => %s)
        ORDER BY i.created_at, i.id
        LIMIT %s
        """,
        (profile_id, flow_id, DONE, CANDIDATE_WINDOW_DAYS, CANDIDATE_LIMIT),
    )
    found: list[Candidate] = []
    for invoice_id, created_at, fields_json in await cur.fetchall():
        total = None if fields_json is None else reading_from_json(fields_json).amount("total")
        # An invoice without a stored total claims no amount, so no set can ever need it.
        if total is None:
            continue
        found.append(
            Candidate(
                invoice_id=int(invoice_id),
                created_at=cast(datetime, created_at),
                claimed_cents=claimed_cents(total, cap),
            )
        )
    return found


async def add_proposal(
    conn: AsyncConnection[Any],
    profile_id: str,
    flow_id: str,
    paid_on: date,
    amount: Decimal,
    sets: Sequence[tuple[int, ...]],
) -> int:
    """Store a pending payment proposal and return its id (R-7.12); does not commit."""
    cur = await conn.execute(
        """
        INSERT INTO payment_proposals (profile_id, flow_id, paid_on, amount, sets_json)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (profile_id, flow_id, paid_on, amount, Jsonb([list(ids) for ids in sets])),
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("INSERT INTO payment_proposals RETURNING id yielded no row")
    return int(row[0])


async def get_proposal(conn: AsyncConnection[Any], proposal_id: int) -> Proposal | None:
    """Return the stored proposal, or None when it does not exist (R-7.12)."""
    cur = await conn.execute(
        """
        SELECT id, profile_id, flow_id, paid_on, amount, sets_json, state, created_at
        FROM payment_proposals
        WHERE id = %s
        """,
        (proposal_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return Proposal(
        id=int(row[0]),
        profile_id=str(row[1]),
        flow_id=str(row[2]),
        paid_on=cast(date, row[3]),
        amount=cast(Decimal, row[4]),
        sets=tuple(tuple(int(invoice_id) for invoice_id in ids) for ids in row[5]),
        state=str(row[6]),
        created_at=cast(datetime, row[7]),
    )


async def close_proposal(conn: AsyncConnection[Any], proposal_id: int, state: str) -> None:
    """Move the proposal to state; does not commit (R-7.12)."""
    if state not in PROPOSAL_STATES:
        raise ValueError(f"unknown proposal state {state!r}")
    await conn.execute(
        "UPDATE payment_proposals SET state = %s WHERE id = %s",
        (state, proposal_id),
    )


async def record_payment(
    conn: AsyncConnection[Any],
    flow_id: str,
    profile_id: str,
    paid_on: date,
    amount: Decimal,
    invoice_ids: Sequence[int],
) -> int:
    """Store a payment and link its invoices, returning the payment id (R-7.12).

    Only invoices without a payment are linked. When an invoice already belongs to
    another payment, :class:`AlreadyPaid` carries the ids and the caller's rollback
    removes the payment row. Does not commit.
    """
    cur = await conn.execute(
        """
        INSERT INTO payments (flow_id, profile_id, paid_on, amount)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (flow_id, profile_id, paid_on, amount),
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("INSERT INTO payments RETURNING id yielded no row")
    payment_id = int(row[0])
    cur = await conn.execute(
        "UPDATE invoices SET payment_id = %s WHERE id = ANY(%s) AND payment_id IS NULL",
        (payment_id, list(invoice_ids)),
    )
    if cur.rowcount < len(set(invoice_ids)):
        raise AlreadyPaid(invoice_ids)
    return payment_id
