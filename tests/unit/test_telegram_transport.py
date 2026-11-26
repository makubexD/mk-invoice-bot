"""Tests for the Telegram outbox transport (D178, D180, BR-O03, R-1.10)."""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from invoicebot.channels.base import Button, Document, Link, OutboundMessage
from invoicebot.channels.telegram.client import TelegramClient, TelegramError
from invoicebot.channels.telegram.transport import (
    CALLBACK_DATA_MAX_BYTES,
    CAPTION_MAX_CHARS,
    DOCUMENT_MAX_BYTES,
    TEXT_MAX_CHARS,
    make_transport,
)
from invoicebot.queue.outbox import OutboxRow, PermanentSendError, SendLater, Transport
from invoicebot.queue.retry import MAX_BACKOFF_MINUTES

PROFILE = "yo"
USER_ID = 999001
CHAT_ID = 999001
MESSAGE_ID = 42
# Split so the scanner does not read the synthetic value as a committed credential.
TOKEN = "123456" + ":ABC-fake-token"

Handler = Callable[[httpx.Request], httpx.Response]
Payload = dict[str, Any]


def _accept(request: httpx.Request) -> httpx.Response:
    """Answer a send with a Telegram message id and its chat."""
    return httpx.Response(
        200,
        json={"ok": True, "result": {"message_id": MESSAGE_ID, "chat": {"id": CHAT_ID}}},
    )


def _message(**overrides: Any) -> OutboundMessage:
    """Build a canonical outbound message with the test profile's defaults."""
    fields: dict[str, Any] = {
        "channel": "telegram",
        "recipient": PROFILE,
        "profile_id": PROFILE,
        "text": "hola",
    }
    fields.update(overrides)
    return OutboundMessage(**fields)


def _row(msg: OutboundMessage, *, to: str = PROFILE, template: str | None = None) -> OutboxRow:
    """Wrap an outbound message in the due outbox row the transport receives."""
    return OutboxRow(
        id=1,
        channel="telegram",
        to=to,
        payload=msg.model_dump(mode="json"),
        template=template,
        attempts=0,
        state="pending",
    )


def _document(path: Path, *, filename: str = "factura.xlsx", size: int | None = None) -> Document:
    """Build a canonical document pointing at ``path``, with its stat size unless given."""
    return Document(
        path=str(path),
        filename=filename,
        mime="application/vnd.ms-excel",
        size=path.stat().st_size if size is None else size,
    )


async def _noop_token_rejected() -> None:
    """Record nothing: the tests that do not inspect a rejected token pass this callback."""


@contextlib.asynccontextmanager
async def _transport(
    handler: Handler = _accept,
    *,
    user_ids: Mapping[str, int] | None = None,
    on_token_rejected: Callable[[], Awaitable[None]] = _noop_token_rejected,
) -> AsyncIterator[tuple[Transport, list[Payload]]]:
    """Yield a transport plus the payloads it sends, answered through ``handler``."""
    sent: list[Payload] = []

    def recording(request: httpx.Request) -> httpx.Response:
        if request.headers.get("content-type", "").startswith("application/json"):
            sent.append(json.loads(request.content))
        return handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(recording)) as http:
        client = TelegramClient(http, token=TOKEN)
        yield (
            make_transport(
                client,
                user_ids={PROFILE: USER_ID} if user_ids is None else user_ids,
                on_token_rejected=on_token_rejected,
            ),
            sent,
        )


async def test_plain_text_sends_no_reply_markup_and_no_parse_mode() -> None:
    """D180: plain text goes out without parse_mode and without reply_markup."""
    async with _transport() as (transport, sent):
        await transport(_row(_message(text="hola")))

    assert sent == [{"chat_id": USER_ID, "text": "hola"}]
    assert "parse_mode" not in sent[0]
    assert "reply_markup" not in sent[0]


