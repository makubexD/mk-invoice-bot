"""Conversation-level integration tests delivering through the fake channel.

Covers R-1.1, R-1.4, R-1.6, R-1.8, R-2.1, R-3.2, R-3.4, R-3.5, R-3.6, R-3.7, R-10.1, BR-S01,
BR-S02, BR-S07, BR-S08, BR-E01, BR-E04 and BR-R03 against real Postgres, with the outbox
sender handing each message to :class:`FakeChannel`.
"""

import itertools
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import Attachment, InboundMessage
from invoicebot.channels.buttons import button_id
from invoicebot.channels.fake import FakeChannel
from invoicebot.channels.whatsapp.client import WhatsAppClient
from invoicebot.channels.whatsapp.media import WhatsAppMediaSource
from invoicebot.config.loader import load_config
from invoicebot.config.models import Brand, Config, MediaMaxBytes, RoutingRule, Store, StoreDirectory
from invoicebot.extract.fields import Field, Reading
from invoicebot.fetchers.base import FetchInvoice, FetchReason, FetchResult
from invoicebot.fetchers.portal import PortalFetcher
from invoicebot.pipeline.extract import ROUTE_STEP
from invoicebot.pipeline.fetch import FETCH_STEP, make_fetch_handler
from invoicebot.pipeline.handlers import MEGABYTE
from invoicebot.pipeline.intake import handle_inbound
from invoicebot.pipeline.park import park_stale_questions
from invoicebot.pipeline.replies import resolve_reply
from invoicebot.pipeline.route import make_route_handler
from invoicebot.pipeline.steps import ATTACH_STEP
from invoicebot.pipeline.uploads import make_attach_handler
from invoicebot.pipeline.validate import VALIDATE_STEP, make_validate_handler
from invoicebot.queue.jobs import Job
from invoicebot.queue.outbox import OutboxSender
from invoicebot.queue.worker import Handler
from invoicebot.routing.router import compile_rules
from invoicebot.rules.messages_es import FETCH_NOT_FOUND_LINE, MESSAGES, fetcher_desc
from invoicebot.store.invoices import create_invoice, save_reading, set_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

MAX_QUEUED = 10
MEDIA_MAX_BYTES = MediaMaxBytes(image=10 * MEGABYTE, pdf=10 * MEGABYTE, xml=MEGABYTE, zip=10 * MEGABYTE)
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
#: The store's one comercio, named in BR-F11's bullet (BR-F07).
COMERCIO = "Tienda Sintética"
PDF_BYTES = b"%PDF-1.4 synthetic invoice\n"
#: The fake Meta host and the media ids the upload handler downloads (R-1.5).
GRAPH_HOST = "graph.facebook.com"
GRAPH_VERSION = "v23.0"
PHONE_NUMBER_ID = "123456789"
BEARER = "synthetic-bearer-value"
MEDIA_URL = "https://mmg.whatsapp.net/v/t62/{}"
#: Enough runs for the portal's final failure plus the inbox's four retries and the exhaustion check.
MAX_FETCH_RUNS = 10
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
    """Run the route and validate (SUNAT off) handlers on invoice_id, as the queue would (D95, D124)."""
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
    async with httpx.AsyncClient() as http:
        validate = make_validate_handler(pool, http, None, cfg)
        await validate(
            Job(
                id=job_id,
                invoice_id=invoice_id,
                step=VALIDATE_STEP,
                attempts=0,
                crashes=0,
                lease_until=datetime.now(UTC),
            )
        )


