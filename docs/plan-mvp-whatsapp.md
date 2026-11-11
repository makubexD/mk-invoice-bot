# Plan: MVP phase 2 · whatsapp (E2)

Roadmap: `docs/plan-mvp.md`. Backlog: `specs/04-tasks.md` E2 (S2.1–S2.9). Requirements: R-1.1–R-1.6, R-1.8, R-1.10, R-1.11, R-3.2. Design: `specs/03-design.md` §1, §3, §9.

Conventions for every step:

- Everything in `docs/plan-mvp-foundation.md` "Conventions" still applies (Python 3.12, `uv`, psycopg 3 async, plain-SQL Alembic, tests in `tests/unit/` and `tests/integration/`, every test docstring cites its requirement or rule ID, no real RUCs or phone numbers).
- New code lives in the package `src/invoicebot/channels/whatsapp/`, in `src/invoicebot/pipeline/`, `src/invoicebot/store/` and `src/invoicebot/api/webhook.py`. The import contract in `.importlinter` must keep passing: `channels`, `pipeline`, `queue`, `store` and `rules` never import `invoicebot.api`.
- HTTP calls use `httpx.AsyncClient`, always injected (never created inside a function), so tests use `httpx.MockTransport`. No test talks to Meta.
- No Pillow or other image library in this phase: image headers are parsed by hand (step 5).
- Phone numbers are never logged; log `sender_hash(number)` (step 2).
- Lint for every step: `uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src && uv run lint-imports`.

Open points for the owner (they do not block steps 1–12):

- S0.7 (Meta app, dedicated number, six approved templates) is needed only to try the bot against the real API after step 14.
- Step 11 assumes every approved template has exactly **one body variable** (`{{1}}`) that carries the message text, and language `es_PE`. If the templates were submitted with another layout, edit step 11 before running it.
- Step 1 adds a new rule row, BR-S09, for the "recibido" acknowledgement: spec 06 has no text for it today.

## Step 1: Acknowledgement rule BR-S09 `[inline: user-facing copy and a spec change]`

Files: `specs/06-business-rules.md`, `src/invoicebot/rules/catalog.py`, `src/invoicebot/rules/messages_es.py`.

- Add a row after BR-S08 in spec 06: `BR-S09 | An allowlisted sender sends a photo and it is queued (R-1.1) | Invoice created in RECEIVED; job enqueued | 📥 Recibido. Estoy leyendo tu comprobante #{id}.`
- Add `"BR-S09": Rule("BR-S09", "RECEIVED", "BR-S09")` to `RULES` and the same text to `MESSAGES`.
- Add `STATE_DESC: dict[str, str]` to `messages_es.py` for BR-S02's `{state_desc}`: `RECEIVED` "recibido", `EXTRACTING` "en lectura", `EXTRACT_FAILED` "esperando una mejor foto", `ROUTING` "en clasificación", `CONFIRMING` "esperando tu confirmación", `VALIDATING` "en validación", `CONFIRM_INVALID` "esperando tu decisión", `FETCHING_DOCS` "buscando sus archivos", `WAITING_CAPTCHA` "esperando un captcha", `MANUAL_REQUIRED` "esperando sus archivos", `PENDING_USER` "pendiente de tu respuesta", `PENDING_AUTH` "esperando una autorización", `DELIVERING` "en entrega", `BATCHED` "en el lote", `DONE` "terminado", `CANCELLED` "cancelado", `FAILED` "con error". Add `state_desc(state: str) -> str` that falls back to "en otro estado".

Test: `uv run pytest -q tests/unit/test_rules_parity.py`

## Step 2: Webhook signature, handshake and sender hash `[worker]`

Files: `src/invoicebot/channels/whatsapp/__init__.py` (docstring only), `src/invoicebot/channels/whatsapp/signature.py`, `tests/unit/test_whatsapp_signature.py`.

- `verify_signature(app_secret: str, body: bytes, header: str | None) -> bool`: `header` must be `sha256=<hex>`; compute HMAC-SHA256 of the raw body with the app secret; compare with `hmac.compare_digest`. `None`, a missing prefix, non-hex text or an empty secret return `False`; never raise.
- `verify_handshake(mode: str | None, token: str | None, expected_token: str) -> bool`: true only when `mode == "subscribe"`, `expected_token` is not empty and `hmac.compare_digest(token, expected_token)`.
- `sender_hash(sender_id: str) -> str`: first 12 hex characters of SHA-256 of the id (R-1.2, R-9.12).

