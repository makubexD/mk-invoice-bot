# Plan: MVP phase 9z2 · review-ui

Roadmap: `docs/plan-mvp.md`, row 9z2. The owner's UI asks from the `open-links` live run
(`specs/spikes/open-links.md`), plus two leftovers of its finish review in the same code.

What the code does today: a Telegram button press is stored and acknowledged with `answerCallbackQuery`
(`api/telegram_webhook.py`), and the pressed message keeps its keyboard, so BR-D19 still shows [Enviar] [Cancelar] after
a press and BR-D20 adds a second [Enviar]. BR-D10 reads `✅ Archivada en {folder} y enviada por correo.` from three
places (`pipeline/deliver.py` for Graph, `pipeline/laptop.py` for a laptop receipt, `pipeline/actions.py` for [Ya
llegó]); it names neither the invoice nor the recipient, and says "enviada" when Graph only accepted the mail.
`deliver` deletes `mail/<id>/` right after the send even while a review document row waits on a 429, and nothing
deletes a review directory the send never reaches.

Out of scope: WhatsApp keyboards (WhatsApp cannot edit a sent message; a stale press already gets BR-S02); bounce
notices (9x); BR-D19's WhatsApp template slot and body cap (9d2); recipient names, which the config does not hold.

Needs from the owner: review of D195, above all its **Owner decides** items; Telegram for step 9.

Decision this plan proposes (step 1), **D195 settled buttons and BR-D10's text**:

