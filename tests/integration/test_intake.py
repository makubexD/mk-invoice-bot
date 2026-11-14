"""Integration tests for intake: storage, edits, queueing, back-pressure and rollback.

Covers R-1.1, R-1.6, R-1.8, R-1.10, R-3.5, BR-S01 and BR-E01 against real Postgres.
"""

import itertools
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import Attachment, ChannelName, InboundMessage
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, RoutingRule, StoreDirectory
from invoicebot.extract.fields import Field, Reading
from invoicebot.pipeline.extract import ROUTE_STEP
from invoicebot.pipeline.intake import handle_inbound
from invoicebot.pipeline.route import make_route_handler
from invoicebot.queue.jobs import Job, claim
from invoicebot.routing.router import compile_rules
from invoicebot.rules.messages_es import EDIT_LATE, MESSAGES, state_desc
from invoicebot.store.flow_b import merge_flow_data
from invoicebot.store.invoices import create_invoice, save_reading, set_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

MAX_QUEUED = 10
LEASE_S = 60
EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
# From tests/synthetic_rucs.txt; 20000000002 fails its check digit.
OWNER_RUC = "20000000001"
BAD_RUC = "20000000002"
WORK_RUC = "51900000000"
WORK_FLOW = "work_microsoft"
DNI_BUYER = "40382716"
FULL_PROFILE = "yo"
#: One unique numero per reading, so no test claims another's natural key (D93).
_NEXT_NUMERO = itertools.count(40000)


class _Rollback(Exception):
    """Test-local failure that makes the caller roll its transaction back."""


def _profile() -> str:
    """Return a profile id no other test uses, so tests cannot interfere."""
    return f"profile-{uuid4().hex}"


def _external_id() -> str:
    """Return a channel message id no other test uses."""
    return f"ext-{uuid4().hex}"


def _photo() -> Attachment:
    """Return a photo attachment with a unique media reference."""
    return Attachment(kind="photo", media_ref=f"ref-{uuid4().hex}", mime="image/jpeg", size=1024)


def _message(
    profile_id: str,
    *,
    external_id: str,
    channel: ChannelName = "whatsapp",
    text: str = "",
    attachments: list[Attachment] | None = None,
) -> InboundMessage:
    """Build a canonical inbound message for profile_id."""
    return InboundMessage(
        channel=channel,
        external_id=external_id,
        profile_id=profile_id,
        sender_role="owner",
        text=text,
        attachments=[] if attachments is None else attachments,
        received_at=datetime.now(UTC),
    )


async def _today(pool: AsyncConnectionPool[AsyncConnection[Any]]) -> date:
    """Return today in Lima from the database clock, as the route handler sees it."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT (now() AT TIME ZONE 'America/Lima')::date")
        row = await cur.fetchone()
    assert row is not None
    return cast(date, row[0])


def _reading(fecha: date, *, buyer: str = WORK_RUC, buyer_type: str = "6") -> Reading:
    """Build a complete reading for one invoice, with its own numero (D93)."""
    return Reading(
        fields={
            "ruc_emisor": Field(OWNER_RUC, "qr", 1.0),
            "tipo": Field("01", "qr", 1.0),
            "serie": Field("F001", "qr", 1.0),
            "numero": Field(str(next(_NEXT_NUMERO)), "qr", 1.0),
            "igv": Field("7.61", "qr", 1.0),
            "total": Field("49.90", "qr", 1.0),
            "fecha_emision": Field(fecha.isoformat(), "qr", 1.0),
            "tipo_doc_adq": Field(buyer_type, "qr", 1.0),
            "num_doc_adq": Field(buyer, "qr", 1.0),
        },
        document_url=None,
    )


async def _invoice_in(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    state: str,
    reading: Reading | None,
    *,
    rule_id: str | None = None,
    answers: dict[str, Any] | None = None,
) -> int:
    """Create an invoice in ``state`` with ``reading`` and stored answers."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        if reading is not None:
            await save_reading(conn, invoice_id, reading)
        if answers:
            await merge_flow_data(conn, invoice_id, {"answers": answers})
        await set_state(conn, invoice_id, state, rule_id=rule_id)
    return invoice_id