Test cases (R-1.3):

1. A valid signature over `b'{"a":1}'` → `True`.
2. The same signature with one body byte changed → `False`.
3. Header `None`, `""`, `"sha1=abc"`, `"sha256=zz"` → `False` each.
4. An empty app secret → `False` even with a matching digest.
5. Handshake: (`subscribe`, right token) → `True`; wrong token, `mode=None`, `mode="unsubscribe"`, empty expected token → `False` each.
6. `sender_hash("51999000001")` is 12 lower-case hex characters, stable, and does not contain the input.

Test: `uv run pytest -q tests/unit/test_whatsapp_signature.py`

## Step 3: Parse Meta webhook payloads into canonical messages `[worker]`

Files: `src/invoicebot/channels/base.py`, `src/invoicebot/channels/whatsapp/parse.py`, `tests/unit/test_whatsapp_parse.py`.

- `base.py`: add `button_id: str | None = None` to `InboundMessage` (the id of a pressed reply button or list row). Nothing else changes.
- `parse.py`: `parse_webhook(payload: dict[str, Any], index: ProfileIndex, *, owner_profile: str, admin_profiles: frozenset[str]) -> ParsedWebhook`. `ParsedWebhook` is a frozen dataclass with `messages: list[InboundMessage]` and `unknown_senders: list[str]` (already hashed with `sender_hash`).
- Walk `entry[].changes[].value.messages[]`; ignore `value.statuses` and any change whose `field` is not `messages`. A malformed entry is skipped, never raised.
- For each message: `external_id = id`, `received_at` from the `timestamp` string (epoch seconds, UTC), `profile_id = index.resolve("whatsapp", from)`. No profile → add the hash to `unknown_senders` and skip.
- `sender_role`: `owner` when the profile is `owner_profile`, else `admin` when it is in `admin_profiles`, else `sender`.
- `reply_to_external_id` from `context.id` when present.
- Types: `text` → `text = text.body`. `image` → one `Attachment(kind="photo", media_ref=image.id, mime=image.mime_type, size=0)`, `text = image.caption or ""`. `document` → kind by mime: `image/*` → `photo`, `application/pdf` → `pdf`, `text/xml` or `application/xml` → `xml`; any other mime → a message with no attachment and `text = ""` (the pipeline answers BR-E07 in a later phase). `interactive` → `button_id = interactive.button_reply.id` or `interactive.list_reply.id`. Any other type (audio, sticker, location…) → a message with no attachment and empty text.

Test cases (R-1.2, R-1.9), with payloads written inline as dicts in the Meta shape and the synthetic numbers from `config/example.yaml`:

1. A text message → one `InboundMessage`, right `profile_id`, `text`, `received_at` in UTC.
2. An image with a caption → `kind="photo"`, `media_ref` is the media id, caption in `text`.
3. A PDF document and an XML document → `pdf` and `xml`.
4. An image sent as a document → `photo`.
5. A button reply and a list reply → `button_id` set, no attachments.
6. A quoted message → `reply_to_external_id` set.
7. A non-allowlisted number → no message, one entry in `unknown_senders`, and the raw number appears nowhere in the result.
8. A `statuses`-only payload and `{}` → empty result, no exception.
9. Owner, admin and plain sender profiles get `owner`, `admin`, `sender`.
10. Two messages in one payload → two results in payload order.

Test: `uv run pytest -q tests/unit/test_whatsapp_parse.py tests/unit/test_channels_base.py`

## Step 4: Outbound payload builders and button ids `[worker]`

Files: `src/invoicebot/channels/base.py`, `src/invoicebot/channels/whatsapp/payloads.py`, `tests/unit/test_whatsapp_payloads.py`, `tests/unit/test_channels_base.py` (only if the `buttons` type change breaks it).

