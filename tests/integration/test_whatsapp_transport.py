"""Integration tests for the WhatsApp outbox transport (R-1.10, PT-4, BR-S04)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from invoicebot.channels.base import Button, Document, Link, OutboundMessage
from invoicebot.channels.whatsapp.client import WhatsAppClient, WhatsAppError
from invoicebot.channels.whatsapp.transport import (
    DOCUMENT_MAX_BYTES,
    TEMPLATE_PARAM_MAX_CHARS,
    TEMPLATE_SLOT_BY_RULE,
    make_transport,
)
from invoicebot.config.models import ChannelTemplates
from invoicebot.queue.outbox import (
    OutboxRow,
    OutboxSender,
    PermanentSendError,
    SendLater,
    Transport,
    add,
)
from invoicebot.rules.messages_es import LIST_BUTTON, MESSAGES
from invoicebot.store.db import Pool

pytestmark = pytest.mark.asyncio(loop_scope="session")

PROFILE = "yo"
NUMBER = "51999999999"
PHONE_NUMBER_ID = "123456789"
GRAPH_VERSION = "v23.0"
BEARER = "synthetic-bearer-value"
DOCUMENT_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

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


def _row(msg: OutboundMessage, *, to: str = PROFILE) -> OutboxRow:
    """Wrap an outbound message in the due outbox row the transport receives."""
    return OutboxRow(
        id=1,
        channel="whatsapp",
        to=to,
        payload=msg.model_dump(mode="json"),
        template=None,
        attempts=0,
        state="pending",
    )


def _document(tmp_path: Path, *, name: str = "factura.xlsx", data: bytes = b"xlsx") -> Document:
    """Write a synthetic document and return the canonical reference the message carries."""
    path = tmp_path / name
    path.write_bytes(data)
    return Document(path=str(path), filename=name, mime=DOCUMENT_MIME, size=len(data))


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


async def _noop_token_rejected() -> None:
    """Record nothing: the tests that do not inspect a rejected token pass this callback."""


@asynccontextmanager
async def _transport(
    pool: Pool,
    handler: Handler = _accept,
    *,
    templates: ChannelTemplates = TEMPLATES,
    language: str = "es_PE",
    on_token_rejected: Callable[[], Awaitable[None]] = _noop_token_rejected,
    requests: list[httpx.Request] | None = None,
) -> AsyncIterator[tuple[Transport, list[Payload]]]:
    """Yield a transport plus the payloads it sends, answered through ``handler``."""
    sent: list[Payload] = []

    def recording(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        if request.headers.get("content-type", "").startswith("application/json"):
            sent.append(json.loads(request.content))
        return handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(recording)) as http:
        client = WhatsAppClient(
            http,
            phone_number_id=PHONE_NUMBER_ID,
            graph_api_version=GRAPH_VERSION,
            token=BEARER,
        )
        transport = make_transport(
            client,
            pool,
            numbers={PROFILE: NUMBER},
            templates=templates,
            language=language,
            on_token_rejected=on_token_rejected,
        )
        yield transport, sent


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


async def test_three_buttons_send_a_button_message(db_pool: Pool) -> None:
    """BR-R03: three choices still fit Meta's button cap, so they send as reply buttons."""
    await _prepare(db_pool, window_hours_ago=1)
    buttons = [
        Button(id="1:pick:0", title="Facturas"),
        Button(id="1:pick:1", title="Boletas"),
        Button(id="1:pick:2", title="Notas"),
    ]
    async with _transport(db_pool) as (transport, sent):
        await transport(_row(_message(text="¿En qué flujo lo registro?", buttons=buttons)))

    assert len(sent) == 1
    assert sent[0]["type"] == "interactive"
    assert sent[0]["interactive"]["type"] == "button"
    assert len(sent[0]["interactive"]["action"]["buttons"]) == 3


