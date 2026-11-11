"""Integration tests for the WhatsApp outbox transport (R-1.10, PT-4, BR-S04)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from invoicebot.channels.base import Button, OutboundMessage
from invoicebot.channels.whatsapp.client import WhatsAppClient
from invoicebot.channels.whatsapp.transport import TemplateMissing, make_transport
from invoicebot.config.models import ChannelTemplates
from invoicebot.queue.outbox import OutboxRow, OutboxSender, Transport, add
from invoicebot.store.db import Pool

pytestmark = pytest.mark.asyncio(loop_scope="session")

PROFILE = "yo"
NUMBER = "51999999999"
PHONE_NUMBER_ID = "123456789"
GRAPH_VERSION = "v23.0"
BEARER = "synthetic-bearer-value"

TEMPLATES = ChannelTemplates(
    completion="invoice_done",
    manual_required="manual_required",
    resume_notice="resume_notice",
    pending_user_reminder="pending_user_reminder",
    batch_preview="batch_preview",
    admin_alert="admin_alert",
)

Handler = Callable[[httpx.Request], httpx.Response]
Payload = dict[str, Any]


def _accept(request: httpx.Request) -> httpx.Response:
    """Answer a send with a successful Meta message id."""
    return httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})


def _message(**overrides: Any) -> OutboundMessage:
    """Build a canonical outbound message with the test profile's defaults."""
    fields: dict[str, Any] = {
        "channel": "whatsapp",
        "recipient": PROFILE,
        "profile_id": PROFILE,
        "text": "hola",
    }
    fields.update(overrides)
    return OutboundMessage(**fields)


def _row(msg: OutboundMessage) -> OutboxRow:
    """Wrap an outbound message in the due outbox row the transport receives."""
    return OutboxRow(
        id=1,
        channel="whatsapp",
        to=PROFILE,
        payload=msg.model_dump(mode="json"),
        template=None,
        attempts=0,
    )


async def _prepare(pool: Pool, *, window_hours_ago: int | None) -> None:
    """Clear the rows a test depends on and set the profile's last inbound time."""
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM outbox")
        await conn.execute("DELETE FROM channel_windows WHERE channel = 'whatsapp'")
        if window_hours_ago is None:
            return
        await conn.execute(
            """
            INSERT INTO channel_windows (channel, recipient, last_inbound_at)
            VALUES ('whatsapp', %s, now() - make_interval(hours => %s))
            """,
            (PROFILE, window_hours_ago),
        )


@asynccontextmanager
async def _transport(
    pool: Pool,
    handler: Handler = _accept,
    *,
    templates: ChannelTemplates = TEMPLATES,
) -> AsyncIterator[tuple[Transport, list[Payload]]]:
    """Yield a transport plus the payloads it sends, answered through ``handler``."""
    sent: list[Payload] = []

    def recording(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(recording)) as http:
        client = WhatsAppClient(
            http,
            phone_number_id=PHONE_NUMBER_ID,
            graph_api_version=GRAPH_VERSION,
            token=BEARER,
        )
        yield make_transport(client, pool, numbers={PROFILE: NUMBER}, templates=templates), sent


async def test_open_window_sends_free_form_text(db_pool: Pool) -> None:
    """R-9.3, R-1.10: an open window sends one plain text message and no template."""
    await _prepare(db_pool, window_hours_ago=1)
    async with _transport(db_pool) as (transport, sent):
        await transport(_row(_message(text="hola")))

    assert len(sent) == 1
    assert sent[0]["type"] == "text"
    assert sent[0]["to"] == NUMBER
    assert sent[0]["text"] == {"body": "hola"}


async def test_open_window_with_buttons_sends_interactive(db_pool: Pool) -> None:
    """R-3.2: an open window with reply buttons sends an interactive message."""
    await _prepare(db_pool, window_hours_ago=1)
    buttons = [Button(id="1:confirm", title="Confirmar"), Button(id="1:reject", title="Rechazar")]
    async with _transport(db_pool) as (transport, sent):
        await transport(_row(_message(text="¿Confirmas?", buttons=buttons)))

    assert len(sent) == 1
    assert sent[0]["type"] == "interactive"
    assert len(sent[0]["interactive"]["action"]["buttons"]) == 2