- `base.py`: add `class Button(BaseModel)` (frozen, `extra="forbid"`) with `id: str` and `title: str`; change `OutboundMessage.buttons` to `list[Button]`. `grep -rn "buttons" src tests` shows no other user today.
- `payloads.py`:
  - `button_id(invoice_id: int, action: str) -> str` returns `"{invoice_id}:{action}"`; `action` must match `^[a-z_]{1,32}$`. `parse_button_id(value: str) -> tuple[int, str] | None` is its inverse and returns `None` for anything malformed.
  - `text_payload(to: str, body: str) -> dict[str, Any]`: `{"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}`; body 1–4096 characters.
  - `buttons_payload(to, body, buttons: Sequence[Button])`: `type: "interactive"`, `interactive.type: "button"`, `interactive.body.text`, `interactive.action.buttons[] = {"type": "reply", "reply": {"id", "title"}}`. 1–3 buttons, title 1–20 characters, id 1–256, unique ids, body 1–1024.
  - `list_payload(to, body, button_label: str, rows: Sequence[Button])`: `interactive.type: "list"`, one section, `action.button = button_label` (1–20), rows `{"id", "title"}` with 1–10 rows and title 1–24.
  - `template_payload(to, name: str, language: str, body_params: Sequence[str])`: `type: "template"`, `template.name`, `template.language.code`, one `body` component with text parameters (omitted when there are none).
  - Every broken limit raises `ValueError` naming the limit.

Test cases (R-3.2):

1. `text_payload` matches the dict above exactly.
2. Two buttons → the exact interactive dict.
3. Four buttons → `ValueError`; a 21-character title → `ValueError`; duplicate ids → `ValueError`; zero buttons → `ValueError`.
4. A list with 10 rows builds; 11 rows → `ValueError`; a 25-character row title → `ValueError`.
5. `template_payload("invoice_done", "es_PE", ["hola"])` → exact dict; with no params there is no `components` key.
6. `button_id(12, "confirm") == "12:confirm"`; `parse_button_id("12:confirm") == (12, "confirm")`; `"x:confirm"`, `"12"`, `"12:Bad-Action"`, `""` → `None`; `button_id(1, "Bad")` → `ValueError`.

Test: `uv run pytest -q tests/unit/test_whatsapp_payloads.py tests/unit/test_channels_base.py`

## Step 5: Media limits `[worker]`

Files: `src/invoicebot/channels/media_limits.py`, `tests/unit/test_media_limits.py`.

- `class MediaRejected(Exception)` with `reason: Literal["too_large", "too_many_pixels", "unsupported_type", "zip_entries", "zip_ratio"]`.
- `image_megapixels(head: bytes) -> float | None`: reads width and height from a PNG (`IHDR`) or JPEG (first `SOF0`–`SOF15` marker except `0xC4`, `0xC8`, `0xCC`, walking the segment lengths) header using only the bytes given; returns `None` when the bytes are neither, or the header is not complete within `head`. It never decodes pixel data and never allocates by the declared size.
- `check_image_header(head: bytes, max_megapixels: int) -> None`: raises `MediaRejected("unsupported_type")` when `image_megapixels` is `None`, `MediaRejected("too_many_pixels")` when above the limit.
- `class ByteCap`: `__init__(limit: int)`, `add(n: int) -> None` raising `MediaRejected("too_large")` as soon as the running total exceeds `limit`.
- `max_bytes_for(kind: AttachmentKind, limits: MediaLimits) -> int`: `photo` → `image`, `pdf` → `pdf`, `xml` → `xml`; other kinds raise `MediaRejected("unsupported_type")`.
- `check_zip(path: Path, max_entries: int, max_ratio: int) -> None`: with `zipfile`, reads only the central directory; more entries than `max_entries` → `zip_entries`; `sum(file_size) / max(sum(compress_size), 1) > max_ratio` → `zip_ratio`; a file that is not a zip → `unsupported_type`. Nothing is extracted.

Test cases (R-1.11, BR-E07):

