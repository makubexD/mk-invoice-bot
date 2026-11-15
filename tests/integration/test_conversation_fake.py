"""Conversation-level integration tests delivering through the fake channel.

Covers R-1.1, R-1.4, R-1.6, R-1.8, R-2.1, R-3.2, R-3.4, R-3.5, R-3.6, R-3.7, R-10.1, BR-S01,
BR-S02, BR-S07, BR-S08, BR-E01, BR-E04 and BR-R03 against real Postgres, with the outbox
sender handing each message to :class:`FakeChannel`.
"""

import itertools
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import Attachment, InboundMessage
from invoicebot.channels.fake import FakeChannel
from invoicebot.channels.whatsapp.payloads import button_id
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, RoutingRule, StoreDirectory
from invoicebot.extract.fields import Field, Reading
from invoicebot.pipeline.extract import ROUTE_STEP
from invoicebot.pipeline.intake import handle_inbound
from invoicebot.pipeline.park import park_stale_questions
from invoicebot.pipeline.replies import resolve_reply
from invoicebot.pipeline.route import make_route_handler
from invoicebot.queue.jobs import Job
from invoicebot.queue.outbox import OutboxSender
from invoicebot.routing.router import compile_rules
from invoicebot.store.invoices import create_invoice, save_reading, set_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

MAX_QUEUED = 10
#: The owner timezone, for the dates a command prints (S11.4).
TIMEZONE = "America/Lima"
VALID_STATES: dict[str, frozenset[str]] = {"confirm": frozenset({"CONFIRMING"})}
EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
# Both RUCs come from tests/synthetic_rucs.txt; the DNI is the one test_pipeline_route uses.
OWNER_RUC = "20000000001"
WORK_RUC = "51900000000"
DNI_BUYER = "40382716"
OWNER_FLOW = "personal_google"
WORK_FLOW = "work_microsoft"
FULL_PROFILE = "yo"
TOTAL = "49.90"
#: One unique numero per reading, in a range no other test file uses (D93).
_NEXT_NUMERO = itertools.count(50000)


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
    text: str = "",
    attachments: list[Attachment] | None = None,
    button_id: str | None = None,
) -> InboundMessage:
    """Build a canonical inbound message for profile_id."""
    return InboundMessage(
        channel="whatsapp",
        external_id=_external_id(),
        profile_id=profile_id,
        sender_role="owner",
        text=text,
        attachments=[] if attachments is None else attachments,
        button_id=button_id,
        received_at=datetime.now(UTC),
    )


async def _clear_outbox(conn: AsyncConnection[Any]) -> None:
    """Remove every outbox row so a test only sends what it queued."""
    await conn.execute("DELETE FROM outbox")


def _sender(pool: AsyncConnectionPool[AsyncConnection[Any]], fake: FakeChannel) -> OutboxSender:
    """Build an outbox sender that delivers whatsapp rows to fake."""
    return OutboxSender(pool, {"whatsapp": fake.transport()})


def _config(profile_id: str) -> Config:
    """Return the example config with one profile under profile_id and both buyer rules."""
    cfg = load_config(EXAMPLE_CONFIG)
    routing = cfg.routing.model_copy(
        update={
            "rules": [
                RoutingRule(id="BR-R01", when={"num_doc_adq": [OWNER_RUC]}, flow=OWNER_FLOW),
                RoutingRule(id="BR-R02", when={"num_doc_adq": [WORK_RUC]}, flow=WORK_FLOW),
            ]
        }
    )
    return cfg.model_copy(update={"profiles": {profile_id: cfg.profiles[FULL_PROFILE]}, "routing": routing})


def _reading(
    fecha: date,
    numero: int,
    *,
    buyer: str = WORK_RUC,
    buyer_type: str = "6",
    total: str = TOTAL,
) -> Reading:
    """Build a complete reading with its own numero (D93)."""
    return Reading(
        fields={
            "ruc_emisor": Field(OWNER_RUC, "qr", 1.0),
            "tipo": Field("01", "qr", 1.0),
            "serie": Field("F001", "qr", 1.0),
            "numero": Field(str(numero), "qr", 1.0),
            "igv": Field("7.61", "qr", 1.0),
            "total": Field(total, "qr", 1.0),
            "fecha_emision": Field(fecha.isoformat(), "qr", 1.0),
            "tipo_doc_adq": Field(buyer_type, "qr", 1.0),
            "num_doc_adq": Field(buyer, "qr", 1.0),
        },
        document_url=None,
    )


