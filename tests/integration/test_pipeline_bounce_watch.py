"""Integration tests for the bounce_check job (D207, BR-D26, BR-D27).

The readers are fakes at the ``BounceReader`` port; Postgres is real.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.auth.msal_cache import AuthRequired
from invoicebot.channels.base import LINK_URL_MAX
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, MailerSink
from invoicebot.extract.fields import Field, Reading
from invoicebot.pipeline.bounce_watch import BounceReader, make_bounce_check_handler
from invoicebot.pipeline.done import MAIL_INVOICE_KEY
from invoicebot.pipeline.steps import BOUNCE_CHECK_STEP
from invoicebot.queue.jobs import Job, db_now_plus, enqueue
from invoicebot.rules.messages_es import (
    BOUNCE_STATUS_UNKNOWN,
    BOUNCED_DRAFT,
    BOUNCED_NO_DRAFT,
    BUTTON_OPEN_DRAFT,
    BUTTON_SENT,
)
from invoicebot.sinks.mail_bounce import Bounce, DraftUnavailable
from invoicebot.sinks.onedrive import GraphError, GraphTransient
from invoicebot.store.flow_b import (
    load_flow_data,
    load_sent_batch,
    merge_flow_data,
    open_batch,
    set_batch_email_ref,
    set_batch_state,
)
from invoicebot.store.invoices import (
    create_invoice,
    load_invoice_head,
    save_reading,
    set_flow,
    set_state,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

type DbPool = AsyncConnectionPool[AsyncConnection[Any]]

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
FLOW = "work_microsoft"
ACCOUNT = "personal"
MESSAGE_ID = "<fixture-1@example.com>"
SENT_ID = "<outlook-1@example.com>"
WEB_LINK = "https://outlook.live.com/owa/?ItemID=draft-1"
SERIE = "F001"
NUMERO = "123"


class FakeReader:
    """A ``BounceReader`` that returns or raises what the test set, and records its calls."""

    def __init__(
        self,
        bounce: Bounce | Exception | None = None,
        draft: str | Exception = WEB_LINK,
        on_draft: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._bounce = bounce
        self._draft = draft
        self._on_draft = on_draft
        self.finds: list[tuple[str, datetime]] = []
        self.drafts: list[tuple[str, str]] = []

    async def find_bounce(self, *, message_id: str, since: datetime) -> Bounce | None:
        """Record the call and return or raise the set report."""
        self.finds.append((message_id, since))
        if isinstance(self._bounce, Exception):
            raise self._bounce
        return self._bounce

    async def draft_from_sent(self, *, sent_id: str, message_id: str) -> str:
        """Record the call, run the hook, and return or raise the set link."""
        self.drafts.append((sent_id, message_id))
        if self._on_draft is not None:
            await self._on_draft()
        if isinstance(self._draft, Exception):
            raise self._draft
        return self._draft


def _error_id(error: Exception) -> str:
    """Return the parametrize id of an error case: its class name."""
    return type(error).__name__


def _config() -> Config:
    """Return example.yaml with the work flow's mailer sending through ``smtp`` (D205)."""
    cfg = load_config(EXAMPLE)
    sinks = [
        sink.model_copy(update={"modes": ["smtp"]}) if isinstance(sink, MailerSink) else sink
        for sink in cfg.flows[FLOW].sinks
    ]
    flow = cfg.flows[FLOW].model_copy(update={"sinks": sinks})
    return cfg.model_copy(update={"flows": {**cfg.flows, FLOW: flow}})


def _to(cfg: Config) -> str:
    """Return the work flow mailer's first To address."""
    mailer = next(sink for sink in cfg.flows[FLOW].sinks if isinstance(sink, MailerSink))
    return mailer.to[0]