1. A hand-built 33-byte PNG header declaring 8000×6000 → 48.0 MP; `check_image_header(..., 40)` raises `too_many_pixels`. The test builds only the header, never the image.
2. A PNG header declaring 2000×1500 passes.
3. A hand-built JPEG (`SOI`, an `APP0` segment, `SOF0` with 4000×3000) → 12.0 MP; the same with `SOF2` works.
4. A JPEG head cut before the SOF marker → `None`; `b"GIF89a..."` → `unsupported_type`.
5. `ByteCap(10)`: `add(6)`, `add(4)` pass; one more byte raises `too_large`.
6. `max_bytes_for` returns the three configured caps and rejects `captcha`.
7. A zip with 21 small entries and `max_entries=20` → `zip_entries`; a zip of one 1 MB run of zeros with `max_ratio=50` → `zip_ratio`; a small normal zip passes; a text file → `unsupported_type`.

Test: `uv run pytest -q tests/unit/test_media_limits.py`

## Step 6: Graph API client `[worker]`

Files: `pyproject.toml` and `uv.lock` (move `httpx` from the `dev` group to `dependencies` with `uv add "httpx>=0.28"`, then remove it from `dev`), `src/invoicebot/channels/whatsapp/client.py`, `tests/unit/test_whatsapp_client.py`.

- `class WhatsAppError(Exception)` with `code: int | None`, `status: int`; its message never contains the request URL or headers. `class WindowExpired(WhatsAppError)` for Meta error code `131047`.
- `@dataclass(frozen=True) class MediaInfo`: `url: str`, `mime: str`, `size: int`, `sha256: str | None`.
- `class WhatsAppClient.__init__(self, http: httpx.AsyncClient, *, phone_number_id: str, graph_api_version: str, token: str)`. Base URL `https://graph.facebook.com/{graph_api_version}`; every request sends `Authorization: Bearer <token>` and a 30 s timeout.
  - `async send(payload: dict[str, Any]) -> str`: POST `/{phone_number_id}/messages`; returns `messages[0].id`. A non-2xx answer raises `WindowExpired` when `error.code == 131047`, else `WhatsAppError` with Meta's `error.code` and `error.message`.
  - `async media_info(media_id: str) -> MediaInfo`: GET `/{media_id}`; maps `url`, `mime_type`, `file_size`, `sha256`.
  - `stream_media(url: str)`: an async context manager yielding an async iterator of byte chunks (GET with the bearer token, `follow_redirects=False`); a non-200 raises `WhatsAppError`. The URL host must end in `.facebook.com`, `.fbcdn.net` or `.whatsapp.net`, else `WhatsAppError` without any request.

Test cases, all with `httpx.MockTransport`:

1. `send` posts to `/v23.0/123456789/messages` with the bearer header and returns `"wamid.X"` (R-3.2).
2. A 400 with `{"error": {"code": 131047, "message": "Re-engagement"}}` → `WindowExpired` (R-1.10, PT-4).
3. A 401 with code 190 → `WhatsAppError`, `code == 190`, and `str(exc)` contains neither the token nor the URL.
4. `media_info("MID")` requests `/v23.0/MID` and maps the four fields (R-1.4).
5. `stream_media` yields the body in chunks; a 404 raises; a URL on `example.com` raises and the transport sees no request.

Test: `uv run pytest -q tests/unit/test_whatsapp_client.py && uv run pip-audit`

## Step 7: Media download `[worker]`

Files: `src/invoicebot/channels/whatsapp/media.py`, `tests/unit/test_whatsapp_media.py`.

- `@dataclass(frozen=True) class DownloadedMedia`: `path: Path`, `sha256: str`, `size: int`, `mime: str`.
- `async download_media(client: WhatsAppClient, media_id: str, kind: AttachmentKind, dest_root: Path, limits: MediaLimits) -> DownloadedMedia`:
  - `media_id` must match `^[A-Za-z0-9_-]{1,128}$` (it becomes a directory name), else `ValueError`.
  - Calls `client.media_info(media_id)` on **every** call, so a retry always gets a fresh URL (R-1.4). If `info.size` is already above `max_bytes_for(kind, limits)` it raises `MediaRejected("too_large")` before downloading.
  - Streams to `dest_root / media_id / "file"` (directory mode `0o700`), feeding SHA-256 and a `ByteCap` chunk by chunk. For `kind == "photo"` it runs `check_image_header` on the first 64 KiB before writing more.
  - On any exception the partial file and its directory are removed.