async def _today(pool: AsyncConnectionPool[AsyncConnection[Any]]) -> date:
    """Return today in Lima from the database clock, as the route handler sees it."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT (now() AT TIME ZONE 'America/Lima')::date")
        row = await cur.fetchone()
    assert row is not None
    return cast(date, row[0])


async def _start_routing(
    pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int, reading: Reading
) -> None:
    """Store the reading an extract would have written and move the invoice to ROUTING."""
    async with pool.connection() as conn, conn.transaction():
        await save_reading(conn, invoice_id, reading)
        await set_state(conn, invoice_id, "ROUTING")


async def _run_route(pool: AsyncConnectionPool[AsyncConnection[Any]], cfg: Config, invoice_id: int) -> None:
    """Run the route handler on invoice_id's newest route job, as the queue would (D95)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT max(id) FROM jobs WHERE invoice_id = %s AND step = %s", (invoice_id, ROUTE_STEP)
        )
        row = await cur.fetchone()
    job_id = 1 if row is None or row[0] is None else int(row[0])
    job = Job(
        id=job_id,
        invoice_id=invoice_id,
        step=ROUTE_STEP,
        attempts=0,
        crashes=0,
        lease_until=datetime.now(UTC),
    )
    handler = make_route_handler(pool, cfg, StoreDirectory(stores={}, portals={}), compile_rules(cfg.routing))
    await handler(job)


async def _photo_invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    cfg: Config,
    reading: Reading,
) -> int:
    """Intake a photo, store its reading and route it, returning the invoice id."""
    msg = _message(profile_id, attachments=[_photo()])
    async with pool.connection() as conn:
        intake = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED, timezone=TIMEZONE)
    assert intake.invoice_id is not None
    await _start_routing(pool, intake.invoice_id, reading)
    await _run_route(pool, cfg, intake.invoice_id)
    return intake.invoice_id