async def _park(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    invoice_id: int,
    waiting_state: str,
    waiting_rule_id: str,
) -> None:
    """Move the invoice to PENDING_USER, storing the state and rule it waited in (D94)."""
    async with pool.connection() as conn, conn.transaction():
        await set_state(conn, invoice_id, "PENDING_USER", rule_id="BR-S08")
        await conn.execute(
            "UPDATE invoices SET waiting_state = %s, waiting_rule_id = %s WHERE id = %s",
            (waiting_state, waiting_rule_id, invoice_id),
        )


async def _waiting_row(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str | None, str | None]:
    """Return the invoice's ``(waiting_state, waiting_rule_id)``."""
    cur = await conn.execute(
        "SELECT waiting_state, waiting_rule_id FROM invoices WHERE id = %s",
        (invoice_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0], row[1]


def _config(profile_id: str) -> Config:
    """Return the example config with ``profile_id`` as the full profile and both buyer rules."""
    cfg = load_config(EXAMPLE)
    routing = cfg.routing.model_copy(
        update={
            "rules": [
                RoutingRule(id="BR-R01", when={"num_doc_adq": [OWNER_RUC]}, flow="personal_google"),
                RoutingRule(id="BR-R02", when={"num_doc_adq": [WORK_RUC]}, flow=WORK_FLOW),
            ]
        }
    )
    return cfg.model_copy(update={"profiles": {profile_id: cfg.profiles[FULL_PROFILE]}, "routing": routing})


async def _run_route(pool: AsyncConnectionPool[AsyncConnection[Any]], cfg: Config, invoice_id: int) -> None:
    """Run the route handler on ``invoice_id`` as the queue would."""
    job = Job(
        id=1, invoice_id=invoice_id, step=ROUTE_STEP, attempts=0, crashes=0, lease_until=datetime.now(UTC)
    )
    handler = make_route_handler(pool, cfg, StoreDirectory(stores={}, portals={}), compile_rules(cfg.routing))
    await handler(job)


async def _state_rule(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str, str | None]:
    """Return ``(state, rule_id)`` of an invoice."""
    cur = await conn.execute("SELECT state, rule_id FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0]), None if row[1] is None else str(row[1])


async def _jobs(conn: AsyncConnection[Any], invoice_id: int) -> list[str]:
    """Return the steps of the invoice's queued jobs, in insertion order."""
    cur = await conn.execute("SELECT step FROM jobs WHERE invoice_id = %s ORDER BY id", (invoice_id,))
    return [str(step) for (step,) in await cur.fetchall()]


async def _replies(conn: AsyncConnection[Any], profile_id: str) -> list[tuple[str, str | None]]:
    """Return the outbox's ``(text, rule_id)`` rows for ``profile_id``, oldest first."""
    cur = await conn.execute(
        """
        SELECT payload_json->>'text', payload_json->>'rule_id'
        FROM outbox
        WHERE "to" = %s
        ORDER BY id
        """,
        (profile_id,),
    )
    rows = await cur.fetchall()
    return [(str(text), rule_id) for text, rule_id in rows]


async def _fields_json(conn: AsyncConnection[Any], invoice_id: int) -> str:
    """Return the invoice's stored reading as text, for an unchanged comparison."""
    cur = await conn.execute("SELECT fields_json::text FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    assert row[0] is not None
    return str(row[0])


async def _field(conn: AsyncConnection[Any], invoice_id: int, name: str) -> tuple[str | None, str | None]:
    """Return ``(value, source)`` of one stored reading field."""
    cur = await conn.execute(
        """
        SELECT fields_json->'fields'->%s->>'value', fields_json->'fields'->%s->>'source'
        FROM invoices
        WHERE id = %s
        """,
        (name, name, invoice_id),
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0], row[1]


async def _counts(conn: AsyncConnection[Any], profile_id: str) -> tuple[int, int, int, int]:
    """Return (messages, invoices, jobs, outbox rows) owned by profile_id."""
    cur = await conn.execute("SELECT count(*) FROM messages WHERE profile_id = %s", (profile_id,))
    row = await cur.fetchone()
    assert row is not None
    messages = int(row[0])
    cur = await conn.execute("SELECT count(*) FROM invoices WHERE profile_id = %s", (profile_id,))
    row = await cur.fetchone()
    assert row is not None
    invoices = int(row[0])
    cur = await conn.execute(
        """
        SELECT count(*)
        FROM jobs AS j
        JOIN invoices AS i ON i.id = j.invoice_id
        WHERE i.profile_id = %s
        """,
        (profile_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    jobs = int(row[0])
    cur = await conn.execute('SELECT count(*) FROM outbox WHERE "to" = %s', (profile_id,))
    row = await cur.fetchone()
    assert row is not None
    return messages, invoices, jobs, int(row[0])


async def test_photo_creates_invoice_job_and_acknowledgement(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.1: a photo becomes a RECEIVED invoice, one job and one BR-S09 outbox row."""
    profile_id = _profile()
    msg = _message(profile_id, external_id=_external_id(), attachments=[_photo()])

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        assert result.status == "queued"
        invoice_id = result.invoice_id
        assert invoice_id is not None
        cur = await conn.execute(
            "SELECT profile_id, origin_channel, state FROM invoices WHERE id = %s",
            (invoice_id,),
        )
        invoice = await cur.fetchone()
        cur = await conn.execute("SELECT step FROM jobs WHERE invoice_id = %s", (invoice_id,))
        jobs = await cur.fetchall()
        cur = await conn.execute(
            "SELECT state, payload_json->>'rule_id', payload_json->>'text' FROM outbox WHERE \"to\" = %s",
            (profile_id,),
        )
        outbox_rows = await cur.fetchall()
        cur = await conn.execute(
            "SELECT invoice_id FROM messages WHERE channel = %s AND external_id = %s",
            (msg.channel, msg.external_id),
        )
        linked = await cur.fetchone()

    assert invoice == (profile_id, "whatsapp", "RECEIVED")
    assert jobs == [("download_media",)]
    assert len(outbox_rows) == 1
    state, rule_id, text = outbox_rows[0]
    assert state == "pending"
    assert rule_id == "BR-S09"
    assert str(invoice_id) in text
    assert linked == (invoice_id,)


async def test_redelivered_message_is_a_duplicate(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.8: the same (channel, external_id) twice stores one invoice, job and reply."""
    profile_id = _profile()
    msg = _message(profile_id, external_id=_external_id(), attachments=[_photo()])

    async with db_pool.connection() as conn:
        first = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        second = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        counts = await _counts(conn, profile_id)

    assert first.status == "queued"
    assert second.status == "duplicate"
    assert second.invoice_id is None
    assert counts == (1, 1, 1, 1)


async def test_text_message_is_only_stored(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """R-1.10: a non-photo is stored and opens the window, with no invoice, job or reply."""
    profile_id = _profile()
    msg = _message(profile_id, external_id=_external_id(), text="hola")

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        counts = await _counts(conn, profile_id)
        cur = await conn.execute(
            "SELECT last_inbound_at FROM channel_windows WHERE channel = %s AND recipient = %s",
            (msg.channel, profile_id),
        )
        window = await cur.fetchone()

    assert result.status == "stored"
    assert result.invoice_id is None
    assert counts == (1, 0, 0, 0)
    assert window is not None
    assert window[0] == msg.received_at


async def test_five_photos_are_claimed_in_arrival_order(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.6: five photos make five invoices whose jobs are claimed in arrival order."""
    profile_id = _profile()
    messages = [_message(profile_id, external_id=_external_id(), attachments=[_photo()]) for _ in range(5)]

    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM jobs")
        results = [await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED) for msg in messages]
        claimed: list[Job] = []
        for _ in range(5):
            job = await claim(conn, "worker-intake", LEASE_S)
            assert job is not None
            claimed.append(job)

    assert [result.status for result in results] == ["queued"] * 5
    assert [job.invoice_id for job in claimed] == [result.invoice_id for result in results]
    assert {job.step for job in claimed} == {"download_media"}


async def test_back_pressure_replies_and_keeps_the_message(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S01: a third photo with two active jobs is stored but answered with BR-S01."""
    profile_id = _profile()
    messages = [_message(profile_id, external_id=_external_id(), attachments=[_photo()]) for _ in range(3)]

    async with db_pool.connection() as conn:
        results = [await handle_inbound(conn, msg, max_queued_per_sender=2) for msg in messages]
        counts = await _counts(conn, profile_id)
        cur = await conn.execute(
            """
            SELECT payload_json->>'rule_id', payload_json->>'text'
            FROM outbox
            WHERE "to" = %s
            ORDER BY id
            """,
            (profile_id,),
        )
        outbox_rows = await cur.fetchall()

    assert [result.status for result in results] == ["queued", "queued", "back_pressure"]
    assert results[2].invoice_id is None
    assert counts == (3, 2, 2, 3)
    back_pressure = [text for rule_id, text in outbox_rows if rule_id == "BR-S01"]
    assert len(back_pressure) == 1
    assert "2" in back_pressure[0]


async def test_back_pressure_is_per_profile(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S01: one profile's full queue does not stop another profile's photo."""
    busy_profile = _profile()
    other_profile = _profile()

    async with db_pool.connection() as conn:
        first = await handle_inbound(
            conn,
            _message(busy_profile, external_id=_external_id(), attachments=[_photo()]),
            max_queued_per_sender=1,
        )
        blocked = await handle_inbound(
            conn,
            _message(busy_profile, external_id=_external_id(), attachments=[_photo()]),
            max_queued_per_sender=1,
        )
        other = await handle_inbound(
            conn,
            _message(other_profile, external_id=_external_id(), attachments=[_photo()]),
            max_queued_per_sender=1,
        )
        busy_counts = await _counts(conn, busy_profile)
        other_counts = await _counts(conn, other_profile)

    assert first.status == "queued"
    assert blocked.status == "back_pressure"
    assert other.status == "queued"
    assert other.invoice_id is not None
    assert busy_counts == (2, 1, 1, 2)
    assert other_counts == (1, 1, 1, 1)


async def test_caller_rollback_leaves_nothing_behind(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.8: a caller failure after intake rolls back message, invoice, job and reply."""
    profile_id = _profile()
    msg = _message(profile_id, external_id=_external_id(), attachments=[_photo()])

    async with db_pool.connection() as conn:
        with pytest.raises(_Rollback):
            async with conn.transaction():
                result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
                assert result.status == "queued"
                raise _Rollback
        counts = await _counts(conn, profile_id)
        cur = await conn.execute("SELECT count(*) FROM messages WHERE external_id = %s", (msg.external_id,))
        row = await cur.fetchone()

    assert row is not None
    assert row[0] == 0
    assert counts == (0, 0, 0, 0)


async def test_edit_reroutes_a_confirming_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.5, D97: total=10.00 on a CONFIRMING invoice is stored as the user's and routes again."""
    profile_id = _profile()
    invoice_id = await _invoice_in(
        db_pool,
        profile_id,
        "CONFIRMING",
        _reading(await _today(db_pool)),
        rule_id="BR-R03",
        answers={"flow": WORK_FLOW},
    )
    msg = _message(profile_id, external_id=_external_id(), text="total=10.00")

    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        state_rule = await _state_rule(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        total = await _field(conn, invoice_id, "total")
        cur = await conn.execute(
            "SELECT total, flow_data_json->'answers' FROM invoices WHERE id = %s", (invoice_id,)
        )
        row = await cur.fetchone()

    assert result.invoice_id == invoice_id
    assert state_rule == ("ROUTING", None)
    assert jobs == ["route"]
    assert total == ("10.00", "user")
    assert row == (Decimal("10.00"), {})


async def test_complete_edit_reroutes_an_extract_failed_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.5, BR-E01: a complete typed reading on an EXTRACT_FAILED invoice goes to ROUTING."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, "EXTRACT_FAILED", None, rule_id="BR-E01")
    msg = _message(
        profile_id,
        external_id=_external_id(),
        text=f"ruc={OWNER_RUC}\nserie=F001\nnumero=4321\nfecha=01/02/2025\ntotal=45.50",
    )

    async with db_pool.connection() as conn:
        await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        state_rule = await _state_rule(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        cur = await conn.execute(
            "SELECT ruc_emisor, tipo, serie, numero, fecha, total FROM invoices WHERE id = %s",
            (invoice_id,),
        )
        row = await cur.fetchone()
        source = await _field(conn, invoice_id, "ruc_emisor")

    assert state_rule == ("ROUTING", None)
    assert jobs == ["route"]
    assert row == (20000000001, "01", "F001", "4321", date(2025, 2, 1), Decimal("45.50"))
    assert source == (OWNER_RUC, "user")


async def test_complete_edit_resumes_a_parked_extract_failed_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D94: a complete edit on a PENDING_USER invoice unparks EXTRACT_FAILED and routes it."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, "EXTRACT_FAILED", None, rule_id="BR-E01")
    await _park(db_pool, invoice_id, "EXTRACT_FAILED", "BR-E01")
    msg = _message(
        profile_id,
        external_id=_external_id(),
        text=f"ruc={OWNER_RUC}\nserie=F001\nnumero=4321\nfecha=01/02/2025\ntotal=45.50",
    )

    async with db_pool.connection() as conn:
        await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        state_rule = await _state_rule(conn, invoice_id)
        waiting = await _waiting_row(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        ruc = await _field(conn, invoice_id, "ruc_emisor")

    assert state_rule == ("ROUTING", None)
    assert waiting == (None, None)
    assert jobs == ["route"]
    assert ruc == (OWNER_RUC, "user")


async def test_incomplete_edit_on_extract_failed_asks_for_data_and_stays(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.5, BR-E01: only total=10.00 leaves a required field missing, so BR-E01 repeats."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, "EXTRACT_FAILED", None, rule_id="BR-E01")
    msg = _message(profile_id, external_id=_external_id(), text="total=10.00")

    async with db_pool.connection() as conn:
        await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        state_rule = await _state_rule(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        replies = await _replies(conn, profile_id)

    assert state_rule == ("EXTRACT_FAILED", "BR-E01")
    assert jobs == []
    assert replies == [(MESSAGES["BR-E01"], "BR-E01")]


async def test_edit_in_delivering_is_refused_as_late(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.5, D97: an edit in DELIVERING gets EDIT_LATE and changes nothing."""
    profile_id = _profile()
    invoice_id = await _invoice_in(db_pool, profile_id, "DELIVERING", _reading(await _today(db_pool)))
    msg = _message(profile_id, external_id=_external_id(), text="total=10.00")

    async with db_pool.connection() as conn:
        before = await _fields_json(conn, invoice_id)
        await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        state_rule = await _state_rule(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        replies = await _replies(conn, profile_id)
        after = await _fields_json(conn, invoice_id)

    expected = EDIT_LATE.format(id=invoice_id, state_desc=state_desc("DELIVERING"))
    assert state_rule == ("DELIVERING", None)
    assert jobs == []
    assert replies == [(expected, None)]
    assert after == before


async def test_bad_ruc_edit_answers_br_e02_and_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.5, BR-E02: ruc=<bad> answers BR-E02 and leaves the invoice and its reading as they were."""
    profile_id = _profile()
    invoice_id = await _invoice_in(
        db_pool, profile_id, "CONFIRMING", _reading(await _today(db_pool)), rule_id="BR-R03"
    )
    msg = _message(profile_id, external_id=_external_id(), text=f"ruc={BAD_RUC}")

    async with db_pool.connection() as conn:
        before = await _fields_json(conn, invoice_id)
        await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        state_rule = await _state_rule(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        replies = await _replies(conn, profile_id)
        after = await _fields_json(conn, invoice_id)

    assert state_rule == ("CONFIRMING", "BR-R03")
    assert jobs == []
    assert replies == [(MESSAGES["BR-E02"].format(ruc=BAD_RUC), "BR-E02")]
    assert after == before


async def test_cliente_edit_reroutes_a_br_r03_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.5, BR-R03: cliente=<work RUC> on a BR-R03 invoice lands in the work flow."""
    profile_id = _profile()
    cfg = _config(profile_id)
    invoice_id = await _invoice_in(
        db_pool,
        profile_id,
        "ROUTING",
        _reading(await _today(db_pool), buyer=DNI_BUYER, buyer_type="1"),
    )
    await _run_route(db_pool, cfg, invoice_id)
    msg = _message(profile_id, external_id=_external_id(), text=f"cliente={WORK_RUC}")

    async with db_pool.connection() as conn:
        assert await _state_rule(conn, invoice_id) == ("CONFIRMING", "BR-R03")
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED)
        state_rule = await _state_rule(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        buyer = await _field(conn, invoice_id, "num_doc_adq")

    assert result.invoice_id == invoice_id
    assert state_rule == ("ROUTING", None)
    assert jobs == ["route"]
    assert buyer == (WORK_RUC, "user")

    await _run_route(db_pool, cfg, invoice_id)
    async with db_pool.connection() as conn:
        final_state = await _state_rule(conn, invoice_id)
        cur = await conn.execute("SELECT flow_id FROM invoices WHERE id = %s", (invoice_id,))
        row = await cur.fetchone()

    assert final_state == ("FETCHING_DOCS", "BR-R02")
    assert row == (WORK_FLOW,)