Test cases (`MockTransport`, `tmp_path`):

1. A small PNG downloads; the file exists, `sha256` and `size` match the bytes (R-1.4).
2. Calling twice issues two `media_info` requests, and the second call downloads from the second URL the mock returned (fresh URL per attempt).
3. A body longer than the cap raises `too_large` while streaming, and no file or directory is left (R-1.11).
4. `file_size` above the cap in `media_info` raises `too_large` and no download request is made.
5. A photo whose PNG header declares 48 MP raises `too_many_pixels` after the first chunk; the mock asserts the rest of the body was not read to the end (BR-E07).
6. `media_id="../x"` raises `ValueError`.

Test: `uv run pytest -q tests/unit/test_whatsapp_media.py`

## Step 8: Invoice, file and window stores `[worker]`

Files: `migrations/versions/0004_channel_windows.py`, `src/invoicebot/store/invoices.py`, `tests/integration/test_invoices_store.py`, `tests/integration/test_schema.py` (only: add `"channel_windows"` to `EXPECTED_TABLES`; spec 03 §3 lists the table).

- Migration `0004` (revises `0003`, expand only): `CREATE TABLE channel_windows (channel TEXT NOT NULL, recipient TEXT NOT NULL, last_inbound_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (channel, recipient))`. `recipient` is the profile id. Downgrade drops the table.
- `store/invoices.py`, every function taking the caller's `conn` and never committing:
  - `create_invoice(conn, profile_id: str, origin_channel: ChannelName) -> int`: inserts with `state = 'RECEIVED'`.
  - `get_state(conn, invoice_id: int) -> tuple[str, str] | None`: `(state, profile_id)`.
  - `set_state(conn, invoice_id: int, state: str, *, rule_id: str | None = None, error: str | None = None) -> None`: also sets `updated_at = now()`.
  - `count_active_jobs(conn, profile_id: str) -> int`: jobs joined to invoices of that profile whose `run_after` is not `infinity`.
  - `add_file(conn, invoice_id, record: FileRecord) -> int`, where `FileRecord` is a frozen dataclass with `kind`, `local_path: str | None`, `remote_ref: str | None`, `sha256: str`, `size: int` (keeps the call within the six-argument cap).
  - `touch_window(conn, channel: ChannelName, profile_id: str, at: datetime) -> None`: upsert keeping the greatest `last_inbound_at`.
  - `window_open(conn, channel: ChannelName, profile_id: str) -> bool`: true when `last_inbound_at > now() - interval '24 hours'`.

Test cases (real Postgres, `db_pool` fixture):

1. `create_invoice` returns an id; `get_state` gives `("RECEIVED", profile)`; an unknown id gives `None`.
2. `set_state(..., "CANCELLED", rule_id="BR-E07")` updates the three columns.
3. `count_active_jobs`: two enqueued jobs for profile A and one for B → 2 and 1; parking one of A's jobs at `infinity` → 1 (BR-S01).
4. `add_file` stores the SHA-256 and size (R-1.4).
5. `touch_window` twice with an older second timestamp keeps the newer one; `window_open` is true for 1 hour ago and false for 25 hours ago and for an unknown profile (R-1.10).
6. `alembic upgrade head`, `downgrade 0003`, `upgrade head` all succeed (reuse the pattern in `tests/integration/test_schema.py`).

Test: `uv run pytest -q tests/integration/test_invoices_store.py tests/integration/test_schema.py`

## Step 9: Intake: idempotent store, ack, job and back-pressure `[worker]`

Files: `src/invoicebot/pipeline/intake.py`, `tests/integration/test_intake.py`.

- `@dataclass(frozen=True) class IntakeResult`: `status: Literal["duplicate", "queued", "back_pressure", "stored"]`, `invoice_id: int | None`.
- `async handle_inbound(conn, msg: InboundMessage, *, max_queued_per_sender: int) -> IntakeResult`, inside **one** `conn.transaction()`:
  1. `SELECT pg_advisory_xact_lock(hashtext(%s))` with `"intake:" + msg.profile_id`, so messages of one profile are taken in arrival order (R-1.6, PT-2).
  2. `store_inbound`; not new → `duplicate`, nothing else happens (R-1.8).
  3. `touch_window(conn, msg.channel, msg.profile_id, msg.received_at)`.
  4. No `photo` attachment → `stored` (text, documents and button replies are handled by later steps and phases).
  5. `count_active_jobs >= max_queued_per_sender` → outbox message BR-S01 with `n` = that count, result `back_pressure`; the message stays stored and no invoice or job is created.
  6. Otherwise: `create_invoice`, `link_message_to_invoice`, `enqueue(conn, invoice_id, "download_media")`, outbox message BR-S09 with `id` = invoice id; result `queued`.
