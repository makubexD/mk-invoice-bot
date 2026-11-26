# Plan: MVP phase 9z · open-links

Roadmap: `docs/plan-mvp.md`, row 9z. After filing, the chat links to where the files went, a flow's mail can wait for
the owner's [Enviar], and the dry_run `.eml` reaches the chat as a document.

What the code does today: `OutboundMessage` (`channels/base.py`) carries text and reply buttons only; no channel sends
a document. Flow B's BR-D07, BR-D10 and BR-D13 name the folder but give no link; `file_folder` never reads the folder's
`webUrl`. Flow A's BR-D16 and BR-D17 put the row and folder links in the text (`link_row`, `link_folder`), and BR-D18
puts the row link there. `deliver` hands the mail to the flow's first mode at once. A dry_run draft is written to
`<data_dir>/mail/<id>.eml` and stays on the host. Live invoice #34 is `BATCHED` with a `prepared` dry_run batch.

Out of scope: anonymous share links; sending a mail the owner edited in the chat (9y); a review step for flow A, which
sends no mail; a reminder for a mail that waits (see D194).

Needs from the owner: review of D194, above all its **Owner decides** items; Telegram for step 19.

Decision this plan proposes (step 1), **D194 open links and review before send**:

- **Links.** A chat message may carry links (`title`, `https` URL). Telegram shows each as a URL button under the
  text, WhatsApp and the console as one line each after the text: `📂 Abrir carpeta: <url>`. Flow B's link is the
  OneDrive folder's `webUrl`, read once after filing and kept in `flow_data.folder_url`. Flow A's row and folder links
  move out of the text into links (`📄 Abrir fila`, `📂 Abrir carpeta`). The bot never creates a share link. A URL that
  is not `https` is dropped and logged.
- **Documents.** A chat message may instead carry one document: a file under the data directory, its file name and
  MIME type, with the text as its caption. One outbox row per document, so each retries alone. Caps: Telegram 50 MB
  (`sendDocument`), WhatsApp 100 MB (media upload, then a document message). A file over the cap, or gone, fails the
  row at once. Outside WhatsApp's 24-hour window a document cannot be sent, so its row fails at once and the text rows
  still go out by template.
- **Review before send.** The mailer sink gets `send: review | auto` (default `auto`, today's behavior). With
  `review`, `deliver` files the folder and the Excel as today, writes each mail attachment to
  `<data_dir>/mail/<id>/`, opens the batch as `review`, moves the invoice to `BATCHED` with BR-D19 and queues, in one
  transaction: each attachment as a document, then BR-D19 (To, Cc, subject, body, attachment names, the folder link)
  with [Enviar] and [Cancelar]. Nothing is sent.
  - [Enviar] (batch `review`): records `reviewed` in the flow data, puts the invoice back in `DELIVERING`, enqueues
    `deliver` and answers BR-D21. `deliver` reuses the saved folder and sequence (D88) and runs the flow's first mode
    as today, so Graph confirms with BR-D10 and dry_run answers BR-D07.
  - [Cancelar] (batch `review`): holds the mail. The batch and invoice stay as they are; BR-D20 answers with [Enviar]
    again. It is not `/cancelar`: the invoice stays filed.
  - Any other batch state gets BR-S02.
  - The review files are deleted after the send commits, next to the photos, and when the invoice is cancelled.
- **A mail that waits.** A `review` batch has no laptop job, so the BR-D11 sweep (`laptop.check_overdue`) never sees
  it and `confirm_timeout` does not apply: the mail waits for the owner as long as it takes. `/estado` shows it.
- **The dry_run draft.** BR-D07 is preceded by the `.eml` as a document, from the first draft and from BR-D11's [Lo
  envío yo] (`resend_eml`).
- **A batch prepared before review existed.** `/revisar <n>` (admins) takes a `BATCHED` invoice whose batch is
  `prepared` with no laptop job and whose flow is on `review` back through `deliver`, which then posts the review.
  Anything else gets BR-D22. This is how live #34 is sent.
- **Live.** Flow B goes to `send: review` while the trial proves the Graph send.

**Owner decides** (the plan proposes the first choice in each):