async def test_three_buttons_become_three_one_button_rows_in_order() -> None:
    """D180: buttons travel as an inline keyboard, one button per row, in order."""
    buttons = [
        Button(id="1:pick:0", title="Facturas"),
        Button(id="1:pick:1", title="Boletas"),
        Button(id="1:pick:2", title="Notas"),
    ]
    async with _transport() as (transport, sent):
        await transport(_row(_message(text="¿En qué flujo lo registro?", buttons=buttons)))

    assert sent[0]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "Facturas", "callback_data": "1:pick:0"}],
            [{"text": "Boletas", "callback_data": "1:pick:1"}],
            [{"text": "Notas", "callback_data": "1:pick:2"}],
        ]
    }


async def test_a_button_and_a_link_give_the_callback_row_then_the_url_row() -> None:
    """D194: a message with one button and one link shows the button first, then the link as a URL button."""
    msg = _message(
        text="📧 Revisa el correo",
        buttons=[Button(id="1:send", title="Enviar")],
        links=[Link(title="📂 Abrir carpeta", url="https://x.example/f")],
    )
    async with _transport() as (transport, sent):
        await transport(_row(msg))

    assert sent[0]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "Enviar", "callback_data": "1:send"}],
            [{"text": "📂 Abrir carpeta", "url": "https://x.example/f"}],
        ]
    }


async def test_only_a_link_still_gets_reply_markup() -> None:
    """D194: a message with a link and no buttons carries the link as its only URL button row."""
    msg = _message(links=[Link(title="📂 Abrir carpeta", url="https://x.example/f")])
    async with _transport() as (transport, sent):
        await transport(_row(msg))

    assert sent[0]["reply_markup"]["inline_keyboard"] == [
        [{"text": "📂 Abrir carpeta", "url": "https://x.example/f"}]
    ]


async def test_no_links_and_no_buttons_send_no_reply_markup() -> None:
    """D194: a message with neither links nor buttons goes out without reply_markup, as before."""
    async with _transport() as (transport, sent):
        await transport(_row(_message()))

    assert "reply_markup" not in sent[0]


async def test_the_returned_id_is_the_chat_and_message_ids() -> None:
    """D178: the transport returns "<chat_id>:<message_id>" for a later button press to point at."""
    async with _transport() as (transport, _sent):
        assert await transport(_row(_message())) == f"{CHAT_ID}:{MESSAGE_ID}"


async def test_a_delivered_message_without_ids_returns_none_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D102: an ok answer without a chat or message id means the message went out, so the row returns None."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"message_id": MESSAGE_ID}})

    with caplog.at_level(logging.WARNING, logger="invoicebot.channels.telegram.transport"):
        async with _transport(handler) as (transport, sent):
            assert await transport(_row(_message())) is None

    assert len(sent) == 1
    assert "outbox row 1" in caplog.text


async def test_a_row_with_a_template_still_sends_its_plain_text() -> None:
    """R-1.10, D180: Telegram has no templates, so a row's template is ignored and its text is sent."""
    async with _transport() as (transport, sent):
        await transport(_row(_message(text="hola", template="completion"), template="completion"))

    assert sent == [{"chat_id": USER_ID, "text": "hola"}]


async def test_unknown_profile_is_permanent_and_sends_nothing() -> None:
    """D180: a profile with no Telegram user id can never be sent, so no request is made."""
    async with _transport() as (transport, sent):
        with pytest.raises(PermanentSendError):
            await transport(_row(_message(), to="desconocido"))

    assert sent == []


async def test_a_text_over_the_cap_is_permanent_and_sends_nothing() -> None:
    """D180: text over 4096 characters is a PermanentSendError and makes no request."""
    async with _transport() as (transport, sent):
        with pytest.raises(PermanentSendError):
            await transport(_row(_message(text="a" * (TEXT_MAX_CHARS + 1))))

    assert sent == []


async def test_a_text_over_the_cap_in_utf16_units_is_permanent_and_sends_nothing() -> None:
    """D180: 2049 emoji are 4098 UTF-16 code units, over the 4096 cap, so no request is made."""
    async with _transport() as (transport, sent):
        with pytest.raises(PermanentSendError):
            await transport(_row(_message(text="😀" * 2049)))

    assert sent == []