async def _photo_invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    cfg: Config,
    reading: Reading,
) -> int:
    """Intake a photo, store its reading and route it, returning the invoice id."""
    msg = _message(profile_id, attachments=[_photo()])
    async with pool.connection() as conn:
        intake = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
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
        result = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )

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
            await handle_inbound(
                conn, msg, max_queued_per_sender=2, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
            )
            for msg in messages
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
        first = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
    assert await sender.send_pending() == 1

    async with db_pool.connection() as conn:
        second = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )

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
        intake = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
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
        await handle_inbound(
            conn,
            pressed,
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
        )
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
        result = await handle_inbound(
            conn, edit, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
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
        intake = await handle_inbound(
            conn, msg, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
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
        # The question went out as long ago, so /pendientes may re-send it (S4.8, D94).
        await conn.execute(
            "UPDATE outbox SET created_at = now() - interval '25 hours' WHERE invoice_id = %s",
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
        await handle_inbound(
            conn,
            pendientes,
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
        )
    await sender.send_pending()
    assert "Esto espera algo de ti:" in fake.texts()[-2]
    assert f"#{invoice_id}" in fake.texts()[-2]
    assert fake.rule_ids()[-1] == "BR-R03"
    assert "¿En qué flujo lo registro?" in fake.texts()[-1]

    pressed = _message(profile_id, button_id=fake.sent[-1].buttons[1].id)
    async with db_pool.connection() as conn:
        await handle_inbound(
            conn,
            pressed,
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
        )
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
        await handle_inbound(
            conn,
            command,
            max_queued_per_sender=MAX_QUEUED,
            media_max_bytes=MEDIA_MAX_BYTES,
            timezone=TIMEZONE,
        )
    assert await sender.send_pending() == 1

    async with db_pool.connection() as conn:
        assert await _state_rule(conn, invoice_id) == ("FETCHING_DOCS", "BR-R02")
    assert fake.rule_ids() == ["BR-S07"]
    assert fake.texts() == [f"No encontré el comprobante #{invoice_id}."]


class _NotFoundInbox:
    """An ``email_inbox`` fetcher whose mailbox never has the invoice (R-5.5)."""

    async def fetch(self, invoice: FetchInvoice, out_dir: Path) -> FetchResult:
        """Report the invoice as not found yet, whatever the mailbox holds."""
        del invoice, out_dir
        return FetchResult(FetchReason.NOT_FOUND_YET, (), None)


def _matching_xml(serie: str, numero: int, total: str, fecha: date) -> bytes:
    """Return a synthetic UBL invoice that matches the reading (R-5.6)."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2">
  <ID>{serie}-{numero}</ID>
  <IssueDate>{fecha.isoformat()}</IssueDate>
  <InvoiceTypeCode>01</InvoiceTypeCode>
  <AccountingSupplierParty>
    <Party>
      <PartyIdentification><ID>{OWNER_RUC}</ID></PartyIdentification>
      <PartyLegalEntity><RegistrationName>Restaurante La Lena S.A.C.</RegistrationName></PartyLegalEntity>
    </Party>
  </AccountingSupplierParty>
  <LegalMonetaryTotal>
    <PayableAmount>{total}</PayableAmount>
  </LegalMonetaryTotal>
</Invoice>
""".encode()


def _media_server(media: dict[str, tuple[str, bytes]]) -> Callable[[httpx.Request], httpx.Response]:
    """Serve each media id's info and bytes from a fake Meta (R-1.5)."""

    def handle(request: httpx.Request) -> httpx.Response:
        media_id = request.url.path.rsplit("/", 1)[-1]
        mime, body = media[media_id]
        if request.url.host == GRAPH_HOST:
            return httpx.Response(
                200,
                json={"url": MEDIA_URL.format(media_id), "mime_type": mime, "file_size": len(body)},
            )
        return httpx.Response(200, content=body)

    return handle


def _job(job_id: int, invoice_id: int, step: str) -> Job:
    """Build the claimed job a handler receives."""
    return Job(
        id=job_id,
        invoice_id=invoice_id,
        step=step,
        attempts=0,
        crashes=0,
        lease_until=datetime.now(UTC),
    )


async def _newest_job_id(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int, step: str) -> int:
    """Return the id of invoice_id's newest job of ``step``."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT max(id) FROM jobs WHERE invoice_id = %s AND step = %s", (invoice_id, step)
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] is not None
    return int(row[0])


async def _fetch_to_manual(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    handler: Handler,
    invoice_id: int,
) -> None:
    """Run the fetch chain's queued jobs until the invoice leaves FETCHING_DOCS (BR-F07)."""
    for _ in range(MAX_FETCH_RUNS):
        async with pool.connection() as conn:
            state, _rule = await _state_rule(conn, invoice_id)
        if state != "FETCHING_DOCS":
            return
        await handler(_job(await _newest_job_id(pool, invoice_id, FETCH_STEP), invoice_id, FETCH_STEP))
    raise AssertionError("the fetch chain never reached MANUAL_REQUIRED")


async def test_manual_portal_exhausts_the_chain_then_uploads_deliver(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """R-5.5, R-1.5, S7.4: a manual portal ends in BR-F07, then a PDF and its XML deliver."""
    profile_id = _profile()
    cfg = _config(profile_id)
    flow = cfg.flows[WORK_FLOW].model_copy(update={"manual_uploads": ["pdf", "xml"]})
    cfg = cfg.model_copy(update={"flows": {**cfg.flows, WORK_FLOW: flow}})
    today = await _today(db_pool)
    numero = next(_NEXT_NUMERO)
    reading = _reading(today, numero)
    directory = StoreDirectory(
        stores={
            OWNER_RUC: Store(
                ruc=OWNER_RUC,
                razon_social="Comercio Sintético",
                tipo="01",
                brands=[Brand(comercio=COMERCIO)],
                portal="manual",
            )
        },
        portals={},
    )
    invoice_id = await _photo_invoice(db_pool, profile_id, cfg, reading)
    fetch = make_fetch_handler(
        db_pool,
        cfg,
        directory,
        {
            WORK_FLOW: {
                "portal_registry": PortalFetcher(
                    directory, cfg.limits.media.max_bytes, cfg.retrieval.portal_timeout_s
                ),
                "email_inbox": _NotFoundInbox(),
            }
        },
        tmp_path,
    )
    await _fetch_to_manual(db_pool, fetch, invoice_id)

    async with db_pool.connection() as conn:
        assert await _state_rule(conn, invoice_id) == ("MANUAL_REQUIRED", "BR-F07")
    fake = FakeChannel()
    sender = _sender(db_pool, fake)
    await sender.send_pending()
    assert "BR-F07" in fake.rule_ids()
    text = fake.texts()[fake.rule_ids().index("BR-F07")]
    assert MESSAGES["BR-F11"].format(comercio=COMERCIO) in text
    assert FETCH_NOT_FOUND_LINE.format(fetcher=fetcher_desc("email_inbox")) in text
    assert MESSAGES["BR-F01"] in text

    pdf_ref = f"pdf-{uuid4().hex}"
    xml_ref = f"xml-{uuid4().hex}"
    xml_bytes = _matching_xml(reading.value("serie") or "", numero, TOTAL, today)
    upload = _message(
        profile_id,
        attachments=[
            Attachment(kind="pdf", media_ref=pdf_ref, mime="application/pdf", size=len(PDF_BYTES)),
            Attachment(kind="xml", media_ref=xml_ref, mime="application/xml", size=len(xml_bytes)),
        ],
    )
    async with db_pool.connection() as conn:
        await handle_inbound(
            conn, upload, max_queued_per_sender=MAX_QUEUED, media_max_bytes=MEDIA_MAX_BYTES, timezone=TIMEZONE
        )
    attach_job_id = await _newest_job_id(db_pool, invoice_id, ATTACH_STEP)

    media = {pdf_ref: ("application/pdf", PDF_BYTES), xml_ref: ("application/xml", xml_bytes)}
    async with httpx.AsyncClient(transport=httpx.MockTransport(_media_server(media))) as http:
        client = WhatsAppClient(
            http,
            phone_number_id=PHONE_NUMBER_ID,
            graph_api_version=GRAPH_VERSION,
            token=BEARER,
        )
        attach = make_attach_handler(
            db_pool,
            cfg,
            {"whatsapp": WhatsAppMediaSource(client)},
            tmp_path,
            cfg.limits.media,
        )
        await attach(_job(attach_job_id, invoice_id, ATTACH_STEP))

    async with db_pool.connection() as conn:
        assert await _state_rule(conn, invoice_id) == ("DELIVERING", None)