async def test_four_buttons_send_a_list_message(db_pool: Pool) -> None:
    """BR-R03: four choices exceed the button cap, so they send as an interactive list."""
    await _prepare(db_pool, window_hours_ago=1)
    buttons = [
        Button(id="1:pick:0", title="Facturas"),
        Button(id="1:pick:1", title="Boletas"),
        Button(id="1:pick:2", title="Notas"),
        Button(id="1:pick:3", title="Otros"),
    ]
    async with _transport(db_pool) as (transport, sent):
        await transport(_row(_message(text="¿En qué flujo lo registro?", buttons=buttons)))

    assert len(sent) == 1
    assert sent[0]["type"] == "interactive"
    assert sent[0]["interactive"]["type"] == "list"
    assert sent[0]["interactive"]["action"]["button"] == LIST_BUTTON
    assert len(sent[0]["interactive"]["action"]["sections"][0]["rows"]) == 4


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


async def test_open_window_appends_the_link_lines(db_pool: Pool) -> None:
    """D194: in the window, a message with a link sends its line after the text."""
    await _prepare(db_pool, window_hours_ago=1)
    link = Link(title="📂 Abrir carpeta", url="https://x.example/f")
    async with _transport(db_pool) as (transport, sent):
        await transport(_row(_message(text="Listo", links=[link])))

    assert sent[0]["type"] == "text"
    assert sent[0]["text"]["body"].endswith("\n📂 Abrir carpeta: https://x.example/f")


async def test_closed_window_template_keeps_the_link_lines(db_pool: Pool) -> None:
    """D194: outside the window, the template parameter ends with the link line."""
    await _prepare(db_pool, window_hours_ago=None)
    link = Link(title="📂 Abrir carpeta", url="https://x.example/f")
    async with _transport(db_pool) as (transport, sent):
        await transport(_row(_message(rule_id="BR-F07", text="Listo", links=[link])))

    parameter = sent[0]["template"]["components"][0]["parameters"][0]["text"]
    assert parameter.endswith("📂 Abrir carpeta: https://x.example/f")


async def test_template_keeps_the_link_lines_when_the_text_is_capped(db_pool: Pool) -> None:
    """D194: a text longer than the parameter cap is truncated, but its link line survives whole."""
    await _prepare(db_pool, window_hours_ago=None)
    link = Link(title="📂 Abrir carpeta", url="https://x.example/f")
    text = "x" * (TEMPLATE_PARAM_MAX_CHARS + 100)
    async with _transport(db_pool) as (transport, sent):
        await transport(_row(_message(rule_id="BR-F07", text=text, links=[link])))

    parameter = sent[0]["template"]["components"][0]["parameters"][0]["text"]
    assert parameter.endswith("📂 Abrir carpeta: https://x.example/f")
    assert len(parameter) <= TEMPLATE_PARAM_MAX_CHARS


@pytest.mark.parametrize("window_hours_ago", [1, None])
async def test_a_message_without_links_keeps_its_text(db_pool: Pool, window_hours_ago: int | None) -> None:
    """D194: a message with no links sends exactly its text, in and out of the window."""
    await _prepare(db_pool, window_hours_ago=window_hours_ago)
    async with _transport(db_pool) as (transport, sent):
        await transport(_row(_message(rule_id="BR-F07", text="hola")))

    if window_hours_ago is None:
        assert sent[0]["template"]["components"][0]["parameters"][0]["text"] == "hola"
    else:
        assert sent[0]["text"]["body"] == "hola"


async def test_a_document_is_uploaded_then_sent_with_its_media_id(db_pool: Pool, tmp_path: Path) -> None:
    """D194: in the window a document row uploads once, then sends that media id as a document."""
    await _prepare(db_pool, window_hours_ago=1)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "m1"})
        return httpx.Response(200, json={"messages": [{"id": "wamid.D"}]})

    link = Link(title="📂 Abrir carpeta", url="https://x.example/f")
    document = _document(tmp_path)
    async with _transport(db_pool, handler, requests=requests) as (transport, sent):
        result = await transport(_row(_message(text="Listo", links=[link], document=document)))

    assert result == "wamid.D"
    assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == ["media", "messages"]
    assert len(sent) == 1
    assert sent[0]["type"] == "document"
    assert sent[0]["document"]["id"] == "m1"
    assert sent[0]["document"]["filename"] == "factura.xlsx"
    assert sent[0]["document"]["caption"] == "Listo\n📂 Abrir carpeta: https://x.example/f"