async def test_a_text_at_the_utf16_cap_is_sent() -> None:
    """D180: 2048 emoji are 4096 UTF-16 code units, which Telegram accepts, so the row is sent."""
    text = "😀" * 2048
    async with _transport() as (transport, sent):
        assert await transport(_row(_message(text=text))) == f"{CHAT_ID}:{MESSAGE_ID}"

    assert sent == [{"chat_id": USER_ID, "text": text}]


async def test_a_callback_id_over_the_cap_is_permanent_and_sends_nothing() -> None:
    """D180: a button id over 64 UTF-8 bytes is a PermanentSendError and makes no request."""
    button = Button(id="a" * (CALLBACK_DATA_MAX_BYTES + 1), title="Factura")
    async with _transport() as (transport, sent):
        with pytest.raises(PermanentSendError):
            await transport(_row(_message(buttons=[button])))

    assert sent == []


async def test_rate_limited_reschedules_by_the_retry_after() -> None:
    """D180: a 429 with retry_after 3 waits 3 seconds without counting an attempt."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry after 3",
                "parameters": {"retry_after": 3},
            },
        )

    async with _transport(handler) as (transport, _sent):
        with pytest.raises(SendLater) as raised:
            await transport(_row(_message()))

    assert raised.value.delay == timedelta(seconds=3)
    assert raised.value.reason == "telegram_rate_limited"


async def test_a_huge_retry_after_is_clamped_to_the_max_backoff() -> None:
    """D180: a retry_after beyond the backoff cap waits MAX_BACKOFF_MINUTES, not the raw delay."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry after 999999",
                "parameters": {"retry_after": 999999},
            },
        )

    async with _transport(handler) as (transport, _sent):
        with pytest.raises(SendLater) as raised:
            await transport(_row(_message()))

    assert raised.value.delay == timedelta(minutes=MAX_BACKOFF_MINUTES)


async def test_a_rejected_token_alerts_once_and_reschedules() -> None:
    """BR-O03: a 401 alerts the owner once and waits 15 minutes for the token to be renewed."""
    rejected: list[None] = []

    async def on_token_rejected() -> None:
        rejected.append(None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})

    async with _transport(handler, on_token_rejected=on_token_rejected) as (transport, _sent):
        with pytest.raises(SendLater) as raised:
            await transport(_row(_message()))

    assert raised.value.delay == timedelta(minutes=15)
    assert raised.value.reason == "BR-O03"
    assert len(rejected) == 1


async def test_forbidden_is_permanent() -> None:
    """D180: a 403 means the user never pressed Start or blocked the bot, so the row dies."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"},
        )

    async with _transport(handler) as (transport, _sent):
        with pytest.raises(PermanentSendError) as raised:
            await transport(_row(_message()))

    assert str(raised.value) == "Telegram API error 403: code 403"
    assert "blocked" not in str(raised.value)


async def test_bad_request_is_permanent_with_status_and_code_only() -> None:
    """D180: a 400 is permanent and its message names only the status and code, never the text."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"ok": False, "error_code": 400, "description": "Bad Request: message text is empty"},
        )

    async with _transport(handler) as (transport, _sent):
        with pytest.raises(PermanentSendError) as raised:
            await transport(_row(_message(text="hola")))

    assert str(raised.value) == "Telegram API error 400: code 400"
    assert "hola" not in str(raised.value)