- Outbox rows are written with `outbox.add(conn, msg.channel, msg.profile_id, OutboundMessage(...).model_dump(mode="json"))`, where the `OutboundMessage` has `recipient = msg.profile_id`, the `rule_id`, and `text` = the formatted `MESSAGES[rule_id]`.

Test cases (real Postgres):

1. A photo → `queued`; one invoice in `RECEIVED` with the right profile and `origin_channel`; one `download_media` job; one pending outbox row whose payload has `rule_id == "BR-S09"`; the message row points at the invoice (R-1.1).
2. The same `external_id` again → `duplicate`; still one invoice, one job, one outbox row (R-1.8).
3. A text message → `stored`; no invoice, job or outbox row; the window row exists.
4. Five photos in a row → five invoices and five jobs; `claim` returns them in arrival order (R-1.6).
5. With `max_queued_per_sender=2` and two active jobs, a third photo → `back_pressure`; the message is stored, no third invoice or job, and the outbox row has `rule_id == "BR-S01"` with "2" in the text (BR-S01).
6. Back-pressure is per profile: profile B still gets `queued`.
7. If the transaction is rolled back by the caller's failure after `handle_inbound`, no message, invoice, job or outbox row remains.

Test: `uv run pytest -q tests/integration/test_intake.py`

## Step 10: Media job handler and stale-button replies `[worker]`

Files: `src/invoicebot/pipeline/handlers.py`, `src/invoicebot/pipeline/replies.py`, `tests/integration/test_pipeline_handlers.py`.

- `handlers.py`: `make_download_handler(pool, client: WhatsAppClient, data_dir: Path, limits: MediaLimits) -> Handler` for the job step `download_media`:
  - Reads the invoice's message (`messages.invoice_id = job.invoice_id`, oldest first) and takes the first attachment from `payload_json`.
  - Calls `download_media(..., dest_root=data_dir / "tmp")` with no transaction open.
  - Success: in one transaction, `add_file(conn, invoice_id, FileRecord(kind, local_path, media_ref, sha256, size))` from `store/invoices.py`. The invoice stays in `RECEIVED`; the extraction phase adds the next job.
  - `MediaRejected`: in one transaction, `set_state(invoice, "CANCELLED", rule_id="BR-E07")` and an outbox message BR-E07 with `reason_desc` (`too_large` "es demasiado grande", `too_many_pixels` "la imagen es demasiado grande", `unsupported_type` "el tipo de archivo no es compatible", `zip_entries` and `zip_ratio` "el archivo comprimido no es válido") and `max_mb` from the kind's byte cap. The handler returns normally, so the job is completed, not retried.
  - Any other exception propagates, so the queue retries with backoff.
- `replies.py`: `async resolve_reply(conn, msg: InboundMessage, valid_states: Mapping[str, frozenset[str]]) -> tuple[int, str] | None`:
  - `parse_button_id(msg.button_id)`; malformed, unknown action, unknown invoice or an invoice of another profile → `None`, nothing sent.
  - Invoice state not in `valid_states[action]` → outbox message BR-S02 with `id` and `state_desc(state)`, returns `None`, changes nothing else (R-3.5, BR-S02).
  - Otherwise returns `(invoice_id, action)`. The routing phase supplies the real `valid_states`.

Test cases (real Postgres, `MockTransport` for Meta):