async def test_a_document_outside_the_window_is_permanent(db_pool: Pool, tmp_path: Path) -> None:
    """D194: outside the window a document posts nothing and fails permanently."""
    await _prepare(db_pool, window_hours_ago=None)
    requests: list[httpx.Request] = []
    async with _transport(db_pool, requests=requests) as (transport, sent):
        with pytest.raises(PermanentSendError, match="outside the 24-hour window"):
            await transport(_row(_message(document=_document(tmp_path))))

    assert requests == []
    assert sent == []


async def test_a_missing_document_is_permanent(db_pool: Pool, tmp_path: Path) -> None:
    """D194: a document whose file is gone posts nothing and fails permanently."""
    await _prepare(db_pool, window_hours_ago=1)
    missing = Document(path=str(tmp_path / "gone.xlsx"), filename="gone.xlsx", mime=DOCUMENT_MIME, size=1)
    requests: list[httpx.Request] = []
    async with _transport(db_pool, requests=requests) as (transport, sent):
        with pytest.raises(PermanentSendError, match="missing"):
            await transport(_row(_message(document=missing)))

    assert requests == []
    assert sent == []


async def test_a_document_over_the_cap_is_permanent(db_pool: Pool, tmp_path: Path) -> None:
    """D194: a document over Meta's 100 MB cap posts nothing and fails permanently."""
    await _prepare(db_pool, window_hours_ago=1)
    path = tmp_path / "big.xlsx"
    with path.open("wb") as file:
        file.truncate(DOCUMENT_MAX_BYTES + 1)
    document = Document(path=str(path), filename="big.xlsx", mime=DOCUMENT_MIME, size=DOCUMENT_MAX_BYTES + 1)
    requests: list[httpx.Request] = []
    async with _transport(db_pool, requests=requests) as (transport, sent):
        with pytest.raises(PermanentSendError, match="over"):
            await transport(_row(_message(document=document)))

    assert requests == []
    assert sent == []


async def test_a_rejected_token_on_the_upload_reschedules_and_alerts(db_pool: Pool, tmp_path: Path) -> None:
    """D194, BR-O03: a 401 on the media upload reschedules the row and alerts once, as text does."""
    await _prepare(db_pool, window_hours_ago=1)
    rejected: list[None] = []

    async def on_token_rejected() -> None:
        rejected.append(None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": 190, "message": "Expired"}})

    requests: list[httpx.Request] = []
    async with _transport(db_pool, handler, on_token_rejected=on_token_rejected, requests=requests) as (
        transport,
        sent,
    ):
        with pytest.raises(SendLater) as raised:
            await transport(_row(_message(document=_document(tmp_path))))

    assert raised.value.delay == timedelta(minutes=15)
    assert raised.value.reason == "BR-O03"
    assert len(rejected) == 1
    assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == ["media"]
    assert sent == []


async def test_window_expired_on_the_document_send_is_permanent(db_pool: Pool, tmp_path: Path) -> None:
    """D194: Meta's closed-window answer on the document send is permanent, not a template fallback."""
    await _prepare(db_pool, window_hours_ago=1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "m1"})
        return httpx.Response(400, json={"error": {"code": 131047, "message": "Re-engagement"}})

    async with _transport(db_pool, handler) as (transport, _sent):
        with pytest.raises(PermanentSendError, match="outside the 24-hour window"):
            await transport(_row(_message(document=_document(tmp_path))))


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


async def test_closed_window_without_a_slot_is_permanent(db_pool: Pool) -> None:
    """R-1.10, R-11.5: a closed window for a rule with no template fails permanently, no request."""
    await _prepare(db_pool, window_hours_ago=None)
    async with _transport(db_pool) as (transport, sent):
        with pytest.raises(PermanentSendError):
            await transport(_row(_message(rule_id="BR-S01")))

    assert sent == []


