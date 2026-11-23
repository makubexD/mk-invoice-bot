# Plan: Telegram phase T1 · telegram-adapter

Roadmap: `docs/plan-telegram.md`. Adds Telegram as a second chat channel through the seams T0 built: a
`channels/telegram/` package (client, parser, transport, media source), a webhook router in `api/`, a builder in
`wiring/` and one line in `CHANNEL_BUILDERS`. Nothing in the pipeline changes. Everything is tested offline with
`httpx.MockTransport`; the bot, the token and the live run are T2.

In scope: R-1.7, R-1.8, R-1.9, R-1.11, R-9.16, BR-O03 for Telegram; S14.1 (conversation parity over the fake, WhatsApp
and Telegram inbound paths); S14.2 and D176 (cross-channel upload and `/estado`); the T0 review follow-ups listed in the
roadmap; the two pieces of shared knowledge a second adapter would otherwise copy (the document mime table and the
capped media writer) and the BR-O03 token alert.

Out of scope: the `setWebhook`/`getWebhookInfo` CLI subcommand and the user-id helper (T2 decides both, roadmap § T2
steps 7 and 9); a polling mode; reading an XML sent as `application/octet-stream` by its file name (T2's live run shows
whether Telegram clients do that).

Decisions this plan proposes (step 1): D180–D183 below.