1. A valid photo → one `files` row with the SHA-256, the file on disk under `tmp_path`, invoice still `RECEIVED` (R-1.4).
2. A 48 MP header → invoice `CANCELLED` with `rule_id` BR-E07, one outbox row BR-E07 containing "la imagen es demasiado grande", no `files` row, and the handler does not raise (R-1.11).
3. A Meta 500 on `media_info` → the handler raises `WhatsAppError`; no state change.
4. `resolve_reply` with `{"confirm": frozenset({"CONFIRMING"})}`: invoice in `CONFIRMING` → `(id, "confirm")`, no outbox row.
5. The same button with the invoice in `DONE` → `None` and one outbox row BR-S02 containing "terminado"; the invoice state is unchanged.
6. A button for another profile's invoice, `button_id="x"`, and an unknown action → `None`, no outbox row.

Test: `uv run pytest -q tests/integration/test_pipeline_handlers.py`

## Step 11: Outbox transport: 24 h window and templates `[worker]`

Files: `src/invoicebot/channels/whatsapp/transport.py`, `tests/integration/test_whatsapp_transport.py`.

- `TEMPLATE_SLOT_BY_RULE: dict[str, str]`: `BR-F07` → `manual_required`, `BR-S08` → `pending_user_reminder`, `BR-S04` → `admin_alert`, `BR-O01` → `admin_alert`. `template_slot(msg: OutboundMessage) -> str | None` returns `msg.template` when set, else the table entry for `msg.rule_id`, else `None`.
- `class TemplateMissing(Exception)`.
- `make_transport(client: WhatsAppClient, pool: Pool, *, numbers: Mapping[str, str], templates: ChannelTemplates, language: str = "es_PE") -> Transport` (the `Transport` type of `queue/outbox.py`). For each `OutboxRow`:
  1. `msg = OutboundMessage.model_validate(row.payload)`; `to = numbers[row.to]` (profile id → WhatsApp number; an unknown profile raises `KeyError`).
  2. `window_open` → free-form: `buttons_payload` when `msg.buttons`, else `text_payload`. Send.
  3. Window closed, or the free-form send raised `WindowExpired` → `slot = template_slot(msg)`; the template name is `getattr(templates, slot)`. No slot or an empty name → `TemplateMissing(slot or msg.rule_id or "?")`. Otherwise send `template_payload(to, name, language, [msg.text[:1024].replace("\n", " ")])` in the same call (R-1.10, PT-4, BR-S04).
- `TemplateMissing` propagates, so the outbox keeps the row and retries it with backoff.

Test cases (real Postgres for the window, `MockTransport` for Meta):

1. Window open → one request, `type == "text"`; no template sent (R-9.3: template sends = 0 on the happy path).
2. Window open with two buttons → `type == "interactive"`.
3. Window closed and `rule_id="BR-F07"` → one request, `type == "template"`, name `manual_required`, the text as the single body parameter.
4. Window open but Meta answers 131047 → two requests: the free-form one, then the template (PT-4).
5. Window closed and a rule with no slot → `TemplateMissing`, no request.
6. Window closed, slot known but its config name is `""` → `TemplateMissing`.
7. `msg.template="admin_alert"` wins over the rule table.
8. Through `OutboxSender`: case 5 leaves the row `pending` with `attempts == 1` and `last_error` starting with `TemplateMissing`.

Test: `uv run pytest -q tests/integration/test_whatsapp_transport.py`

## Step 12: Webhook routes `[worker]`

Files: `src/invoicebot/api/webhook.py`, `tests/integration/test_webhook.py`.

- `@dataclass(frozen=True) class WebhookDeps`: `pool: Pool`, `index: ProfileIndex`, `owner_profile: str`, `admin_profiles: frozenset[str]`, `app_secret: str`, `verify_token: str`, `max_queued_per_sender: int`.
- `make_router(deps: WebhookDeps) -> APIRouter`:
  - `GET /webhook/whatsapp`: query `hub.mode`, `hub.verify_token`, `hub.challenge`; `verify_handshake` → 200 with the challenge as plain text, else 403.
  - `POST /webhook/whatsapp`: read the **raw** body first; `verify_signature` with `X-Hub-Signature-256` → 401 on failure, before any JSON parsing. A body over 1 MiB → 413. Invalid JSON → 400.
  - `parse_webhook`; each unknown sender is logged once at INFO as `"ignored sender"` with only the hash (R-1.2); no reply.
  - For each message, one pooled connection and `handle_inbound`. Any database error → 503 with no body detail, so Meta retries (R-9.15). Otherwise 200 `{"status": "ok"}`.
  - Button replies and documents are stored by `handle_inbound`; acting on them belongs to the routing phase.