async def _sent_invoice(
    pool: DbPool,
    profile_id: str,
    *,
    sent_ago_s: int = 120,
    state: str = "sent",
    email_ref: str | None = MESSAGE_ID,
    mail_invoice: bool = True,
) -> int:
    """Create a DONE invoice whose batch went out ``sent_ago_s`` ago with ``email_ref``, in ``state``."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await set_flow(conn, invoice_id, FLOW)
        await set_state(conn, invoice_id, "DONE", rule_id="BR-D10")
        if mail_invoice:
            await merge_flow_data(conn, invoice_id, {MAIL_INVOICE_KEY: [SERIE, NUMERO]})
        batch_id = await open_batch(conn, invoice_id, FLOW, "sending")
        await set_batch_state(conn, batch_id, "sent")
        if email_ref is not None:
            await set_batch_email_ref(conn, batch_id, email_ref)
        await conn.execute(
            "UPDATE batches SET sent_at = now() - make_interval(secs => %s), state = %s WHERE id = %s",
            (sent_ago_s, state, batch_id),
        )
    return invoice_id


async def _run(
    pool: DbPool,
    invoice_id: int,
    reader: BounceReader,
    *,
    job_id: int = 0,
    cfg: Config | None = None,
    account: str = ACCOUNT,
) -> None:
    """Run the bounce_check handler once for ``invoice_id``, as job ``job_id``."""
    handler = make_bounce_check_handler(pool, cfg or _config(), {account: reader})
    await handler(
        Job(
            id=job_id,
            invoice_id=invoice_id,
            step=BOUNCE_CHECK_STEP,
            attempts=0,
            crashes=0,
            lease_until=datetime.now(UTC),
        )
    )


async def _jobs(pool: DbPool, invoice_id: int) -> list[tuple[str, float]]:
    """Return each job's step and its delay in seconds from now, oldest first."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT step, extract(epoch FROM run_after - now()) FROM jobs WHERE invoice_id = %s ORDER BY id",
            (invoice_id,),
        )
        return [(str(step), float(delay)) for step, delay in await cur.fetchall()]


async def _outbox(pool: DbPool, profile_id: str) -> list[dict[str, Any]]:
    """Return the outbox payloads for ``profile_id``, oldest first."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT payload_json FROM outbox WHERE "to" = %s ORDER BY id""", (profile_id,)
        )
        return [row[0] for row in await cur.fetchall()]


async def _batch_state(pool: DbPool, invoice_id: int) -> str | None:
    """Return the invoice's batch state."""
    async with pool.connection() as conn:
        batch = await load_sent_batch(conn, invoice_id)
    return None if batch is None else batch.state


async def _invoice_state(pool: DbPool, invoice_id: int) -> str | None:
    """Return the invoice's state."""
    async with pool.connection() as conn:
        head = await load_invoice_head(conn, invoice_id)
    return None if head is None else head.state


async def test_no_report_before_the_deadline_checks_again(db_pool: DbPool) -> None:
    """D207: with no report inside bounce_watch_s, one bounce_check is queued about 300 s ahead."""
    profile_id = f"bounce-{uuid4().hex}"
    invoice_id = await _sent_invoice(db_pool, profile_id)
    reader = FakeReader()

    await _run(db_pool, invoice_id, reader)

    jobs = await _jobs(db_pool, invoice_id)
    assert [step for step, _ in jobs] == [BOUNCE_CHECK_STEP]
    assert 280 <= jobs[0][1] <= 320
    assert [message_id for message_id, _ in reader.finds] == [MESSAGE_ID]
    async with db_pool.connection() as conn:
        batch = await load_sent_batch(conn, invoice_id)
    assert batch is not None
    assert batch.sent_at is not None
    assert reader.finds[0][1] == batch.sent_at - timedelta(seconds=60)
    assert await _batch_state(db_pool, invoice_id) == "sent"
    assert await _outbox(db_pool, profile_id) == []


async def test_no_report_after_the_deadline_ends_the_watch(
    db_pool: DbPool, caplog: pytest.LogCaptureFixture
) -> None:
    """D207: with no report once bounce_watch_s has passed, nothing is queued and the watch logs clear."""
    profile_id = f"bounce-{uuid4().hex}"
    invoice_id = await _sent_invoice(db_pool, profile_id, sent_ago_s=1801)
    caplog.set_level(logging.INFO, logger="invoicebot.pipeline.bounce_watch")

    await _run(db_pool, invoice_id, FakeReader())

    assert await _jobs(db_pool, invoice_id) == []
    assert "bounce watch clear" in caplog.messages
    assert await _batch_state(db_pool, invoice_id) == "sent"