- **D180 Telegram outbound:** plain text without `parse_mode`, at most 4096 characters (a longer text is a
  `PermanentSendError`, as WhatsApp's `text_payload` refuses it); buttons as an inline keyboard, one button per row,
  `callback_data` = the button id (≤ 64 bytes, T0 step 2); no window and no templates, so `OutboundMessage.template` is
  ignored (R-1.10). Errors: 429 → `SendLater(retry_after)`; 401 → BR-O03 alert and `SendLater(TOKEN_RETRY_DELAY)`; 400
  and 403 → `PermanentSendError` (403 = the user never pressed Start or blocked the bot); 5xx and network errors are
  raised for the outbox's normal retry.
- **D181 Telegram media:** a photo update carries several sizes; the parser keeps the largest whose `file_size` fits the
  channel cap, else the largest. The download cap is the smaller of the kind's `limits.media` cap and `getFile`'s 20 MB
  (R-1.11), checked against `getFile`'s `file_size` before any byte is read and again while streaming. A document's kind
  comes from its mime type through the same table WhatsApp uses.
- **D182 no `mode` or `api_base` config:** the roadmap sketch named both; neither has a caller (webhook is the only
  mode, D173, and `httpx.MockTransport` serves tests whatever the host), so `TelegramChannel` stays `{kind, enabled}`.
- **D183 one "configured" rule:** `ChannelBuilder` gains `configured(cfg) -> bool`; startup and the reload check both
  decide "no chat channel" from it, so neither special-cases the console (T0 review follow-up).

## Step 1: D180–D183, roadmap row `[inline: decision and spec wording]`

1. `specs/05-open-questions.md`, after D179: rows D180–D183 worded as above, each "(T1 plan, proposed)".
2. `docs/plan-telegram.md`, phases table: the T1 row's Plan cell becomes `docs/plan-telegram-adapter.md`, and its What
   cell drops "`TelegramChannel` config (`mode` fixed to webhook, `api_base`);" with "(D182)" after the sentence.

Test: `grep -n "D18[0-3]" specs/05-open-questions.md docs/plan-telegram.md` shows each decision.

## Step 2: One home for the document mime table `[worker]`

Files: `src/invoicebot/channels/media_limits.py`, `src/invoicebot/channels/whatsapp/parse.py`,
`tests/unit/test_media_limits.py`.

1. Move `_DOCUMENT_KINDS` and `_document_kind` from `whatsapp/parse.py` to `media_limits.py` as the public
   `DOCUMENT_KINDS` and `document_kind(mime: str) -> AttachmentKind | None` (docstring: "Return the attachment kind a
   channel's document mime type maps to, or `None` when it is not kept (R-1.4)."). Behavior unchanged: `image/*` →
   `photo`, `application/pdf` → `pdf`, `text/xml` and `application/xml` → `xml`, anything else → `None`.
2. `whatsapp/parse.py` imports `document_kind` from `media_limits`.
3. Tests, one parametrized test, docstring `R-1.4`: `image/png` → `photo`, `image/jpeg` → `photo`, `application/pdf` →
   `pdf`, `text/xml` → `xml`, `application/xml` → `xml`, `application/zip` → `None`, `text/plain` → `None`.

Test: `uv run pytest -q tests/unit/test_media_limits.py tests/unit/test_whatsapp_parse.py`

## Step 3: One home for the capped media writer `[worker]`

Files: `src/invoicebot/channels/media_limits.py`, `src/invoicebot/channels/whatsapp/media.py`,
`tests/unit/test_media_limits.py`.

1. Move `_stream_to_file` and `_append_bytes` from `whatsapp/media.py` to `media_limits.py`. The writer becomes
   `async def write_capped_stream(chunks: AsyncIterator[bytes], path: Path, cap: ByteCap, kind: AttachmentKind, limits: MediaLimits) -> tuple[int, str]`:
   it takes the byte iterator instead of the client and the `MediaInfo`, and keeps the docstring and the photo header
   hold (BR-E07) word for word. `_append_bytes` stays private in `media_limits.py`.
2. `whatsapp/media.py` opens `client.stream_media(info.url)` itself and passes the body to `write_capped_stream`.
3. Tests, docstrings `R-1.11` / `BR-E07`, over an async generator of chunks and `tmp_path`:
   - two PDF chunks under the cap → the file holds both, the returned size is their total and the hash their SHA-256;
   - PDF chunks over the cap → raises `MediaRejected("too_large")`;
   - a photo whose first 512 KiB fail `check_image_header` → raises and the file holds 0 bytes;
   - a valid photo under 512 KiB in one chunk → written whole after the header check.

Test: `uv run pytest -q tests/unit/test_media_limits.py tests/unit/test_whatsapp_media.py`

## Step 4: The BR-O03 token alert is channel-neutral `[worker]`

Files: `src/invoicebot/queue/retry.py`, `src/invoicebot/channels/whatsapp/transport.py`,
`src/invoicebot/wiring/channels.py`, `src/invoicebot/wiring/whatsapp.py`.

1. Move `TOKEN_RETRY_DELAY` and `RULE_TOKEN_REJECTED` (with its `noqa`/`nosec` comment) from `whatsapp/transport.py` to
   `queue/retry.py`; the transport imports them. `TOKEN_REJECTED_STATUS` and `TOKEN_REJECTED_CODE` stay: they are Meta's
   codes.
2. Move `_alert_channel_token` from `wiring/whatsapp.py` to `wiring/channels.py` as the public
   `alert_channel_token(pool, cfg, channel)`. Delete its `shortcut:` comment: the alert goes to the owner's preferred
   channel, which with two channels need not be the one whose token failed. `wiring/whatsapp.py` imports it.
3. If a test imports either old name, change only that import.

Test: `uv run pytest -q tests/unit/test_wiring_whatsapp.py tests/integration/test_whatsapp_transport.py`

## Step 5: Builders say whether their channel is configured `[worker]`

Files: `src/invoicebot/wiring/channels.py`, `src/invoicebot/wiring/whatsapp.py`, `src/invoicebot/wiring/console.py`,
`tests/unit/test_wiring_whatsapp.py`.

1. `ChannelBuilder` gains `def configured(self, cfg: Config) -> bool:` with the docstring "Return whether `cfg` enables
   this channel, whatever its secrets (D183)." Update the protocol docstring: a builder returns `None` from `__call__`
   exactly when `configured` is false or its secrets are incomplete.
2. `WhatsAppBuilder.configured` returns `_whatsapp_channel(cfg) is not None`; `__call__` and `secret_problem` use it
   instead of repeating the lookup. `ConsoleBuilder.configured` returns `console_channel(cfg) is not None` (D165).
3. Tests, docstring `D183`: WhatsApp `configured` is true for `config/example.yaml` and false with the WhatsApp channel
   removed. Replace every use of the private `_whatsapp_secrets` in this file with the public path
   (`whatsapp_wiring.secret_problem` on a store lacking a key returns `secrets:whatsapp`).

Test: `uv run pytest -q tests/unit/test_wiring_whatsapp.py tests/unit/test_app_console.py`

## Step 6: Startup and reload share the "no chat channel" rule `[worker]`

Files: `src/invoicebot/app.py`, `tests/unit/test_app_wiring.py`.

1. `_secret_problem(cfg, directory, store)` gains a `builders: tuple[ChannelBuilder, ...]` parameter; its caller passes
   `_channel_builders(...)` with the same data dir the runtime starts with. `channels:none` becomes
   `not any(builder.configured(cfg) for builder in builders)`; it loops over `builders`, not `CHANNEL_BUILDERS`.
2. Delete the `secrets:channels` entry and the `console_enabled` line: every network builder's own `secret_problem`
   reports `secrets:channels` already, and the console needs no shared secret.
3. `tests/unit/test_app_wiring.py`: drop the import of `_whatsapp_secrets` (T0 follow-up); build the failing store
   through `WHATSAPP_SECRETS` and assert through `secret_problem` only. Add, docstring `D183`: a console-only config and
   a WhatsApp-only config both give no `channels:none`; a config with no channel gives `channels:none`.

Test: `uv run pytest -q tests/unit/test_app_wiring.py tests/unit/test_app_console.py`

## Step 7: Contract and cap pins from the T0 review `[worker]`

Files: `.importlinter`, `tests/unit/test_whatsapp_payloads.py`.

1. The "Only the composition root builds channels" contract lists every top-level `invoicebot` package and module except
   `invoicebot.app` and `invoicebot.wiring` itself as `source_modules` (list them from `ls src/invoicebot`), so only
   `app` may import `invoicebot.wiring`.
2. Test, docstring `R-1.7`: `config.checks.BUTTON_TITLE_MAX <= BUTTONS_LABEL_MAX_CHARS` and
   `<= LIST_ROW_TITLE_MAX_CHARS` (Telegram documents no title cap, so WhatsApp's are the binding ones).

Test: `uv run lint-imports && uv run pytest -q tests/unit/test_whatsapp_payloads.py`

## Step 8: An imported claim takes its own profile's channel `[worker]`

Files: `src/invoicebot/importer.py`, `scripts/import_claims.py`, `tests/unit/test_importer.py`.

1. `import_origin_channel(cfg, profile_id)`: the named profile's `preferred_channel` when it has one, else the owner
   profile's (R-1.9, D110). Docstring and `Raises` updated: `ValueError` only when neither has one.
2. `scripts/import_claims.py` passes the profile the claims are registered under (the `--profile` value, else the owner
   profile).
3. Tests, docstring `R-1.9`: a profile preferring `telegram` under an owner preferring `whatsapp` → `telegram`; a
   profile without a preference → the owner's channel; neither has one → `ValueError`. Adapt the two existing tests to
   the new signature.

Test: `uv run pytest -q tests/unit/test_importer.py`

## Step 9: Telegram Bot API client `[worker]`

Files: `src/invoicebot/channels/telegram/__init__.py` (new, docstring only),
`src/invoicebot/channels/telegram/client.py` (new), `tests/unit/test_telegram_client.py` (new).

1. `client.py`, modeled on `channels/whatsapp/client.py`: `API_BASE_URL = "https://api.telegram.org"`,
   `REQUEST_TIMEOUT_SECONDS = 30.0`, `GETFILE_MAX_BYTES = 20 * 1024 * 1024` (R-1.11).
2. `class TelegramError(Exception)` with `status: int`, `code: int | None` (the body's `error_code`),
   `retry_after: int | None` (`parameters.retry_after`, a positive int or `None`); its text is built from the status,
   code and `description` only, never the URL or token (R-9.16). `NOT_SENT_STATUS = 0` for errors before an answer.
3. `class TelegramClient(http: httpx.AsyncClient, token: str)` with `__repr__` returning `"TelegramClient()"`:
   - `async def call(method: str, payload: dict[str, Any]) -> object`: POSTs JSON to
     `{API_BASE_URL}/bot{token}/{method}` with the timeout; an `ok: true` body returns `result`; anything else raises
     `TelegramError`. An `httpx.HTTPError` becomes `TelegramError(status=NOT_SENT_STATUS, message=<class name>)` raised
     `from None`, so no URL travels in the chain.
   - `send_message(payload) -> tuple[int, int]` returns `(chat.id, message_id)` from the result, raising `TelegramError`
     when either is missing or not an int.
   - `answer_callback_query(callback_query_id: str) -> None`.
   - `get_file(file_id: str) -> FileInfo` (frozen dataclass `file_path: str`, `size: int | None`); a `file_path` that is
     empty, longer than 256 characters, absolute or holds `..` raises `TelegramError`.
   - `stream_file(file_path)` as an async context manager yielding the byte iterator of
     `{API_BASE_URL}/file/bot{token}/{file_path}`; a non-2xx raises `TelegramError` before yielding.
4. Tests over `httpx.MockTransport`, docstrings citing R-1.7, R-1.11 or R-9.16:
   - `send_message` ok → returns `(chat_id, message_id)` and the request path is `/bot<token>/sendMessage`;
   - 429 with `retry_after: 7` → `TelegramError` with `status 429`, `retry_after 7`;
   - 401 → `TelegramError(status=401)`; `str(err)` and `repr(client)` hold no part of the token;
   - `ok: false` with status 200 → `TelegramError`;
   - a non-JSON 502 → `TelegramError(status=502, code=None)`;
   - `httpx.ConnectError` → `TelegramError(status=0)` whose `__cause__` and `__context__` are `None`;
   - `get_file` with `file_path` `"../x"`, `"/x"`, `""` → `TelegramError`; `"photos/file_1.jpg"` → returned;
   - R-9.16: with `configure_logging` on a `StringIO` stream, `logger.exception` on a 401 from `send_message` writes no
     part of the token.

Test: `uv run pytest -q tests/unit/test_telegram_client.py`

## Step 10: Telegram update parser `[worker]`

Files: `src/invoicebot/channels/telegram/parse.py` (new), `tests/unit/test_telegram_parse.py` (new).

1. `parse_update(payload, index, *, owner_profile, admin_profiles, sender_hash_key, now: datetime) -> ParsedUpdate`;
   `ParsedUpdate` is a frozen dataclass `messages: list[InboundMessage]`, `unknown_senders: list[str]`,
   `callback_query_ids: list[str]` (the presses to acknowledge, D177). Untrusted body: every field checked, a malformed
   update returns an empty result, never raises (mirror `whatsapp/parse.py`'s helpers).
2. A `message` update: needs int `message_id`, int `from.id`, `chat.type == "private"` and `chat.id == from.id` (D175;
   anything else is dropped without a hash), int `date` in range. `external_id` and `reply_to_external_id` are
   `"<chat_id>:<message_id>"` (D178). Text from `text`, else `caption`, through `replace_unsafe_chars`; a leading
   `/cmd@name` becomes `/cmd`. A `photo` list → one `photo` attachment per D181 (largest whose `file_size` ≤
   `GETFILE_MAX_BYTES`, else the largest), mime `image/jpeg`, `media_ref` = its `file_id`, `size` = `file_size` or 0. A
   `document` → `document_kind(mime_type)`; an unknown kind gives a message with no attachment (as WhatsApp). A
   `file_id` must match `[A-Za-z0-9_-]{1,255}`, else the update is dropped (the media-id check the roadmap carried over
   to T1).
3. A `callback_query` update: `external_id` = its `id` (D178), `button_id` = `data`, `reply_to_external_id` =
   `"<chat_id>:<message.message_id>"` when present, `received_at = now` (a press has no date), private-chat rule as
   above via `message.chat`. Its id goes into `callback_query_ids` only when the sender resolved to a profile.
4. Senders resolve with `index.resolve("telegram", from_id)`; an unknown one adds `sender_hash(key, str(from_id))`.
5. Tests, docstrings citing R-1.7, R-1.8, D175, D178 or D181, one per case:
   - text from a known user → one message, `external_id "<chat>:<msg>"`, role from `sender_role`;
   - `/estado@mk_facturas_bot` → text `/estado`;
   - photo with three sizes, the largest over 20 MB → attachment is the middle one;
   - document `application/pdf` → `pdf`; `application/zip` → message with no attachment;
   - caption on a photo → text = caption;
   - `reply_to_message` → `reply_to_external_id "<chat>:<replied id>"`;
   - callback query → `button_id`, `external_id` = query id, its id in `callback_query_ids`;
   - group chat (`chat.type "group"`) → nothing; `chat.id != from.id` → nothing;
   - unknown user → no message, one digest in `unknown_senders`, the raw id absent from it;
   - `file_id` with `/` → nothing; `date` as a string → nothing; payload not a dict field shape (`message: []`) →
     nothing.

Test: `uv run pytest -q tests/unit/test_telegram_parse.py`

## Step 11: Telegram outbox transport `[worker]`

Files: `src/invoicebot/channels/telegram/transport.py` (new), `tests/unit/test_telegram_transport.py` (new).

1. `make_transport(client, *, user_ids: Mapping[str, int], on_token_rejected) -> Transport` per D180. `row.to` is a
   profile id; a missing user id → `PermanentSendError`. Payload `{"chat_id", "text"}` plus
   `{"reply_markup": {"inline_keyboard": [[{"text": b.title, "callback_data": b.id}] for b in buttons]}}` when there are
   buttons. Text over `TEXT_MAX_CHARS = 4096` or a button id over `CALLBACK_DATA_MAX_BYTES = 64` UTF-8 bytes →
   `PermanentSendError`. Returns `"<chat_id>:<message_id>"` (D178).
2. Errors: `retry_after` set (429) → `SendLater(timedelta(seconds=retry_after), "telegram_rate_limited")`; 401 → await
   `on_token_rejected()` then `SendLater(TOKEN_RETRY_DELAY, RULE_TOKEN_REJECTED)` (BR-O03, `queue/retry.py`); 400 or 403
   → `PermanentSendError` with status and code only; other errors re-raised.
3. Tests over `httpx.MockTransport` and a real `TelegramClient`, docstrings citing D180, BR-O03, R-1.10 or D178: plain
   text sends no `reply_markup` and no `parse_mode`; three buttons → three one-button rows in order; returned id
   `"<chat>:<msg>"`; a row with `template` set still sends its plain text; unknown profile → `PermanentSendError` and no
   request; 4097-character text → `PermanentSendError`; 65-byte button id → `PermanentSendError`; 429 `retry_after 3` →
   `SendLater` of 3 s; 401 → callback awaited once and `SendLater` with `BR-O03`; 403 → `PermanentSendError`; 500 →
   `TelegramError` raised.

Test: `uv run pytest -q tests/unit/test_telegram_transport.py`

## Step 12: Telegram media source `[worker]`

Files: `src/invoicebot/channels/telegram/media.py` (new), `tests/unit/test_telegram_media.py` (new).

1. `TelegramMediaSource(client)` implementing `MediaSource.download` (R-1.5), modeled on `whatsapp/media.py`: check the
   `file_id` pattern (`ValueError`), `limit = min(max_bytes_for(kind, limits), GETFILE_MAX_BYTES)`, `get_file` on every
   attempt; `size > limit` → `MediaRejected("too_large")` before streaming; stream with `write_capped_stream` under
   `asyncio.timeout(DOWNLOAD_TIMEOUT_S)` (import the WhatsApp constant's value as its own `DOWNLOAD_TIMEOUT_S = 120.0`
   with the same "below the worker lease" comment); directory = `out_dir / file_id`, removed on any failure. Mime:
   `image/jpeg` for `photo`, `application/pdf` for `pdf`, `application/xml` for `xml`.
2. `TelegramError` → `MediaTransient` for status 0, ≥ 500, 401, 408 or 429; else `MediaUnusable`; each `from` the
   original.
3. Tests over `httpx.MockTransport`, docstrings citing R-1.4, R-1.11 or BR-E07: a PDF downloads with the right hash and
   size; `file_size` 21 MB → `MediaRejected("too_large")` with no file request made; streamed bytes over the cap with no
   `file_size` → `MediaRejected`, directory gone; 502 on `getFile` → `MediaTransient`; 400 → `MediaUnusable`; a bad
   photo header → rejected, directory gone; `file_id` `"a/b"` → `ValueError`.

Test: `uv run pytest -q tests/unit/test_telegram_media.py`

## Step 13: Telegram webhook router `[worker]`

Files: `src/invoicebot/api/telegram_webhook.py` (new), `tests/integration/test_telegram_webhook.py` (new).

1. `TelegramWebhookDeps` (frozen dataclass: `intake_deps`, `index`, `owner_profile`, `webhook_secret`,
   `sender_hash_key`, `client`) and `make_router(deps)` with `POST /webhook/telegram` (D173): `read_body` (413 when
   over); `X-Telegram-Bot-Api-Secret-Token` compared with `hmac.compare_digest` on bytes (401 when missing or wrong);
   `parse_json` (400); `parse_update(..., now=datetime.now(UTC))`; unknown senders logged by digest as in
   `api/webhook.py`; `response = await intake(...)`.
2. Only when `response.status_code == 200`: for each callback id, `await client.answer_callback_query(id)` inside
   `asyncio.timeout(5)`; a `TelegramError` or `TimeoutError` is logged at WARNING with the error class only and ignored
   (D177). Return `response`.
3. Integration tests (real Postgres, `httpx.ASGITransport` on a FastAPI app with this router, the client on
   `httpx.MockTransport`), docstrings citing R-1.7, R-1.8, D173 or D177:
   - wrong or missing secret header → 401, nothing stored;
   - a 2 MB body → 413;
   - a text update from a known user → 200 and one `messages` row with channel `telegram`;
   - the same update twice → one row (R-1.8);
   - a callback query → 200 and exactly one `answerCallbackQuery` request with its id;
   - `answerCallbackQuery` answering 500 → still 200;
   - an unknown user → 200, no row, no `answerCallbackQuery`.

Test: `uv run pytest -q tests/integration/test_telegram_webhook.py`

## Step 14: Telegram builder in the registry `[worker]`

Files: `src/invoicebot/wiring/telegram.py` (new), `src/invoicebot/app.py`, `tests/unit/test_wiring_telegram.py` (new).

1. `wiring/telegram.py`, modeled on `wiring/whatsapp.py`: `TELEGRAM_ADAPTER: ChannelName = "telegram"`,
   `TELEGRAM_SECRETS = frozenset({"telegram_bot_token", "telegram_webhook_secret"})`, `TelegramBuilder` with
   `configured` (a `TelegramChannel` with `enabled: true`), `__call__` (fail closed and log the missing key names, as
   WhatsApp), `secret_problem` (`secrets:telegram`, then `secrets:channels`). The transport's `user_ids` come from each
   profile's `telegram_user_id`; `on_token_rejected` is `partial(alert_channel_token, pool, cfg, "Telegram")`. Export
   `telegram_wiring: ChannelBuilder = TelegramBuilder()`.
2. `app.py`: `CHANNEL_BUILDERS = (whatsapp_wiring, telegram_wiring)`. Nothing else in `app.py` changes; if something
   must, stop and report it (the T0 seam is incomplete).
3. Tests, docstrings citing D174, D183 or R-9.10: disabled channel → `configured` false and `__call__` returns `None`;
   enabled with both secrets → a wiring named `telegram` with transport, media source and a router holding
   `/webhook/telegram`; enabled without `telegram_bot_token` → `None` and `secret_problem` `secrets:telegram`; enabled
   without the shared `sender_hash_key` → `secrets:channels`.

Test: `uv run pytest -q tests/unit/test_wiring_telegram.py tests/unit/test_app_wiring.py && uv run lint-imports`

## Step 15: Conversation parity over three inbound paths (S14.1) `[worker]`

Files: `tests/integration/test_conversation_channels.py` (new).

One module parametrized over `inbound` ∈ {`fake`, `whatsapp`, `telegram`}: `fake` builds the canonical `InboundMessage`
directly; `whatsapp` builds a Meta webhook payload and runs `parse_webhook`; `telegram` builds an update and runs
`parse_update`. Each case feeds the result to `handle_inbound` and runs the outbox through `FakeChannel` for that
channel name, reusing the fixtures pattern of `test_conversation_fake.py` (copy what is needed; do not import from
another test module). Cases, docstring `S14.1` plus the rule:

- one photo → the outbox holds the BR-S09 acknowledgement, addressed on the inbound channel;
- the same delivery twice → one acknowledgement only (R-1.8);
- a button press whose id names an invoice not waiting for it → BR-S02;
- `/estado` from the owner → one reply on the inbound channel (S11.4).

Expected: all 12 parametrized cases pass with identical rule ids across the three paths.

Test: `uv run pytest -q tests/integration/test_conversation_channels.py`

## Step 16: Cross-channel upload and `/estado` (S14.2, D176) `[worker]`

Files: `tests/integration/test_conversation_cross_channel.py` (new).

A profile with both a WhatsApp number and a Telegram user id. Cases, docstrings `S14.2` / `D176` / `R-1.9`:

- an invoice opened by a WhatsApp photo, waiting for its PDF at BR-F07; the PDF arrives as a Telegram document → the
  attach job matches that invoice, and the next outbox row about it has channel `whatsapp` (origin);
- the same invoice; `/estado` sent from Telegram → the reply lists it and goes out on `telegram` (the command's
  channel);
- a Telegram message quoting a WhatsApp `external_id` → no quote match (quote resolution stays within one channel).

If the first or second case fails because the pipeline filters open invoices by channel, stop and report: that is a
design gap for the main session, not a worker fix.

Test: `uv run pytest -q tests/integration/test_conversation_cross_channel.py`

## Step 17: Docs for the second channel `[worker:low]`

Files: `README.md`, `docs/architecture.md`, `config/example.yaml`.

1. `README.md` status: Telegram adapter built and tested offline (T1); live setup is T2 (`docs/plan-telegram.md`).
2. `docs/architecture.md` components diagram: a `channels/telegram` node next to `channels/whatsapp`, an
   `api/telegram_webhook` route node, both drawn the same way as their WhatsApp counterparts.
3. `config/example.yaml`, the telegram channel's comment: secrets are `telegram_bot_token` and `telegram_webhook_secret`
   in the age store (the comment's upper-case env-style names are wrong since R-9.9).

Test: `uv run pytest -q tests/unit/test_docs.py tests/unit/test_config_models.py`