async def _state_rule(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str, str | None]:
    """Return ``(state, rule_id)`` of an invoice."""
    cur = await conn.execute("SELECT state, rule_id FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0]), None if row[1] is None else str(row[1])


async def _waiting(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str | None, str | None]:
    """Return the invoice's ``(waiting_state, waiting_rule_id)``."""
    cur = await conn.execute(
        "SELECT waiting_state, waiting_rule_id FROM invoices WHERE id = %s", (invoice_id,)
    )
    row = await cur.fetchone()
    assert row is not None
    return (None if row[0] is None else str(row[0])), (None if row[1] is None else str(row[1]))


async def test_one_photo_sends_the_acknowledgement(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.1: one photo sends one BR-S09 whose text carries the invoice id."""
    fake = FakeChannel()
    profile_id = _profile()
    msg = _message(profile_id, attachments=[_photo()])

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        result = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED, timezone=TIMEZONE)

    sender = _sender(db_pool, fake)
    assert await sender.send_pending() == 1
    assert fake.rule_ids() == ["BR-S09"]
    assert result.invoice_id is not None
    assert str(result.invoice_id) in fake.texts()[0]


async def test_three_photos_with_two_queued_send_back_pressure_last(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.6, BR-S01: two acks then BR-S01, in the order the photos arrived."""
    fake = FakeChannel()
    profile_id = _profile()
    messages = [_message(profile_id, attachments=[_photo()]) for _ in range(3)]

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        results = [
            await handle_inbound(conn, msg, max_queued_per_sender=2, timezone=TIMEZONE) for msg in messages
        ]

    sender = _sender(db_pool, fake)
    assert await sender.send_pending() == 3
    assert [result.status for result in results] == ["queued", "queued", "back_pressure"]
    assert fake.rule_ids() == ["BR-S09", "BR-S09", "BR-S01"]


async def test_duplicate_photo_sends_nothing_more(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-1.8: a redelivered photo is stored once and sends no second message."""
    fake = FakeChannel()
    profile_id = _profile()
    msg = _message(profile_id, attachments=[_photo()])
    sender = _sender(db_pool, fake)

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        first = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED, timezone=TIMEZONE)
    assert await sender.send_pending() == 1

    async with db_pool.connection() as conn:
        second = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED, timezone=TIMEZONE)

    assert first.status == "queued"
    assert second.status == "duplicate"
    assert await sender.send_pending() == 0
    assert fake.rule_ids() == ["BR-S09"]


async def test_a_stale_button_sends_br_s02(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """BR-S02: a button pressed for a DONE invoice sends one BR-S02 and resolves to None."""
    fake = FakeChannel()
    profile_id = _profile()

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await set_state(conn, invoice_id, "DONE")

    msg = _message(profile_id, button_id=f"{invoice_id}:confirm")
    async with db_pool.connection() as conn:
        resolved = await resolve_reply(conn, msg, VALID_STATES, {})

    assert resolved is None
    sender = _sender(db_pool, fake)
    assert await sender.send_pending() == 1
    assert fake.rule_ids() == ["BR-S02"]


async def test_fetch_media_returns_registered_paths_only(tmp_path: Path) -> None:
    """R-1.4: fetch_media serves a registered path and raises KeyError for an unknown ref."""
    fake = FakeChannel()
    path = tmp_path / "photo.jpg"
    fake.add_media("ref-known", path)

    assert await fake.fetch_media("ref-known") == path
    with pytest.raises(KeyError, match="ref-unknown"):
        await fake.fetch_media("ref-unknown")


async def test_qr_photo_gets_the_ack_then_the_summary(read_conversation: Any) -> None:
    """R-2.1, R-3.4: a QR photo for the work RUC gets "Recibido", then the summary naming the work flow."""
    fake, _ = await read_conversation(True)

    assert fake.rule_ids() == ["BR-S09", "BR-R02"]
    assert "Recibido" in fake.texts()[0]
    assert "➡️ Trabajo (Microsoft)" in fake.texts()[1]


async def test_blank_photo_gets_the_ack_then_br_e01(read_conversation: Any) -> None:
    """R-2.13, BR-E01: a photo with no QR gets "Recibido", then BR-E01."""
    fake, _ = await read_conversation(False)

    assert fake.rule_ids() == ["BR-S09", "BR-E01"]
    assert "Recibido" in fake.texts()[0]


async def test_dni_buyer_picks_a_flow_and_gets_the_summary(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.2, BR-R03: a DNI photo asks which flow; the second flow's press routes it to a summary."""
    fake = FakeChannel()
    profile_id = _profile()
    cfg = _config(profile_id)
    sender = _sender(db_pool, fake)
    msg = _message(profile_id, attachments=[_photo()])

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        intake = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED, timezone=TIMEZONE)
    assert intake.invoice_id is not None
    invoice_id = intake.invoice_id
    reading = _reading(await _today(db_pool), next(_NEXT_NUMERO), buyer=DNI_BUYER, buyer_type="1")
    await _start_routing(db_pool, invoice_id, reading)
    await _run_route(db_pool, cfg, invoice_id)
    assert await sender.send_pending() == 2

    async with db_pool.connection() as conn:
        assert await _state_rule(conn, invoice_id) == ("CONFIRMING", "BR-R03")
    assert fake.rule_ids() == ["BR-S09", "BR-R03"]
    assert "¿En qué flujo lo registro?" in fake.texts()[-1]

    pressed = _message(profile_id, button_id=fake.sent[-1].buttons[1].id)
    async with db_pool.connection() as conn:
        await handle_inbound(conn, pressed, max_queued_per_sender=MAX_QUEUED, timezone=TIMEZONE)
    await _run_route(db_pool, cfg, invoice_id)
    assert await sender.send_pending() == 1

    async with db_pool.connection() as conn:
        assert await _state_rule(conn, invoice_id) == ("FETCHING_DOCS", "BR-R03")
    summary = fake.sent[-1]
    assert "Total: S/ 49.90" in summary.text
    assert "➡️ Trabajo (Microsoft)" in summary.text
    assert [(button.id, button.title) for button in summary.buttons] == [
        (button_id(invoice_id, "edit"), "Editar"),
        (button_id(invoice_id, "cancel"), "Cancelar"),
    ]


async def test_second_photo_of_the_same_invoice_sends_br_e04(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.6, BR-E04: a second photo whose natural key the first holds is cancelled naming it."""
    fake = FakeChannel()
    profile_id = _profile()
    cfg = _config(profile_id)
    sender = _sender(db_pool, fake)
    reading = _reading(await _today(db_pool), next(_NEXT_NUMERO))

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
    first = await _photo_invoice(db_pool, profile_id, cfg, reading)
    second = await _photo_invoice(db_pool, profile_id, cfg, reading)
    assert await sender.send_pending() == 4

    async with db_pool.connection() as conn:
        assert await _state_rule(conn, second) == ("CANCELLED", "BR-E04")
    assert fake.rule_ids()[-1] == "BR-E04"
    assert f"(#{first})" in fake.texts()[-1]
    assert f"/estado {first}" in fake.texts()[-1]


async def test_total_edit_reroutes_and_shows_the_new_summary(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.5: total=12.30 on a routed invoice re-routes it and the summary shows the new total."""
    fake = FakeChannel()
    profile_id = _profile()
    cfg = _config(profile_id)
    sender = _sender(db_pool, fake)

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
    invoice_id = await _photo_invoice(
        db_pool, profile_id, cfg, _reading(await _today(db_pool), next(_NEXT_NUMERO))
    )
    assert await sender.send_pending() == 2

    edit = _message(profile_id, text="total=12.30")
    async with db_pool.connection() as conn:
        result = await handle_inbound(conn, edit, max_queued_per_sender=MAX_QUEUED, timezone=TIMEZONE)
        assert await _state_rule(conn, invoice_id) == ("ROUTING", None)
    assert result.invoice_id == invoice_id

    await _run_route(db_pool, cfg, invoice_id)
    assert await sender.send_pending() == 1

    async with db_pool.connection() as conn:
        assert await _state_rule(conn, invoice_id) == ("FETCHING_DOCS", "BR-R02")
    assert "Total: S/ 12.30" in fake.texts()[-1]


async def test_parked_question_is_listed_and_resumed_from_pendientes(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-3.7, BR-S08: an unanswered question parks; /pendientes re-sends it and pressing it resumes."""
    fake = FakeChannel()
    profile_id = _profile()
    cfg = _config(profile_id)
    sender = _sender(db_pool, fake)
    msg = _message(profile_id, attachments=[_photo()])

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        intake = await handle_inbound(conn, msg, max_queued_per_sender=MAX_QUEUED, timezone=TIMEZONE)
    assert intake.invoice_id is not None
    invoice_id = intake.invoice_id
    reading = _reading(await _today(db_pool), next(_NEXT_NUMERO), buyer=DNI_BUYER, buyer_type="1")
    await _start_routing(db_pool, invoice_id, reading)
    await _run_route(db_pool, cfg, invoice_id)
    assert await sender.send_pending() == 2

    async with db_pool.connection() as conn:
        assert await _state_rule(conn, invoice_id) == ("CONFIRMING", "BR-R03")
        await conn.execute(
            "UPDATE invoices SET updated_at = now() - interval '25 hours' WHERE id = %s",
            (invoice_id,),
        )

    assert await park_stale_questions(db_pool, 24) >= 1
    await sender.send_pending()
    async with db_pool.connection() as conn:
        assert await _state_rule(conn, invoice_id) == ("PENDING_USER", "BR-S08")
        assert await _waiting(conn, invoice_id) == ("CONFIRMING", "BR-R03")
    assert any("en pendientes" in text for text in fake.texts())

    pendientes = _message(profile_id, text="/pendientes")
    async with db_pool.connection() as conn:
        await handle_inbound(conn, pendientes, max_queued_per_sender=MAX_QUEUED, timezone=TIMEZONE)
    await sender.send_pending()
    assert "Esto espera algo de ti:" in fake.texts()[-2]
    assert f"#{invoice_id}" in fake.texts()[-2]
    assert fake.rule_ids()[-1] == "BR-R03"
    assert "¿En qué flujo lo registro?" in fake.texts()[-1]

    pressed = _message(profile_id, button_id=fake.sent[-1].buttons[1].id)
    async with db_pool.connection() as conn:
        await handle_inbound(conn, pressed, max_queued_per_sender=MAX_QUEUED, timezone=TIMEZONE)
        assert await _state_rule(conn, invoice_id) == ("ROUTING", "BR-R03")
        assert await _waiting(conn, invoice_id) == (None, None)
    await _run_route(db_pool, cfg, invoice_id)
    assert await sender.send_pending() == 1

    async with db_pool.connection() as conn:
        assert await _state_rule(conn, invoice_id) == ("FETCHING_DOCS", "BR-R03")
    assert "Total: S/ 49.90" in fake.texts()[-1]


async def test_estado_on_another_profiles_invoice_sends_br_s07(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-10.1, BR-S07: /estado naming another profile's invoice gets BR-S07 and changes nothing."""
    fake = FakeChannel()
    owner_profile = _profile()
    other_profile = _profile()
    cfg = _config(owner_profile)
    sender = _sender(db_pool, fake)

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
    invoice_id = await _photo_invoice(
        db_pool, owner_profile, cfg, _reading(await _today(db_pool), next(_NEXT_NUMERO))
    )

    async with db_pool.connection() as conn:
        await _clear_outbox(conn)
        command = _message(other_profile, text=f"/estado {invoice_id}")
        await handle_inbound(conn, command, max_queued_per_sender=MAX_QUEUED, timezone=TIMEZONE)
    assert await sender.send_pending() == 1

    async with db_pool.connection() as conn:
        assert await _state_rule(conn, invoice_id) == ("FETCHING_DOCS", "BR-R02")
    assert fake.rule_ids() == ["BR-S07"]
    assert fake.texts() == [f"No encontré el comprobante #{invoice_id}."]