- **Settled buttons (Telegram).** Once a press is stored and acknowledged, the webhook edits the pressed message's
  keyboard (`editMessageReplyMarkup`): the callback buttons go, URL buttons ([📂 Abrir carpeta]) stay. Every press
  settles its message: no message in the bot expects a second press on the same keyboard; the answer to a press is a
  new message with its own buttons (BR-D20's [Enviar]). Like the acknowledgement (D177), the edit is not a message, so
  it bypasses the outbox, and a failure is logged and ignored. The keyboard is read from the callback's own `message`,
  so nothing is stored. WhatsApp keeps its buttons; a stale press there gets BR-S02 as today.
- **BR-D10's text.** `✅ {serie}-{numero} archivada en {folder}. Outlook aceptó el correo para {to}.` where `{to}` is the
  mail's To addresses, comma-separated. The addresses are saved in the flow data (`mail_to`) when `deliver` builds the
  mail, so the laptop receipt and [Ya llegó] read the same ones. One home for the text: `pipeline/done.py`. A batch
  built before this release has no `mail_to` and reads `para el destinatario`. **Owner decides:** the wording; whether
  CC is named too (proposal: no, To only).
- **Review files.** `deliver` removes `mail/<id>/` after the send only when no pending outbox row of that invoice
  carries a document. The daily housekeeping removes each `mail/<id>/` directory whose invoice is terminal or unknown
  and has no pending document row, the same way it removes `tmp/<id>/` (R-9.7). A held mail's invoice is `BATCHED`,
  so its files stay while it waits. `mail/<id>.eml` drafts are files, not directories, and are left alone.

## Step 1: D195, BR-D10's text and the roadmap row `[inline: spec text and an owner decision]`

Files: `specs/05-open-questions.md`, `specs/06-business-rules.md`, `docs/plan-mvp.md`.

- `specs/05-open-questions.md`: add row D195 after D194 with the decision above (settled buttons, BR-D10's text,
  review files), marked "(9z2 plan, proposed 2026-11-26)".
- `specs/06-business-rules.md`: BR-D10's message becomes
  `✅ {serie}-{numero} archivada en {folder}. Outlook aceptó el correo para {to}.`; its condition cell adds "(D195)".
- `docs/plan-mvp.md`: add row 9z2 `review-ui` after 9z (plan `docs/plan-mvp-review-ui.md`, D195), and mark the first
  two bullets and the 429 and `mail/` retention bullets of "Carried over from the `open-links` live check" as taken by
  9z2.

Test: `uv run pytest -q tests/unit/test_messages_es.py` still passes (the code text changes in step 4).

## Step 2: the settled keyboard of a press `[worker]`

Files: `src/invoicebot/channels/telegram/parse.py`, `tests/unit/test_telegram_parse.py`.

- New frozen dataclass `SettledKeyboard(chat_id: int, message_id: int, url_rows: list[list[dict[str, str]]])`:
  the pressed message and the URL buttons to keep (D195).
- `ParsedUpdate` gains `settled: list[SettledKeyboard]`; every existing constructor passes `[]` except the callback
  path, which passes one entry for the accepted press (the same chat id and message id it already reads).
- `url_rows` is read from `callback["message"]["reply_markup"]["inline_keyboard"]`: for each row, keep each button
  that is a dict with a `text` str and a `url` str that `Link(title=text, url=url)` accepts, as
  `{"text": text, "url": url}`; drop rows left empty. A missing or malformed `reply_markup` gives `[]`. Never raise.
- A press from an unknown sender or a malformed callback gives `settled=[]`, as it gives no messages today.

Cases (each docstring starts with `D195:`):

1. A press on a message with `[[{text: Enviar, callback_data: x}, {text: Cancelar, callback_data: y}],
[{text: 📂 Abrir carpeta, url: https://example.com/f}]]` gives `settled == [SettledKeyboard(chat, msg,
[[{"text": "📂 Abrir carpeta", "url": "https://example.com/f"}]])]`.
2. A press on a message with only callback buttons gives `url_rows == []`.
3. A press whose message has no `reply_markup` gives one entry with `url_rows == []`.
4. A URL button with `url: "http://example.com"` (not https) is dropped.
5. A `reply_markup` that is a string, or an `inline_keyboard` row that is not a list, gives `url_rows == []`.
6. An unknown sender's press gives `settled == []`.
7. A text message gives `settled == []`.

Test: `uv run pytest -q tests/unit/test_telegram_parse.py`

## Step 3: edit the pressed keyboard after the acknowledgement `[worker]`

Files: `src/invoicebot/channels/telegram/client.py`, `src/invoicebot/api/telegram_webhook.py`,
`tests/unit/test_telegram_client.py`, `tests/integration/test_telegram_webhook.py`.

- `TelegramClient.edit_reply_markup(chat_id: int, message_id: int, inline_keyboard: list[list[dict[str, str]]]) ->
None`: calls `editMessageReplyMarkup` with `{"chat_id", "message_id", "reply_markup": {"inline_keyboard": ...}}`.
- The webhook, after the acknowledgements and only on a 200 intake, calls `_settle_keyboard(client, settled)` for each
  entry of `parsed.settled`: same timeout (`ANSWER_TIMEOUT_SECONDS`) and the same failure handling as
  `_answer_callback_query` (`TelegramError`, `TimeoutError` logged as a warning with `error_type`, then ignored). The
  module docstring names D195.

Cases (docstrings start with `D195:`):

1. Client: `edit_reply_markup(1, 2, [])` posts to `.../editMessageReplyMarkup` with
   `{"chat_id": 1, "message_id": 2, "reply_markup": {"inline_keyboard": []}}` (`httpx.MockTransport`).
2. Webhook: a stored press calls `answerCallbackQuery`, then `editMessageReplyMarkup` with the kept URL row.
3. Webhook: a press whose intake does not answer 200 calls neither.
4. Webhook: `editMessageReplyMarkup` answering 400 still returns 200 and logs one warning.
5. Webhook: a text message calls no `editMessageReplyMarkup`.

Test: `uv run pytest -q tests/unit/test_telegram_client.py tests/integration/test_telegram_webhook.py`

## Step 4: BR-D10's text and its one home `[worker]`

Files: `src/invoicebot/rules/messages_es.py`, `src/invoicebot/pipeline/done.py` (new),
`tests/integration/test_pipeline_done.py` (new), `tests/unit/test_messages_es.py`.

- `messages_es.py`: `"BR-D10": "✅ {serie}-{numero} archivada en {folder}. Outlook aceptó el correo para {to}."` and a
  new constant `DONE_RECIPIENT_UNKNOWN = "el destinatario"` next to the other message fragments (D195).
- `pipeline/done.py`, module docstring "BR-D10's text for a filed and sent invoice (D195).":
  - `MAIL_TO_KEY = "mail_to"`: the flow-data key that holds the mail's To addresses.
  - `async def done_text(conn, invoice_id: int) -> str`: loads the flow data (`load_flow_data`) and the reading
    (`load_reading`, as `laptop._notify_stale` does); renders BR-D10 with `serie`, `numero`, `folder` and `to` =
    `", ".join(mail_to)` when `mail_to` is a non-empty list of str, else `DONE_RECIPIENT_UNKNOWN`. Raises
    `RuntimeError` when the invoice has no reading, like the laptop code.

Cases (docstrings start with `BR-D10, D195:`):

1. `mail_to = ["a@example.com", "b@example.com"]`, folder `F`, reading `F001`/`123` gives
   `✅ F001-123 archivada en F. Outlook aceptó el correo para a@example.com, b@example.com.`
2. No `mail_to` gives `... para el destinatario.`
3. `mail_to = []` gives `... para el destinatario.`
4. An invoice with no reading raises `RuntimeError`.
5. `test_messages_es.py`: BR-D10 formats with exactly `serie`, `numero`, `folder` and `to` (update the existing
   placeholder case if there is one).

Test: `uv run pytest -q tests/integration/test_pipeline_done.py tests/unit/test_messages_es.py`

## Step 5: `deliver` saves `mail_to` and uses `done_text` `[worker]`

Files: `src/invoicebot/pipeline/deliver.py`, `tests/integration/test_pipeline_deliver.py`.

- Right after `build_mail` in the filing path, merge `{MAIL_TO_KEY: list(mail.to)}` into the flow data, in its own
  short transaction like the folder merge above it.
- The Graph completion (`_Reply(mode.done_rule, MESSAGES[mode.done_rule].format(folder=...), ...)`) uses
  `await done_text(conn, delivery.invoice_id)` when `mode.done_rule == "BR-D10"`; every other done rule keeps its
  current text.
- Update the existing BR-D10 assertions in the test file to the new text.

Cases (docstrings start with `BR-D10, D195:`):

1. A Graph send ends `DONE` with BR-D10 naming the serie-numero and the sink's To addresses.
2. After a delivery, `flow_data["mail_to"]` equals the sink's `to`.
3. An `inbox_rule` delivery still answers BR-D13's unchanged text.

Test: `uv run pytest -q tests/integration/test_pipeline_deliver.py`

## Step 6: the laptop receipt and [Ya llegó] use `done_text` `[worker]`

Files: `src/invoicebot/pipeline/laptop.py`, `src/invoicebot/pipeline/actions.py`,
`tests/integration/test_pipeline_laptop.py`, `tests/integration/test_pipeline_actions.py`.

- `laptop._queue_done` and `actions._arrived` render BR-D10 with `await done_text(conn, invoice_id)` instead of
  `MESSAGES[RULE_DONE].format(folder=folder)`; drop the folder lookup each did for it.
- Update the existing BR-D10 assertions in both test files; the fixtures that build flow data add `mail_to`.

Cases (docstrings start with `BR-D10, D195:`):

1. A sent laptop receipt queues BR-D10 naming the serie-numero and `mail_to`.
2. A late receipt on a sent batch queues the same text.
3. [Ya llegó] on a `prepared` batch answers the same text; on a `sent` batch too.

Test: `uv run pytest -q tests/integration/test_pipeline_laptop.py tests/integration/test_pipeline_actions.py`

## Step 7: keep the review files while a document row waits `[worker]`

Files: `src/invoicebot/queue/outbox.py`, `src/invoicebot/pipeline/deliver.py`,
`tests/integration/test_pipeline_review.py`.

- `outbox.has_pending_document(conn, invoice_id: int) -> bool`: true when a row of the invoice is `pending` and its
  `payload_json` has a non-null `document` key.
- In `deliver`, the call `remove_review_files(context.data_dir, delivery.invoice_id)` runs only when
  `has_pending_document` is false (checked on a fresh connection right before it); drop the `shortcut:` comment in
  `review.remove_review_files` that this closes.

Cases (docstrings start with `D195:`):

1. [Enviar] while a review document row is `pending` sends the mail and leaves `mail/<id>/` in place.
2. [Enviar] with every document row `sent` removes `mail/<id>/` (today's behavior).
3. `has_pending_document`: a pending text row gives false; a pending document row of another invoice gives false; a
   `sent` document row gives false.

Test: `uv run pytest -q tests/integration/test_pipeline_review.py`

## Step 8: housekeeping sweeps leftover review directories `[worker]`

Files: `src/invoicebot/ops/housekeeping.py`, `tests/integration/test_ops_housekeeping.py`.

- Generalise `_remove_stale_tmp(pool, data_dir)` into `_remove_stale(pool, root: Path, keep: set[int])` that removes
  the invoice-named directories under `root` whose invoice is terminal or unknown, skipping ids in `keep`; the tmp
  sweep passes an empty set.
- `housekeep` also sweeps `data_dir / MAIL_DIRECTORY`, with `keep` = the invoices among its directories that
  `has_pending_document` reports; entries that are files (`<id>.eml`) are never touched (the existing `is_dir` filter
  does it). The INFO line gains `mail_dirs`. Module docstring names D195.

Cases (docstrings start with `R-9.7, D195:`):

1. `mail/<id>/` of a `DONE` invoice is removed.
2. `mail/<id>/` of a `BATCHED` invoice (a held mail) stays.
3. `mail/<id>/` of a `DONE` invoice with a pending document row stays.
4. `mail/<id>.eml` of a `DONE` invoice stays.
5. `mail/<id>/` of an unknown invoice is removed.
6. The tmp sweep's existing cases still pass.

Test: `uv run pytest -q tests/integration/test_ops_housekeeping.py`

## Step 9: live check `[inline: needs the owner's Telegram and the live bot]`

Files: `specs/spikes/review-ui.md` (new).

On the live bot, with flow B on `send: review`: `/revisar <n>` on a held mail, press [Cancelar] (BR-D19 loses
[Enviar] [Cancelar] and keeps [📂 Abrir carpeta]; BR-D20 shows one [Enviar]), press [Enviar] (BR-D20 loses its button;
BR-D10 names the invoice and the To address). Record each result in the spike file as in
`specs/spikes/open-links.md`, with any finding for the roadmap.

Test: the spike file's table has a result for each press.