async def test_empty_template_name_is_permanent(db_pool: Pool) -> None:
    """R-1.10, R-11.5: a configured slot whose name is empty counts as missing, so it is permanent."""
    await _prepare(db_pool, window_hours_ago=None)
    templates = TEMPLATES.model_copy(update={"manual_required": ""})
    async with _transport(db_pool, templates=templates) as (transport, sent):
        with pytest.raises(PermanentSendError, match="manual_required"):
            await transport(_row(_message(rule_id="BR-F07")))

    assert sent == []


async def test_recipient_missing_from_numbers_is_permanent(db_pool: Pool) -> None:
    """R-11.5: a recipient with no WhatsApp number can never be sent, so no request is made."""
    await _prepare(db_pool, window_hours_ago=1)
    async with _transport(db_pool) as (transport, sent):
        with pytest.raises(PermanentSendError):
            await transport(_row(_message(), to="desconocido"))

    assert sent == []


async def test_a_permanent_meta_code_is_a_permanent_failure(db_pool: Pool) -> None:
    """R-11.5: Meta's code 131026 (undeliverable) raises PermanentSendError."""
    await _prepare(db_pool, window_hours_ago=None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": 131026, "message": "Undeliverable"}})

    async with _transport(db_pool, handler) as (transport, _sent):
        with pytest.raises(PermanentSendError):
            await transport(_row(_message(rule_id="BR-F07")))


async def test_a_permanent_meta_code_reports_only_its_status_and_code(db_pool: Pool) -> None:
    """R-9.16: a permanent Meta error keeps its status and code, never Meta's free text."""
    await _prepare(db_pool, window_hours_ago=None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": 131026, "message": "Param text['body'] = 0000"}})

    async with _transport(db_pool, handler) as (transport, _sent):
        with pytest.raises(PermanentSendError) as raised:
            await transport(_row(_message(rule_id="BR-F07")))

    assert str(raised.value) == "WhatsApp API error 400: code 131026"
    assert "0000" not in str(raised.value)


async def test_a_transient_meta_code_stays_a_whatsapp_error(db_pool: Pool) -> None:
    """R-11.5: Meta's code 1 on a 500 is transient, so it is not a PermanentSendError."""
    await _prepare(db_pool, window_hours_ago=None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"code": 1, "message": "Unknown"}})

    async with _transport(db_pool, handler) as (transport, _sent):
        with pytest.raises(WhatsAppError):
            await transport(_row(_message(rule_id="BR-F07")))