1. A waiting review has no reminder and no timeout.
2. [Cancelar] holds the mail and keeps the invoice filed; `/cancelar` still voids an invoice before `DELIVERING`
   only.
3. A WhatsApp document outside the window fails rather than waiting for the window to reopen (WhatsApp is off in
   the live config today).
4. The attachments are posted before BR-D19, so its buttons come last in the chat.

## Step 1: D194, spec texts and messages `[inline: user-facing copy and the decision record]`

Files: `specs/05-open-questions.md`, `specs/06-business-rules.md`, `specs/02-requirements.md`, `specs/03-design.md`,
`src/invoicebot/rules/messages_es.py`.

- D194 in `05` (the text above); `02` R-7.5 to R-7.8 (review before send, links, documents), R-1.9 (the canonical
  message carries links and one document); `03` the canonical message, the `review` batch state, the `BATCHED` →
  `DELIVERING` move on [Enviar], `/revisar`; `06` rows BR-D19 to BR-D22 and BR-D16 to BR-D18 without the link lines.
- Messages (rule in brackets):
  - `BR-D19`:
    `📧 Revisa el correo de {serie}-{numero} antes de enviarlo.\nPara: {to}\n{cc_line}Asunto: {subject}\n\n{body}\n\nAdjuntos: {files}`
    with `{cc_line}` = `CC: {cc}\n` or empty.
  - `BR-D20`: `⏸️ No envié el correo de {serie}-{numero}. Toca [Enviar] cuando quieras.`
  - `BR-D21`: `📤 Enviando el correo de {serie}-{numero}…`
  - `BR-D22`: `No hay un correo por revisar para #{id}.`
  - `BR-D16`, `BR-D17`, `BR-D18`: drop the `Fila:` and `Carpeta:` lines.
  - Button and link titles: `BUTTON_SEND = "Enviar"`, `BUTTON_HOLD = "Cancelar"`, `LINK_FOLDER = "📂 Abrir carpeta"`,
    `LINK_ROW = "📄 Abrir fila"`.
  - `/revisar` in the command help: `Muestra el correo preparado de una factura para enviarlo.`

Test: `uv run pytest -q tests/unit/test_rules_parity.py tests/unit/test_messages_es.py tests/unit/test_docs.py`

## Step 2: Links and documents in the canonical message `[worker]`

Files: `src/invoicebot/channels/base.py`, `src/invoicebot/pipeline/outgoing.py`, `tests/unit/test_channels_base.py`,
`tests/integration/test_messages_store.py`.

- `Link(BaseModel, frozen, extra=forbid)`: `title: str`, `url: str`; a validator refuses a URL whose scheme is not
  `https` or that has no host.