async def test_closed_window_sends_the_rule_template(db_pool: Pool) -> None:
    """R-1.10, BR-S04: a closed window sends the rule's template with the text as its parameter."""
    await _prepare(db_pool, window_hours_ago=None)
    async with _transport(db_pool) as (transport, sent):
        await transport(_row(_message(rule_id="BR-F07", text="Revisa\nel comprobante")))

    assert len(sent) == 1
    assert sent[0]["type"] == "template"
    assert sent[0]["template"]["name"] == "manual_required"
    assert sent[0]["template"]["language"] == {"code": "es_PE"}
    assert sent[0]["template"]["components"] == [
        {"type": "body", "parameters": [{"type": "text", "text": "Revisa el comprobante"}]}
    ]


async def test_template_parameter_collapses_tabs_and_space_runs(db_pool: Pool) -> None:
    """PT-4: a tab and a run of six spaces become single spaces in the template parameter."""
    await _prepare(db_pool, window_hours_ago=None)
    async with _transport(db_pool) as (transport, sent):
        await transport(_row(_message(rule_id="BR-F07", text="Revisa\tel      comprobante")))

    assert sent[0]["template"]["components"] == [
        {"type": "body", "parameters": [{"type": "text", "text": "Revisa el comprobante"}]}
    ]


async def test_window_expired_on_send_falls_back_to_template(db_pool: Pool) -> None:
    """PT-4: a free-form send answered with Meta code 131047 is retried as a template."""
    await _prepare(db_pool, window_hours_ago=1)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(400, json={"error": {"code": 131047, "message": "Re-engagement"}})
        return _accept(request)

    async with _transport(db_pool, handler) as (transport, sent):
        await transport(_row(_message(rule_id="BR-F07", text="hola")))

    assert len(sent) == 2
    assert sent[0]["type"] == "text"
    assert sent[1]["type"] == "template"
    assert sent[1]["template"]["name"] == "manual_required"


async def test_closed_window_without_a_slot_raises_template_missing(db_pool: Pool) -> None:
    """R-1.10: a closed window for a rule with no template fails without any request."""
    await _prepare(db_pool, window_hours_ago=None)
    async with _transport(db_pool) as (transport, sent):
        with pytest.raises(TemplateMissing):
            await transport(_row(_message(rule_id="BR-S01")))

    assert sent == []


async def test_empty_template_name_raises_template_missing(db_pool: Pool) -> None:
    """R-1.10: a configured slot whose name is empty counts as missing."""
    await _prepare(db_pool, window_hours_ago=None)
    templates = TEMPLATES.model_copy(update={"manual_required": ""})
    async with _transport(db_pool, templates=templates) as (transport, sent):
        with pytest.raises(TemplateMissing, match="manual_required"):
            await transport(_row(_message(rule_id="BR-F07")))

    assert sent == []


async def test_message_template_wins_over_the_rule_table(db_pool: Pool) -> None:
    """R-1.10: the message's own template slot overrides its rule's slot."""
    await _prepare(db_pool, window_hours_ago=None)
    async with _transport(db_pool) as (transport, sent):
        await transport(_row(_message(rule_id="BR-F07", template="admin_alert")))

    assert len(sent) == 1
    assert sent[0]["template"]["name"] == "admin_alert"


async def test_outbox_sender_keeps_the_row_after_template_missing(db_pool: Pool) -> None:
    """R-11.5: TemplateMissing propagates, leaving the row pending with attempts incremented."""
    await _prepare(db_pool, window_hours_ago=None)
    async with db_pool.connection() as conn:
        outbox_id = await add(
            conn,
            "whatsapp",
            PROFILE,
            _message(rule_id="BR-S01").model_dump(mode="json"),
        )

    async with _transport(db_pool) as (transport, sent):
        sender = OutboxSender(db_pool, {"whatsapp": transport})
        assert await sender.send_pending() == 0

    assert sent == []
    async with db_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT state, attempts, last_error FROM outbox WHERE id = %s",
            (outbox_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "pending"
    assert row[1] == 1
    assert row[2].startswith("TemplateMissing")