async def test_a_401_token_rejection_reschedules_and_alerts(db_pool: Pool) -> None:
    """BR-O03: a 401 reschedules the row for 15 minutes, without an attempt, and alerts once."""
    await _prepare(db_pool, window_hours_ago=1)
    rejected: list[None] = []

    async def on_token_rejected() -> None:
        rejected.append(None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": 190, "message": "Expired"}})

    async with _transport(db_pool, handler, on_token_rejected=on_token_rejected) as (transport, _sent):
        with pytest.raises(SendLater) as raised:
            await transport(_row(_message()))

    assert raised.value.delay == timedelta(minutes=15)
    assert raised.value.reason == "BR-O03"
    assert len(rejected) == 1


async def test_a_400_with_token_code_190_reschedules_and_alerts(db_pool: Pool) -> None:
    """BR-O03: Meta's code 190 on a 400 reschedules the row for 15 minutes and alerts once."""
    await _prepare(db_pool, window_hours_ago=1)
    rejected: list[None] = []

    async def on_token_rejected() -> None:
        rejected.append(None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": 190, "message": "Invalid token"}})

    async with _transport(db_pool, handler, on_token_rejected=on_token_rejected) as (transport, _sent):
        with pytest.raises(SendLater) as raised:
            await transport(_row(_message()))

    assert raised.value.delay == timedelta(minutes=15)
    assert raised.value.reason == "BR-O03"
    assert len(rejected) == 1


async def test_a_non_token_400_keeps_the_whatsapp_error(db_pool: Pool) -> None:
    """BR-O03: a 400 with another non-permanent code stays a WhatsAppError and does not alert."""
    await _prepare(db_pool, window_hours_ago=1)
    rejected: list[None] = []

    async def on_token_rejected() -> None:
        rejected.append(None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": 2, "message": "Service unavailable"}})

    async with _transport(db_pool, handler, on_token_rejected=on_token_rejected) as (transport, _sent):
        with pytest.raises(WhatsAppError):
            await transport(_row(_message()))

    assert rejected == []


async def test_template_language_comes_from_the_transport(db_pool: Pool) -> None:
    """R-1.10: a template send uses the configured language, not a hard-coded one."""
    await _prepare(db_pool, window_hours_ago=None)
    async with _transport(db_pool, language="en_US") as (transport, sent):
        await transport(_row(_message(rule_id="BR-F07")))

    assert sent[0]["template"]["language"] == {"code": "en_US"}


async def test_every_admin_rule_has_the_admin_alert_template() -> None:
    """R-1.10, BR-S04: an admin alert reaches an admin whose window is closed, not a dead row."""
    admin_rules = {rule_id for rule_id in MESSAGES if rule_id.endswith("_admin")}

    assert admin_rules
    assert {rule_id: TEMPLATE_SLOT_BY_RULE.get(rule_id) for rule_id in admin_rules} == dict.fromkeys(
        admin_rules, "admin_alert"
    )


async def test_closed_window_admin_alert_uses_the_admin_template(db_pool: Pool) -> None:
    """R-1.10, BR-D03: BR-D03's admin text goes out as the admin_alert template outside the window."""
    await _prepare(db_pool, window_hours_ago=None)
    async with _transport(db_pool) as (transport, sent):
        await transport(_row(_message(rule_id="BR-D03_admin")))

    assert sent[0]["template"]["name"] == "admin_alert"


async def test_message_template_wins_over_the_rule_table(db_pool: Pool) -> None:
    """R-1.10: the message's own template slot overrides its rule's slot."""
    await _prepare(db_pool, window_hours_ago=None)
    async with _transport(db_pool) as (transport, sent):
        await transport(_row(_message(rule_id="BR-F07", template="admin_alert")))

    assert len(sent) == 1
    assert sent[0]["template"]["name"] == "admin_alert"


async def test_a_free_form_send_returns_metas_message_id(db_pool: Pool) -> None:
    """D102: a 200 answer with messages[0].id returns that id to the outbox."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": [{"id": "wamid.A"}]})

    await _prepare(db_pool, window_hours_ago=1)
    async with _transport(db_pool, handler) as (transport, _sent):
        assert await transport(_row(_message())) == "wamid.A"


async def test_a_send_without_messages_returns_none_and_warns_with_the_row_id(
    db_pool: Pool, caplog: pytest.LogCaptureFixture
) -> None:
    """D102: a 200 without messages is delivered with no id and logs a warning with the row id."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    await _prepare(db_pool, window_hours_ago=1)
    with caplog.at_level(logging.WARNING, logger="invoicebot.channels.whatsapp.transport"):
        async with _transport(db_pool, handler) as (transport, _sent):
            assert await transport(_row(_message())) is None

    assert "outbox row 1" in caplog.text
    assert NUMBER not in caplog.text
    assert "hola" not in caplog.text


async def test_a_template_send_returns_its_message_id(db_pool: Pool) -> None:
    """D102: a closed-window template send also returns Meta's message id."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": [{"id": "wamid.T"}]})

    await _prepare(db_pool, window_hours_ago=None)
    async with _transport(db_pool, handler) as (transport, sent):
        assert await transport(_row(_message(rule_id="BR-F07"))) == "wamid.T"

    assert sent[0]["type"] == "template"


@pytest.mark.parametrize("message_id", ["a" * 257, "", 42])
async def test_an_unusable_message_id_is_treated_as_missing(db_pool: Pool, message_id: object) -> None:
    """D102: an id over 256 characters, empty or not a string is treated as missing."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": [{"id": message_id}]})

    await _prepare(db_pool, window_hours_ago=1)
    async with _transport(db_pool, handler) as (transport, _sent):
        assert await transport(_row(_message())) is None


async def test_outbox_sender_marks_the_row_dead_after_template_missing(db_pool: Pool) -> None:
    """R-11.5: a missing template is permanent, so the outbox marks the row dead at once."""
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
    assert row[0] == "dead"
    assert row[1] == 1
    assert row[2].startswith("PermanentSendError")