- `Document(BaseModel, frozen, extra=forbid)`: `path: str` (absolute), `filename: str` (no `/`, no `\`, not empty, at
  most 255 chars), `mime: str`, `size: int` (≥ 0).
- `OutboundMessage` gains `links: list[Link] = []` and `document: Document | None = None`.
- `queue_message` gains a keyword `links: Sequence[Link] = ()`. New `queue_document(conn, to, document, *, caption,
invoice_id, rule_id)` writes one row with `text=caption` and the document.
- `safe_links(pairs: Sequence[tuple[str, str]]) -> list[Link]` in `outgoing.py`: builds the links, drops (and logs at
  WARNING with the title only) a pair whose URL the validator refuses.

Test cases (docstrings start with `D194`):

- `Link(title="x", url="https://onedrive.live.com/?id=1")` is accepted; `http://…`, `javascript:alert(1)` and
  `https:///x` are refused.
- `Document` refuses `filename="a/b.pdf"`, `""` and a negative size.
- An `OutboundMessage` with links and a document survives `model_dump(mode="json")` and `model_validate`.
- `queue_message(..., links=[…])` stores the links in `payload_json`; `queue_document` stores the document and the
  caption as `text`.
- `safe_links([("a", "https://x.example/1"), ("b", "ftp://x")])` returns only `a`.

Test: `uv run pytest -q tests/unit/test_channels_base.py tests/integration/test_messages_store.py`

## Step 3: Telegram shows links as URL buttons `[worker]`

Files: `src/invoicebot/channels/telegram/transport.py`, `tests/unit/test_telegram_transport.py`.

- `_payload` adds one keyboard row per link, `{"text": link.title, "url": link.url}`, after the reply-button rows. A
  message with links and no buttons still gets `reply_markup`.

Test cases (docstrings start with `D194`):

- One button and one link → two rows: the callback row, then the URL row.
- Only a link → `reply_markup.inline_keyboard == [[{"text": "📂 Abrir carpeta", "url": "https://…"}]]`.
- No links and no buttons → no `reply_markup`, as today.

Test: `uv run pytest -q tests/unit/test_telegram_transport.py`

## Step 4: Telegram sends documents `[worker]`

Files: `src/invoicebot/channels/telegram/client.py`, `src/invoicebot/channels/telegram/transport.py`,
`tests/unit/test_telegram_client.py`, `tests/unit/test_telegram_transport.py`.

- `TelegramClient.send_document(chat_id, data: bytes, filename, mime, caption, reply_markup | None) -> tuple[int,
int]`: multipart `sendDocument` (fields `chat_id`, `caption`, `reply_markup` as JSON, file part `document`), same
  timeout and error mapping as `call`, same result check as `send_message`.
- The transport sends a row with a document through `send_document`: it refuses (`PermanentSendError`) a file over
  `DOCUMENT_MAX_BYTES = 50 * 1024 * 1024`, a missing file, and a caption over 1024 UTF-16 code units; it reads the file
  with `asyncio.to_thread`. Links and buttons go in the same `reply_markup` as a text row.

Test cases (docstrings start with `D194`), with `httpx.MockTransport`:

- The request is `multipart/form-data` to `.../sendDocument`, carries the file name, bytes and caption.
- A 51 MB file (size from `stat`, sparse file) and a missing path end as `PermanentSendError`; nothing is posted.
- A 429 with `retry_after` maps to `SendLater`, as for text.
- A text row still goes through `sendMessage`.

Test: `uv run pytest -q tests/unit/test_telegram_client.py tests/unit/test_telegram_transport.py`

## Step 5: WhatsApp shows links as text lines `[worker]`

Files: `src/invoicebot/channels/whatsapp/transport.py`, `tests/integration/test_whatsapp_transport.py`.

- `_with_links(msg) -> str`: the text, then one line per link, `f"{link.title}: {link.url}"`. Both the in-window
  payload and the template parameter use it.

Test cases (docstrings start with `D194`):

- In the window, a message with one link sends text ending in `\n📂 Abrir carpeta: https://…`.
- Out of the window, the template parameter carries the same line.
- No links → the text is unchanged.

Test: `uv run pytest -q tests/integration/test_whatsapp_transport.py`

## Step 6: WhatsApp media upload and document payload `[worker]`

Files: `src/invoicebot/channels/whatsapp/client.py`, `src/invoicebot/channels/whatsapp/payloads.py`,
`tests/unit/test_whatsapp_client.py`, `tests/unit/test_whatsapp_payloads.py`.

- `WhatsAppClient.upload_media(data: bytes, filename, mime) -> str`: multipart `POST /{phone_number_id}/media` with
  `messaging_product=whatsapp`, `type=<mime>` and the file; returns the media `id` or raises `WhatsAppError` with the
  status when the answer has none. Same timeout and error mapping as `send`.
- `document_payload(to, media_id, filename, caption) -> dict`: `type: document`, `document: {id, filename,
caption}`; caption checked at 1024 chars with `_check_chars`.

Test cases (docstrings start with `D194`), with `httpx.MockTransport`:

- `upload_media` posts multipart with the three fields and returns `"m1"` from `{"id": "m1"}`.
- An answer without `id` raises `WhatsAppError`; a 400 maps as `send` does.
- `document_payload` builds the shape above; a 1025-char caption raises `ValueError`.

Test: `uv run pytest -q tests/unit/test_whatsapp_client.py tests/unit/test_whatsapp_payloads.py`

## Step 7: WhatsApp sends documents `[worker]`

Files: `src/invoicebot/channels/whatsapp/transport.py`, `tests/integration/test_whatsapp_transport.py`.

- A row with a document: out of the window → `PermanentSendError("document outside the 24-hour window")`; over
  `DOCUMENT_MAX_BYTES = 100 * 1024 * 1024` or missing → `PermanentSendError`; else `upload_media`, then
  `document_payload` with the text plus its link lines as caption. The token and permanent-code handling of `_send`
  covers both calls.

Test cases (docstrings start with `D194`):

- In the window: one upload, then one document message with the returned id; the row's message id is Meta's.
- Out of the window: nothing is posted, the row is dead.
- A missing file: nothing is posted, the row is dead.

Test: `uv run pytest -q tests/integration/test_whatsapp_transport.py`

## Step 8: The console shows links and documents `[worker]`

Files: `src/invoicebot/rehearse.py`, `tests/unit/test_channels_console.py`.

- `ConsoleReply` gains `links: list[tuple[str, str]]` and `document: str | None` (the file name); the printed reply
  shows each link as `title: url` and a document as `📎 <filename>`, after the text.

Test cases (docstrings start with `D194`):

- A row with a link prints `📂 Abrir carpeta: https://…`.
- A row with a document prints `📎 factura.xlsx` and its caption.

Test: `uv run pytest -q tests/unit/test_channels_console.py`

## Step 9: A port that reads a folder's link `[worker]`

Files: `src/invoicebot/sinks/base.py`, `src/invoicebot/sinks/onedrive.py`, `tests/unit/test_sinks_onedrive.py`.

- `FolderLinks(Protocol)`: `async def web_url(self, path: str) -> str`, documented: returns the item's browser link
  as Graph gives it, never a share link; raises `FolderMissing` when the item is gone.
- `GraphDrive.web_url`: `GET /me/drive/root:{encoded path}?$select=webUrl`, the same token, timeout and
  `raise_graph_error` handling as `move`; refuses an answer whose `webUrl` is missing or not `https` with
  `GraphError`.

Test cases (docstrings start with `D194`), with `httpx.MockTransport`:

- Returns `https://onedrive.live.com/?id=X` from `{"webUrl": …}`; the request path is encoded as `_encode_path` does.
- 404 → `FolderMissing`; `{"webUrl": "http://x"}` and `{}` → `GraphError`; 503 → `GraphTransient`.

Test: `uv run pytest -q tests/unit/test_sinks_onedrive.py`

## Step 10: Flow B's messages carry the folder link `[worker]`

Files: `src/invoicebot/pipeline/deliver.py`, `src/invoicebot/app.py`, `tests/integration/test_pipeline_deliver.py`.

- `DeliverContext` gains `links: FolderLinks` (the same `GraphDrive` in `app.py`).
- `_prepare`: after `file_folder`, the folder URL is `flow_data.folder_url` when saved, else `links.web_url(path)`,
  saved in the same `merge_flow_data` as `folder`; `_Delivery` gains `folder_url`.
- BR-D07, BR-D10 and BR-D13 are queued with `links=safe_links([(LINK_FOLDER, folder_url)])`.

Test cases (docstrings start with `D194`), with the fake drive gaining `web_url`:

- A graph delivery queues BR-D10 with one link to the fake URL, and `flow_data.folder_url` holds it.
- A retried delivery reuses the saved URL: `web_url` is called once in total.
- A dry_run delivery's BR-D07 has the link; an inbox_rule's BR-D13 has it next to [No llegó].
- `web_url` raising `GraphTransient` leaves the invoice `DELIVERING` for the retry, as a filing error does.

Test: `uv run pytest -q tests/integration/test_pipeline_deliver.py`

## Step 11: Flow A's links move out of the text `[worker]`

Files: `src/invoicebot/pipeline/deliver_google.py`, `tests/integration/test_pipeline_deliver_google.py`.

- `_complete` formats BR-D16, BR-D17 and BR-D18 without `link_row` and `link_folder` and queues
  `links=safe_links([(LINK_ROW, link_row), (LINK_FOLDER, link_folder)])`, skipping an empty one (BR-D18 has no
  folder).

Test cases (docstrings start with `D194`):

- BR-D16 has both links, in that order, and its text has no `Fila:` or `Carpeta:` line.
- BR-D18 has only the row link.

Test: `uv run pytest -q tests/integration/test_pipeline_deliver_google.py`

## Step 12: The `send` setting and the `review` batch state `[worker]`

Files: `src/invoicebot/config/models.py`, `migrations/versions/0018_review_batch.py`,
`tests/unit/test_config_models.py`, `tests/integration/test_schema.py`.

- `MailerSink.send: Literal["review", "auto"] = "auto"`.
- Migration 0018 (plain SQL, expand only): drop and re-add `batches_state_check` with `'review'` added; downgrade
  restores 0005's list `NOT VALID`, as 0005 does.

Test cases (docstrings start with `D194`):

- A mailer without `send` loads as `auto`; `send: review` loads; `send: later` is refused.
- The `batches` check accepts `review` and still refuses `paused`.

Test: `uv run pytest -q tests/unit/test_config_models.py tests/integration/test_schema.py`

## Step 13: `deliver` posts the mail for review `[worker:high: reasoning: the review branch has to fit D88 and D89's crash rules: the batch states a retry finds, the reviewed marker and the order of the transaction against the external calls]`

Files: `src/invoicebot/pipeline/review.py` (new), `src/invoicebot/pipeline/deliver.py`,
`tests/integration/test_pipeline_review.py` (new).

- `review.py` holds the branch, so `deliver.py` only dispatches to it: `post_review(context, delivery, mailer)`.
  - Writes each mail attachment to `<data_dir>/mail/<invoice_id>/<filename>` (`asyncio.to_thread`, replacing what is
    there, paths checked with `is_relative_to`).
  - One transaction: `_still_delivering` or return; `open_batch(..., "review")` then `set_batch_state(..., "review")`
    (an earlier `prepared` or `failed` batch is reused); `set_state(BATCHED, rule_id="BR-D19")`; one
    `queue_document` per attachment (caption = the file name); then BR-D19 with [Enviar] and [Cancelar]
    (`button_id(id, SEND_ACTION)`, `HOLD_ACTION`) and the folder link; `_close_fixed_gap`.
- `deliver.py`, after `_prepare`: when `sinks.mailer.send == "review"` and `flow_data.reviewed` is not true, call
  `post_review` and stop; else the mode runs as today. A batch already `sending` still goes to `_crashed_send` first
  (D89).

Test cases (docstrings start with `D194`), with real Postgres and the fake drive and modes:

- A `review` flow: the invoice is `BATCHED` with BR-D19; the batch is `review`; the outbox has the Excel, PDF and XML
  documents, then BR-D19 with To, subject, body, the attachment names, two buttons and the folder link; the files
  exist under `mail/<id>/`; the graph mode was not called.
- A retry of the same job (state already `BATCHED`) queues nothing more.
- `reviewed: true` in the flow data: the graph mode is called once and BR-D10 is queued, as for `auto`.
- An `auto` flow behaves as before (BR-D10, no documents).
- A batch left `sending` with `reviewed: true` goes to BR-D03, never sends (D89).

Test: `uv run pytest -q tests/integration/test_pipeline_review.py tests/integration/test_pipeline_deliver.py`

## Step 14: [Enviar] and [Cancelar] `[worker]`

Files: `src/invoicebot/pipeline/actions.py`, `tests/integration/test_pipeline_actions.py`.

- `SEND_ACTION = "send"`, `HOLD_ACTION = "hold"`, both valid in `BATCHED` only, both registered in `ACTIONS` and
  `VALID_STATES`.
- `_send`: batch not `review` → BR-S02. Else `merge_flow_data(reviewed=True)`, `set_state(DELIVERING)`,
  `enqueue(DELIVER_STEP)`, reply BR-D21.
- `_hold`: batch not `review` → BR-S02. Else reply BR-D20 with [Enviar]; nothing else changes.

Test cases (docstrings start with `D194`):

- [Enviar] on a `review` batch: the invoice is `DELIVERING`, a `deliver` job is queued, `reviewed` is true, BR-D21
  is queued.
- A second [Enviar] (invoice now `DELIVERING`) gets BR-S02 and queues no second job.
- [Cancelar] on a `review` batch: BR-D20 with one [Enviar] button; state and batch unchanged.
- [Enviar] on a `prepared` batch gets BR-S02.

Test: `uv run pytest -q tests/integration/test_pipeline_actions.py`

## Step 15: The review files are removed `[worker]`

Files: `src/invoicebot/pipeline/review.py`, `src/invoicebot/pipeline/deliver.py`, `src/invoicebot/pipeline/actions.py`,
`tests/integration/test_pipeline_review.py`.

- `remove_review_files(data_dir, invoice_id)` in `review.py`: deletes `<data_dir>/mail/<invoice_id>/` and its files,
  an absent directory accepted. Called after the commit wherever the photos are deleted (`_automatic`, `_sent`) and
  after `cancel_invoice`'s commit. `# shortcut:` a document row still pending then fails as missing; the owner has
  pressed [Enviar] after seeing it.

Test cases (docstrings start with `D194`):

- After a reviewed graph send, `mail/<id>/` is gone and the photo is gone.
- After [Ya lo envié] on a dry_run batch, `mail/<id>/` is gone.
- An absent directory raises nothing.

Test: `uv run pytest -q tests/integration/test_pipeline_review.py`

## Step 16: The dry_run draft reaches the chat `[worker]`

Files: `src/invoicebot/pipeline/deliver.py`, `tests/integration/test_pipeline_deliver.py`.

- `_assisted` and `_resend_eml` queue the `.eml` as a document (`filename = f"{serie}-{numero}.eml"`, mime
  `message/rfc822`, caption the file name) in the same transaction, before BR-D07.

Test cases (docstrings start with `D194`):

- A dry_run delivery queues the `.eml` document, then BR-D07 with [Ya lo envié]; the document's path is the written
  draft.
- [Lo envío yo] (`resend_eml`) queues the document again, then BR-D07.

Test: `uv run pytest -q tests/integration/test_pipeline_deliver.py`

## Step 17: `/revisar <n>` `[worker]`

Files: `src/invoicebot/pipeline/commands.py`, `tests/integration/test_commands.py`.

- Admin only, registered in `COMMANDS`. Takes the invoice id. When the invoice is `BATCHED`, its batch `prepared`,
  it has no `mail_jobs` row and its flow's mailer is `send: review`: `set_state(DELIVERING)`, `enqueue(DELIVER_STEP)`;
  the review then comes from step 13. Anything else, a missing or bad id included, answers BR-D22.

Test cases (docstrings start with `D194`):

- A dry_run `prepared` batch on a `review` flow: the invoice is `DELIVERING` and one `deliver` job is queued.
- A batch with a laptop job, a `sent` batch, an `auto` flow, an unknown id and `/revisar abc` → BR-D22, nothing
  queued.
- A sender (not admin) does not get the command.

Test: `uv run pytest -q tests/integration/test_commands.py`

## Step 18: Docs `[inline: user-facing docs]`

Files: `docs/architecture.md`, `docs/runbook.md`, `README.md`, `config/example.yaml`.

- The state diagram's `BATCHED` → `DELIVERING` on [Enviar]; flow B's sequence with the review; the runbook's
  "switch a flow between `review` and `auto`" and `/revisar`; `example.yaml`'s flow B mailer gets `send: review` with a
  comment naming D194.

Test: `uv run pytest -q tests/unit/test_docs.py tests/unit/test_config_models.py`

## Step 19: Live check `[inline: live run on the owner's Telegram and OneDrive]`

Files: `docs/plan-mvp.md` (findings only), the ignored `config/config.yaml`.

- Flow B to `send: review`, deploy, `/revisar 34`: the Excel, PDF and XML arrive as documents, BR-D19 shows the mail,
  [📂 Abrir carpeta] opens the folder in the OneDrive app on the phone and in the browser.
- [Cancelar]: BR-D20; then [Enviar]: BR-D10 and the mail arrives (owner checks the work inbox).
- After `folder_status` renames the folder, the saved link still opens it (`rclone lsf onedrive:` confirms the new
  name). If it does not, record it as a finding: the link must then be read again after the rename.
- A dry_run run in the sandbox: the `.eml` arrives as a document and opens in the phone's mail app.

Test: the live run above, recorded in the roadmap row.