async def test_a_server_error_is_reraised_for_the_normal_retry() -> None:
    """D180: a 5xx is raised as TelegramError so the outbox retries it with backoff."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"ok": False, "error_code": 500, "description": "Internal Server Error"},
        )

    async with _transport(handler) as (transport, _sent):
        with pytest.raises(TelegramError):
            await transport(_row(_message()))


async def test_a_document_row_posts_send_document_with_the_file_and_caption(
    tmp_path: Path,
) -> None:
    """D194: a row with a document posts multipart sendDocument with its name, bytes and caption."""
    path = tmp_path / "factura.xlsx"
    path.write_bytes(b"xlsx-bytes")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _accept(request)

    async with _transport(handler) as (transport, _sent):
        assert await transport(_row(_message(text="Adjunto", document=_document(path)))) == (
            f"{CHAT_ID}:{MESSAGE_ID}"
        )

    request = requests[0]
    assert request.url.path == f"/bot{TOKEN}/sendDocument"
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    body = request.content
    assert b'name="chat_id"' in body
    assert str(USER_ID).encode() in body
    assert b'name="caption"' in body
    assert b"Adjunto" in body
    assert b'name="document"; filename="factura.xlsx"' in body
    assert b"xlsx-bytes" in body


async def test_a_document_row_carries_its_buttons_in_reply_markup(tmp_path: Path) -> None:
    """D194: a document's buttons and links travel as the same reply_markup JSON a text row uses."""
    path = tmp_path / "factura.xlsx"
    path.write_bytes(b"xlsx-bytes")
    msg = _message(
        document=_document(path),
        buttons=[Button(id="1:send", title="Enviar")],
        links=[Link(title="📂 Abrir carpeta", url="https://x.example/f")],
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _accept(request)

    async with _transport(handler) as (transport, _sent):
        await transport(_row(msg))

    body = requests[0].content
    assert b'name="reply_markup"' in body
    expected = {
        "inline_keyboard": [
            [{"text": "Enviar", "callback_data": "1:send"}],
            [{"text": "📂 Abrir carpeta", "url": "https://x.example/f"}],
        ]
    }
    assert json.dumps(expected).encode() in body


async def test_a_document_over_the_cap_is_permanent_and_sends_nothing(tmp_path: Path) -> None:
    """D194: a file over 50 MB by stat size is a PermanentSendError and makes no request."""
    path = tmp_path / "grande.xlsx"
    with path.open("wb") as file:
        file.truncate(DOCUMENT_MAX_BYTES + 1)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _accept(request)

    async with _transport(handler) as (transport, _sent):
        with pytest.raises(PermanentSendError):
            await transport(_row(_message(document=_document(path))))

    assert requests == []


async def test_a_missing_document_is_permanent_and_sends_nothing(tmp_path: Path) -> None:
    """D194: a document path that does not exist is a PermanentSendError and makes no request."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _accept(request)

    async with _transport(handler) as (transport, _sent):
        with pytest.raises(PermanentSendError):
            await transport(_row(_message(document=_document(tmp_path / "falta.xlsx", size=1))))

    assert requests == []


async def test_a_caption_over_the_cap_is_permanent_and_sends_nothing(tmp_path: Path) -> None:
    """D194: a caption over 1024 UTF-16 code units is a PermanentSendError and makes no request."""
    path = tmp_path / "factura.xlsx"
    path.write_bytes(b"xlsx-bytes")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _accept(request)

    caption = "😀" * (CAPTION_MAX_CHARS // 2 + 1)
    async with _transport(handler) as (transport, _sent):
        with pytest.raises(PermanentSendError):
            await transport(_row(_message(text=caption, document=_document(path))))

    assert requests == []


async def test_a_document_rate_limited_reschedules_by_the_retry_after(tmp_path: Path) -> None:
    """D194: a 429 on sendDocument maps to SendLater exactly as it does for a text row."""
    path = tmp_path / "factura.xlsx"
    path.write_bytes(b"xlsx-bytes")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry after 3",
                "parameters": {"retry_after": 3},
            },
        )

    async with _transport(handler) as (transport, _sent):
        with pytest.raises(SendLater) as raised:
            await transport(_row(_message(document=_document(path))))

    assert raised.value.delay == timedelta(seconds=3)
    assert raised.value.reason == "telegram_rate_limited"


async def test_a_text_row_posts_to_send_message() -> None:
    """D194: a row without a document still posts sendMessage."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _accept(request)

    async with _transport(handler) as (transport, _sent):
        await transport(_row(_message()))

    assert requests[0].url.path == f"/bot{TOKEN}/sendMessage"