Test cases (FastAPI app built in the test with `make_router`, `httpx.ASGITransport`, real Postgres, bodies signed in the test with a test secret):

1. GET with the right token → 200 and the challenge; a wrong token → 403 (R-1.3).
2. POST with a bad signature → 401 and nothing stored; with no header → 401.
3. A signed photo → 200 in under 1 s; one invoice, one job, one BR-S09 outbox row (R-1.1).
4. The same signed body again → 200; still one invoice (R-1.8).
5. A signed message from a number not in the profiles → 200; nothing stored; the captured log has the hash and not the number (R-1.2).
6. A signed body that is not JSON → 400.
7. With a closed pool → 503.
8. A signed `statuses` payload → 200, nothing stored.

Test: `uv run pytest -q tests/integration/test_webhook.py`

## Step 13: Fake channel and conversation test `[worker]`

Files: `src/invoicebot/channels/fake.py`, `tests/integration/test_conversation_fake.py`.

- `class FakeChannel` implementing `Channel` with `name: ChannelName = "whatsapp"`: `send` appends the `OutboundMessage` to `sent` and returns `"fake-<n>"`; `fetch_media(media_ref)` returns a path registered with `add_media(media_ref, path)` or raises `KeyError`. Helpers: `rule_ids() -> list[str | None]`, `texts() -> list[str]`, `transport() -> Transport` (validates `row.payload` as an `OutboundMessage` and calls `send`).
- The module is imported only by tests; it has no network or database code.

Test cases (real Postgres; `OutboxSender` with `{"whatsapp": fake.transport()}`):

1. One photo through `handle_inbound`, then `send_pending()` → `fake.rule_ids() == ["BR-S09"]` and the text contains the invoice id (R-1.1).
2. Three photos with `max_queued_per_sender=2` → rule ids `["BR-S09", "BR-S09", "BR-S01"]` in that order (R-1.6, BR-S01).
3. A duplicate photo sends nothing more (R-1.8).
4. A stale button through `resolve_reply` → `["BR-S02"]` (BR-S02).
5. `fetch_media` returns the registered path and raises `KeyError` for an unknown ref.

Test: `uv run pytest -q tests/integration/test_conversation_fake.py`

## Step 14: Runtime wiring `[inline: startup design across settings, secrets, worker and sender]`

Files: `src/invoicebot/settings.py`, `src/invoicebot/app.py`, `tests/unit/test_health.py`, `deploy/docker-compose.yml`, `deploy/README.md`.

- `settings.py` (pydantic-settings): `DATABASE_URL`, `CONFIG_PATH` (default `/app/runtime-config/config.yaml`), `SECRETS_PATH`, `AGE_IDENTITY_PATH`, `DATA_DIR` (default `/data`). None of these names ends in a secret suffix.
- Lifespan, only when `CONFIG_PATH` and `SECRETS_PATH` exist (otherwise the app starts as today, with `/health` only, and logs one warning):
  - `assert_no_plain_secrets(os.environ)`, load the config and the secret store, `configure_logging(store.values())`.
  - Build the `WhatsAppClient` from a scoped `Secrets` view (`whatsapp_token`, `whatsapp_app_secret`, `whatsapp_verify_token`); if the view is not `enabled`, the webhook router is not mounted and one error is logged (fail closed).
  - Mount `make_router(...)`; start one `Worker` with the `download_media` handler and one loop that calls `OutboxSender.send_pending()` every 2 s; both tasks are cancelled and awaited on shutdown, and a task that dies is logged and restarted after 5 s.
- Compose: a `/data` volume for the app, and `CONFIG_PATH` set to the mounted config.
- `deploy/README.md`: how to point Meta at the webhook through a Cloudflare quick tunnel for local trials (D31), and which three secrets the channel needs.
- Check: `/health` still answers with no config present; with the example config and a test secrets file, `GET /webhook/whatsapp` answers the handshake.

Test: `uv run pytest -q tests/unit/test_health.py && bash tests/deploy/test_deploy.sh`