async def test_a_report_with_a_draft_bounces_the_batch_and_queues_br_d26(db_pool: DbPool) -> None:
    """BR-D26: the batch goes bounced, the invoice stays DONE, one BR-D26 with the link and [Ya lo envié]."""
    profile_id = f"bounce-{uuid4().hex}"
    invoice_id = await _sent_invoice(db_pool, profile_id)
    reader = FakeReader(bounce=Bounce(status="5.7.501", sent_id=SENT_ID))

    await _run(db_pool, invoice_id, reader)

    assert reader.drafts == [(SENT_ID, MESSAGE_ID)]
    assert await _batch_state(db_pool, invoice_id) == "bounced"
    assert await _invoice_state(db_pool, invoice_id) == "DONE"
    assert [step for step, _ in await _jobs(db_pool, invoice_id)] == ["folder_status"]
    [payload] = await _outbox(db_pool, profile_id)
    assert payload["rule_id"] == "BR-D26"
    assert payload["text"] == BOUNCED_DRAFT.format(
        id=invoice_id, serie=SERIE, numero=NUMERO, to=_to(_config()), status="5.7.501"
    )
    assert payload["links"] == [{"title": BUTTON_OPEN_DRAFT, "url": WEB_LINK}]
    assert [button["title"] for button in payload["buttons"]] == [BUTTON_SENT]
    async with db_pool.connection() as conn:
        flow_data = await load_flow_data(conn, invoice_id)
    assert flow_data["bounce"] == {"status": "5.7.501", "draft": True}


@pytest.mark.parametrize(
    "error",
    [
        DraftUnavailable("no Sent copy"),
        GraphError(403),
        AuthRequired(ACCOUNT),
        GraphTransient(503),
        httpx.ConnectError("synthetic"),
    ],
    ids=_error_id,
)
async def test_a_report_whose_draft_fails_queues_br_d27(db_pool: DbPool, error: Exception) -> None:
    """BR-D27: a draft that cannot be made still bounces the batch, with [Ya lo envié] only."""
    profile_id = f"bounce-{uuid4().hex}"
    invoice_id = await _sent_invoice(db_pool, profile_id)

    await _run(db_pool, invoice_id, FakeReader(bounce=Bounce(status="5.7.501", sent_id=SENT_ID), draft=error))

    assert await _batch_state(db_pool, invoice_id) == "bounced"
    assert await _invoice_state(db_pool, invoice_id) == "DONE"
    assert [step for step, _ in await _jobs(db_pool, invoice_id)] == ["folder_status"]
    [payload] = await _outbox(db_pool, profile_id)
    assert payload["rule_id"] == "BR-D27"
    assert payload["text"] == BOUNCED_NO_DRAFT.format(
        id=invoice_id, serie=SERIE, numero=NUMERO, to=_to(_config()), status="5.7.501"
    )
    assert payload["links"] == []
    assert [button["title"] for button in payload["buttons"]] == [BUTTON_SENT]
    async with db_pool.connection() as conn:
        flow_data = await load_flow_data(conn, invoice_id)
    assert flow_data["bounce"] == {"status": "5.7.501", "draft": False}


async def test_a_report_without_a_status_code_says_sin_codigo(db_pool: DbPool) -> None:
    """BR-D26: a report with no enhanced status code shows ``sin código``."""
    profile_id = f"bounce-{uuid4().hex}"
    invoice_id = await _sent_invoice(db_pool, profile_id)

    await _run(db_pool, invoice_id, FakeReader(bounce=Bounce(status="?", sent_id=SENT_ID)))

    [payload] = await _outbox(db_pool, profile_id)
    assert f"({BOUNCE_STATUS_UNKNOWN})" in payload["text"]


async def test_a_batch_already_bounced_does_nothing(db_pool: DbPool) -> None:
    """D207: a batch already bounced is not read, changed or reported again."""
    profile_id = f"bounce-{uuid4().hex}"
    invoice_id = await _sent_invoice(db_pool, profile_id, state="bounced")
    reader = FakeReader(bounce=Bounce(status="5.7.501", sent_id=SENT_ID))

    await _run(db_pool, invoice_id, reader)

    assert reader.finds == []
    assert await _batch_state(db_pool, invoice_id) == "bounced"
    assert await _jobs(db_pool, invoice_id) == []
    assert await _outbox(db_pool, profile_id) == []


async def test_a_batch_sent_again_by_hand_after_its_bounce_does_nothing(db_pool: DbPool) -> None:
    """BR-D08: a batch set sent again after its bounce is not watched again."""
    profile_id = f"bounce-{uuid4().hex}"
    invoice_id = await _sent_invoice(db_pool, profile_id)
    async with db_pool.connection() as conn, conn.transaction():
        await merge_flow_data(conn, invoice_id, {"bounce": {"status": "5.7.501", "draft": True}})
    reader = FakeReader(bounce=Bounce(status="5.7.501", sent_id=SENT_ID))

    await _run(db_pool, invoice_id, reader)

    assert reader.finds == []
    assert await _batch_state(db_pool, invoice_id) == "sent"
    assert await _jobs(db_pool, invoice_id) == []
    assert await _outbox(db_pool, profile_id) == []


async def test_a_batch_bounced_meanwhile_is_reported_once(db_pool: DbPool) -> None:
    """D207: the last transaction reads the batch again: a batch bounced during the draft gets no message."""
    profile_id = f"bounce-{uuid4().hex}"
    invoice_id = await _sent_invoice(db_pool, profile_id)

    async def bounce_meanwhile() -> None:
        async with db_pool.connection() as conn, conn.transaction():
            batch = await load_sent_batch(conn, invoice_id)
            assert batch is not None
            await set_batch_state(conn, batch.id, "bounced")

    await _run(
        db_pool,
        invoice_id,
        FakeReader(bounce=Bounce(status="5.7.501", sent_id=SENT_ID), on_draft=bounce_meanwhile),
    )

    assert await _jobs(db_pool, invoice_id) == []
    assert await _outbox(db_pool, profile_id) == []


@pytest.mark.parametrize("error", [AuthRequired(ACCOUNT), GraphError(403)], ids=_error_id)
async def test_a_blocked_inbox_read_logs_and_changes_nothing(
    db_pool: DbPool, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    """D207: AuthRequired or a final GraphError logs ``bounce watch blocked`` and changes nothing."""
    profile_id = f"bounce-{uuid4().hex}"
    invoice_id = await _sent_invoice(db_pool, profile_id)
    caplog.set_level(logging.WARNING, logger="invoicebot.pipeline.bounce_watch")

    await _run(db_pool, invoice_id, FakeReader(bounce=error))

    [record] = [r for r in caplog.records if r.getMessage() == "bounce watch blocked"]
    assert record.__dict__["invoice_id"] == invoice_id
    assert await _batch_state(db_pool, invoice_id) == "sent"
    assert await _invoice_state(db_pool, invoice_id) == "DONE"
    assert await _jobs(db_pool, invoice_id) == []
    assert await _outbox(db_pool, profile_id) == []


async def test_a_transient_inbox_error_propagates_to_the_retry(db_pool: DbPool) -> None:
    """D207: a transient Graph error is raised for the worker's retry and changes nothing."""
    profile_id = f"bounce-{uuid4().hex}"
    invoice_id = await _sent_invoice(db_pool, profile_id)

    with pytest.raises(GraphTransient, match="503"):
        await _run(db_pool, invoice_id, FakeReader(bounce=GraphTransient(503)))

    assert await _batch_state(db_pool, invoice_id) == "sent"
    assert await _jobs(db_pool, invoice_id) == []


async def test_a_draft_link_the_outbound_check_drops_queues_br_d27(db_pool: DbPool) -> None:
    """BR-D27: a draft link safe_links refuses means no link, so BR-D27 and draft False."""
    profile_id = f"bounce-{uuid4().hex}"
    invoice_id = await _sent_invoice(db_pool, profile_id)
    too_long = "https://outlook.live.com/owa/?ItemID=" + "a" * LINK_URL_MAX

    await _run(
        db_pool, invoice_id, FakeReader(bounce=Bounce(status="5.7.501", sent_id=SENT_ID), draft=too_long)
    )

    assert await _batch_state(db_pool, invoice_id) == "bounced"
    [payload] = await _outbox(db_pool, profile_id)
    assert payload["rule_id"] == "BR-D27"
    assert payload["links"] == []
    async with db_pool.connection() as conn:
        flow_data = await load_flow_data(conn, invoice_id)
    assert flow_data["bounce"] == {"status": "5.7.501", "draft": False}


async def test_an_older_job_of_a_forked_chain_does_nothing(db_pool: DbPool) -> None:
    """R-5.1: with two pending bounce_check jobs, the older one reads nothing and changes nothing."""
    profile_id = f"bounce-{uuid4().hex}"
    invoice_id = await _sent_invoice(db_pool, profile_id)
    async with db_pool.connection() as conn, conn.transaction():
        after = await db_now_plus(conn, seconds=300)
        await enqueue(conn, invoice_id, BOUNCE_CHECK_STEP, after)
        await enqueue(conn, invoice_id, BOUNCE_CHECK_STEP, after)
        cur = await conn.execute("SELECT min(id) FROM jobs WHERE invoice_id = %s", (invoice_id,))
        row = await cur.fetchone()
    assert row is not None
    reader = FakeReader(bounce=Bounce(status="5.7.501", sent_id=SENT_ID))

    await _run(db_pool, invoice_id, reader, job_id=row[0])

    assert reader.finds == []
    assert await _batch_state(db_pool, invoice_id) == "sent"
    assert [step for step, _ in await _jobs(db_pool, invoice_id)] == [BOUNCE_CHECK_STEP, BOUNCE_CHECK_STEP]
    assert await _outbox(db_pool, profile_id) == []


async def test_no_reader_for_the_account_logs_blocked(
    db_pool: DbPool, caplog: pytest.LogCaptureFixture
) -> None:
    """D207: with no reader for the mailer's account, the watch logs ``no_reader`` and changes nothing."""
    profile_id = f"bounce-{uuid4().hex}"
    invoice_id = await _sent_invoice(db_pool, profile_id)
    caplog.set_level(logging.WARNING, logger="invoicebot.pipeline.bounce_watch")
    reader = FakeReader(bounce=Bounce(status="5.7.501", sent_id=SENT_ID))

    await _run(db_pool, invoice_id, reader, account="another")

    [record] = [r for r in caplog.records if r.getMessage() == "bounce watch blocked"]
    assert record.__dict__["status"] == "no_reader"
    assert reader.finds == []
    assert await _batch_state(db_pool, invoice_id) == "sent"
    assert await _jobs(db_pool, invoice_id) == []


async def test_a_graph_sent_batch_without_email_ref_does_nothing(db_pool: DbPool) -> None:
    """D207: a sent batch with no Message-ID (a graph send) is not watched."""
    profile_id = f"bounce-{uuid4().hex}"
    invoice_id = await _sent_invoice(db_pool, profile_id, email_ref=None)
    reader = FakeReader(bounce=Bounce(status="5.7.501", sent_id=SENT_ID))

    await _run(db_pool, invoice_id, reader)

    assert reader.finds == []
    assert await _batch_state(db_pool, invoice_id) == "sent"
    assert await _jobs(db_pool, invoice_id) == []
    assert await _outbox(db_pool, profile_id) == []


async def test_a_flow_without_an_smtp_mailer_logs_blocked(
    db_pool: DbPool, caplog: pytest.LogCaptureFixture
) -> None:
    """D207: a flow whose mailer does not list smtp logs ``no_reader`` and reads nothing."""
    profile_id = f"bounce-{uuid4().hex}"
    invoice_id = await _sent_invoice(db_pool, profile_id)
    caplog.set_level(logging.WARNING, logger="invoicebot.pipeline.bounce_watch")
    reader = FakeReader(bounce=Bounce(status="5.7.501", sent_id=SENT_ID))

    await _run(db_pool, invoice_id, reader, cfg=load_config(EXAMPLE))

    [record] = [r for r in caplog.records if r.getMessage() == "bounce watch blocked"]
    assert record.__dict__["status"] == "no_reader"
    assert reader.finds == []
    assert await _batch_state(db_pool, invoice_id) == "sent"


async def test_serie_and_numero_fall_back_to_the_reading(db_pool: DbPool) -> None:
    """R-7.4: without the saved mail invoice, the message takes serie and numero from the reading."""
    profile_id = f"bounce-{uuid4().hex}"
    invoice_id = await _sent_invoice(db_pool, profile_id, mail_invoice=False)
    reading = Reading(
        fields={
            "serie": Field(value="E001", source="qr", confidence=1.0),
            "numero": Field(value="77", source="qr", confidence=1.0),
        },
        document_url=None,
    )
    async with db_pool.connection() as conn, conn.transaction():
        await save_reading(conn, invoice_id, reading)

    await _run(db_pool, invoice_id, FakeReader(bounce=Bounce(status="5.7.501", sent_id=SENT_ID)))

    [payload] = await _outbox(db_pool, profile_id)
    assert payload["text"] == BOUNCED_DRAFT.format(
        id=invoice_id, serie="E001", numero="77", to=_to(_config()), status="5.7.501"
    )
