# Plan: MVP phase 9z12 · chat-tidy

Roadmap: `docs/plan-mvp.md`, row 9z12 (taken before 9z11 by the owner, 2026-11-27). Branch `feat/chat-tidy`, from
`main`.

What the live runs showed (#45, #47): the summary goes out before the fetch, so it says `⚠️ Revisa: serie, número` and
carries [✏️ Corregir] [✖️ Anular], and seconds later BR-F16 says `✅ El XML confirma: serie, número`: two messages, the
first asking for a review the second makes needless. And the final message shows no files: D194 posts them only for a
mail held for review.

Decision this plan proposes (step 1), **D206 One summary after the fetch, and the flow's documents on request**
(owner, 2026-11-27):

- **The summary waits for the fetch.** `validate` no longer posts the summary; it saves what only it knows (the
  routing rule and the SUNAT lines) and the fetch step posts the summary once, at the end of its first attempt that
  does not move straight on to the next fetcher: an `ok` result, a wait for a retry (BR-F13, `FETCH_WAITING`), a
  technical retry, the final failure (BR-F07) or a flow with nothing to fetch (D144). The summary is built from the
  reading as it is then, so a field the XML confirmed (D202) is no longer in `⚠️ Revisa`, and its buttons follow the
  existing rule (D189) on that reading.
- **On `ok` the summary holds BR-F16's lines**: `📄 Encontré {docs_desc}. Lo archivo…` and, when the XML cleared a field
  the reading's `⚠️ Revisa` named, `✅ El XML confirma: {fields_desc}`; then `Toca un botón solo si algo está mal.` only
  when it carries buttons. BR-F16 stays a message of its own only for an `ok` after the summary went out (a later
  retry). Without `ok` the summary ends with today's line (`⏳ Busco sus archivos para archivarlo.` and its button
  variant, D188, D189), followed by the step's own notice as today.
- **A note while it waits** (BR-F17): if the summary has not gone out `limits.fetch_progress_after_s` (default 20)
  after validation, one message says the bot is still on it: `⏳ Sigo con tu comprobante #{id}: busco sus archivos en
el portal del emisor. Te escribo en cuanto los tenga.` At most once per invoice.
- **[📎 Ver documentos]** on the final message of every flow (flow B BR-D10, BR-D13; flow A BR-D16, BR-D17, BR-D18)
  when the flow lists `chat_documents` (`photo`, `pdf`, `xml`, `excel`) and at least one of them was kept. Each flow
  (one per buyer RUC) names its own: live `work_microsoft` `[pdf, xml, excel]`, `personal_google` `[pdf]`; a new
  flow or a change is config only. At completion the bot keeps those files in `<data_dir>/docs/<invoice_id>/`; the
  press posts each as a document (BR-D24); when they are gone it says so with the folder link (BR-D25). Housekeeping
  removes a terminal invoice's kept documents after `ops.retention_days.chat_documents` (default 30).

Out of scope: editing a message already sent (WhatsApp cannot), the BR-D07 and BR-D08 assisted messages, 9z11
`bounce-watch`.

## Step 1: D206, the rule texts and the roadmap `[inline: spec text and user-facing copy]`

Files: `specs/05-open-questions.md`, `specs/06-business-rules.md`, `specs/02-requirements.md`, `docs/plan-mvp.md`.

- `specs/05-open-questions.md`: row D206 after D205 with the decision above, marked "(9z12 plan, proposed 2026-11-27,
  owner)".
- `specs/06-business-rules.md`: the summary section says when it goes out (D206) and its `ok` lines; BR-F16's
  condition adds "and the summary already went out"; new rows BR-F17 (the note), BR-D24 (the documents, caption the
  file name), BR-D25 (`Los documentos de #{id} ya no están guardados en el bot. Ábrelos en la carpeta.`, with the
  folder link when known).
- `specs/02-requirements.md`: R-3.4 says the summary follows the first fetch attempt (D206).
- `docs/plan-mvp.md`: row 9z12 names this plan and D206 and moves above 9z11.

Test: `npx prettier --check specs/05-open-questions.md specs/06-business-rules.md specs/02-requirements.md docs/plan-mvp.md`

## Step 2: config for the note, the documents and their retention `[worker]`

Files: `src/invoicebot/config/models.py`, `config/example.yaml`, `tests/unit/test_config_models.py`.

- `Limits.fetch_progress_after_s: int = Field(default=20, gt=0)`.
- `RetentionDays.chat_documents: int = Field(default=30, gt=0)`.
- `type ChatDocument = Literal["photo", "pdf", "xml", "excel"]`; `Flow.chat_documents: list[ChatDocument] = []`,
  duplicates refused at load; `excel` refused on a flow without an `excel_template` sink.
- `config/example.yaml`: `work_microsoft` gets `chat_documents: [pdf, xml, excel]`, `personal_google`
  `chat_documents: [pdf]`, each with a one-line D206 comment; `limits.fetch_progress_after_s: 20` and
  `ops.retention_days.chat_documents: 30` shown with a comment.

Cases (docstrings start with `D206:`):

1. The example loads; `work_microsoft.chat_documents == ["pdf", "xml", "excel"]`, `personal_dni`'s is `[]`.
2. A config without the new keys loads with 20 and 30.
3. `chat_documents: [pdf, pdf]` is refused.
4. `excel` on `personal_google` is refused.
5. `fetch_progress_after_s: 0` is refused.

Test: `uv run pytest -q tests/unit/test_config_models.py`

## Step 3: the message texts `[worker]`

Files: `src/invoicebot/rules/messages_es.py`, `tests/unit/test_rules_messages.py`.

- Constants with the step 1 texts: `SUMMARY_TAP_HINT = "Toca un botón solo si algo está mal."`, `FETCH_PROGRESS`
  (BR-F17, `{id}`), `DOCS_GONE` (BR-D25, `{id}`), `BUTTON_DOCS = "📎 Ver documentos"`, and `MESSAGES` entries for
  BR-F17 and BR-D25 the way the other rules have theirs. If that test file does not exist, use the one that already
  tests `messages_es` (search `tests/unit` for it) and say which.

Cases (docstrings start with `D206:`):

1. `FETCH_PROGRESS.format(id=47)` names `#47`; `DOCS_GONE.format(id=47)` names `#47`.
2. `BUTTON_DOCS` fits the channel button-title limit the other button titles are checked against.

Test: `uv run pytest -q tests/unit/test_rules_messages.py`

## Step 4: post the summary once, from saved inputs `[worker]`

Files: `src/invoicebot/pipeline/summary_post.py` (new), `tests/integration/test_pipeline_summary_post.py` (new).

- `async def save_summary_inputs(conn, invoice_id, *, rule_id: str, extra_lines: Sequence[str]) -> None`: merges
  `{"summary": {"rule_id": …, "extra_lines": […]}}` into the invoice's flow data (`store/flow_b.merge_flow_data`, which
  works for any invoice).
- `async def post_summary(conn, cfg, invoice_id, *, found_lines: Sequence[str] = ()) -> bool`: in the caller's
  transaction; returns False and queues nothing when the flow data has no `summary` or it says `posted`. Otherwise it
  loads the invoice and its current reading, builds `summary_text(reading, flow_line(flow, rule_id), owner_today,
cfg.extraction, (*extra_lines, *tail))` with `summary_buttons(invoice_id, reading)`, where `tail` is `found_lines`
  plus `SUMMARY_TAP_HINT` when there are buttons, or, with no `found_lines`, today's moving line (`SUMMARY_MOVING` or
  `SUMMARY_MOVING_QUIET`, moved here from `validate.py` as a function both use). Queues it to the sender with the
  saved `rule_id`, then marks `posted`. Reuse the existing helpers; read how `validate.py` gets the flow, `today` and
  the addressee and do the same.
- Module docstring cites D206.

Cases (docstrings start with `D206:`; real Postgres, the existing invoice fixtures of the pipeline tests):

1. Saved inputs and no `found_lines`: one outbox row with the summary ending in the moving line; returns True.
2. A second call queues nothing and returns False.
3. No saved inputs: nothing queued, False.
4. `found_lines=["📄 …"]` and a reading with a printed field: the text ends with that line then `SUMMARY_TAP_HINT`,
   with [✏️ Corregir] [✖️ Anular].
5. A reading whose doubtful fields were confirmed by XML (source `xml`): no `⚠️ Revisa` line and no buttons.

Test: `uv run pytest -q tests/integration/test_pipeline_summary_post.py`

## Step 5: validate saves the summary and schedules the note `[worker]`

Files: `src/invoicebot/pipeline/validate.py`, `src/invoicebot/pipeline/steps.py`,
`tests/integration/test_pipeline_validate.py`.

- `_routed_off` and `_apply` no longer build or queue the summary: they call `save_summary_inputs` with the rule and
  the SUNAT lines (`_extra_lines(verdict)`, empty when validation is off), keep the state change and the fetch job,
  and enqueue `FETCH_PROGRESS_STEP` (`"fetch_progress"`, new in `steps.py`) with `run_after` = database `now()` plus
  `cfg.limits.fetch_progress_after_s`. `_ask` is unchanged.
- Existing tests that expect the summary from `validate` now expect no message and the saved inputs; keep their
  intent, do not delete them.

Cases (docstrings start with `D206:`):

1. A routed, valid invoice: `FETCHING_DOCS`, a `fetch` job, a `fetch_progress` job about 20 s later, no outbox row, the
   saved inputs hold the rule.
2. A BR-V04 verdict: the saved `extra_lines` hold its line.
3. A BR-V02 ask still queues its question at once.

Test: `uv run pytest -q tests/integration/test_pipeline_validate.py`

## Step 6: the progress note `[worker]`

Files: `src/invoicebot/pipeline/fetch_progress.py` (new), `src/invoicebot/app.py`,
`tests/integration/test_pipeline_fetch_progress.py` (new).

- `make_fetch_progress_handler(pool)`: in one transaction, when the invoice is still `FETCHING_DOCS` and its saved
  summary is not `posted` and no note was sent (`summary.noted` in the flow data), queue BR-F17 to the sender and set
  `noted`; otherwise do nothing. Register it where the other step handlers are registered in `app.py`.

Cases (docstrings start with `D206:`):

1. `FETCHING_DOCS`, summary not posted: one BR-F17 row naming the invoice.
2. Run twice: still one row.
3. Summary already posted: nothing.
4. Invoice no longer `FETCHING_DOCS` (e.g. `DELIVERING`, `CANCELLED`): nothing.

Test: `uv run pytest -q tests/integration/test_pipeline_fetch_progress.py`

## Step 7: the fetch step posts the summary `[worker:high: reasoning: which fetch outcomes post it and in what order with the existing notices]`

Files: `src/invoicebot/pipeline/fetch.py`, `tests/integration/test_pipeline_fetch.py`.

- Call `post_summary` in the attempt's transaction, before any other message of that transaction:
  - `ok`: `_found_notice` computes its lines as today (after the XML update); when `post_summary(…,
found_lines=those lines)` returns True, BR-F16 is not queued; when it returns False (summary already out), BR-F16
    goes as today.
  - a wait (`WAIT_REASONS`), a technical retry with a delay, `_exhausted`, `_nothing_to_fetch`: `post_summary(…)`
    with no `found_lines`, then the existing notice (BR-F13, `FETCH_WAITING`, BR-F07, nothing) unchanged.
  - an attempt that moves straight to the next fetcher: no summary.
- An invoice with no saved inputs (validated before this deploy) behaves exactly as today.

Cases (docstrings start with `D206:`; the existing fake fetchers):

1. First attempt `ok` with an XML that confirms `serie` and `numero` read from OCR: one summary row holding `📄
Encontré el PDF y el XML. Lo archivo…` and `✅ El XML confirma: serie, número`, no `⚠️ Revisa`, no buttons, and no
   separate BR-F16 row.
2. First fetcher `not_found` moving on, second `ok`: still one summary, posted with the `ok`.
3. First attempt a wait: the summary with the moving line, then the existing wait notice, in that order.
4. A later `ok` after the wait: BR-F16 as its own message, no second summary.
5. All fetchers exhausted: the summary, then BR-F07.
6. A flow with no fetchers: the summary, then the `deliver` job.
7. An invoice with no saved inputs: today's messages, no summary.

Test: `uv run pytest -q tests/integration/test_pipeline_fetch.py`

## Step 8: keep and list a flow's chat documents `[worker]`

Files: `src/invoicebot/pipeline/chat_docs.py` (new), `tests/unit/test_pipeline_chat_docs.py` (new).

- `DOCS_DIRECTORY = "docs"`. `keep_documents(data_dir, invoice_id, files: Sequence[tuple[str, bytes]]) -> int`: writes
  each `(file name, bytes)` into `<data_dir>/docs/<invoice_id>/` (names checked like attachment names: no path
  separator, no `..`, CR/LF refused), returns how many. `kept_documents(data_dir, invoice_id) -> list[Document]`: the
  files there as `Document`s (MIME from the extension: pdf, xml, xlsx, jpeg/png), sorted by name; `[]` when the
  folder is gone. Module docstring cites D206.

Cases (docstrings start with `D206:`; `tmp_path`):

1. Two files kept, then listed with their MIME types and sizes.
2. Nothing kept: `[]`.
3. A name with `/` or `..` raises `ValueError` and writes nothing.

Test: `uv run pytest -q tests/unit/test_pipeline_chat_docs.py`

## Step 9: flow B keeps its documents and offers them `[worker]`

Files: `src/invoicebot/pipeline/deliver.py`, `tests/integration/test_pipeline_deliver.py`.

- When an automatic mode completes (`_automatic`, BR-D10 and BR-D13), before the photo files are unlinked: keep the
  kinds the flow lists in `chat_documents` (`photo`: the invoice's photo files; `pdf`, `xml`: from `_load`'s bytes;
  `excel`: the workbook bytes the delivery built) with `keep_documents`, using the same file names as the mail
  attachments; when at least one was kept, the final message also carries [📎 Ver documentos]
  (`button_id(invoice_id, DOCS_ACTION)`; add `DOCS_ACTION = "docs"` to `pipeline/questions.py` only if step 11 has
  not, then import it). A failure to keep the files is logged and does not fail the delivery.

Cases (docstrings start with `D206:`):

1. A flow with `chat_documents: [pdf, xml, excel]`: after BR-D10 the docs folder holds the three files and the message
   carries the button next to the folder link.
2. A flow with `chat_documents: []`: no folder, no button (today's message).

Test: `uv run pytest -q tests/integration/test_pipeline_deliver.py`

## Step 10: flow A keeps its documents and offers them `[worker]`

Files: `src/invoicebot/pipeline/deliver_google.py`, `tests/integration/test_pipeline_deliver_google.py`.

- In `_complete` (BR-D16, BR-D17, BR-D18), before the local files are unlinked: keep the kinds the flow lists
  (`photo`, `pdf`, `xml`; flow A has no `excel`) the same way, and add [📎 Ver documentos] when at least one was kept.
  Use the test file that already covers `deliver_google` if the name differs, and say which.

Cases (docstrings start with `D206:`):

1. `chat_documents: [pdf]` with a fetched PDF: the folder holds it and BR-D16 carries the button.
2. BR-D17 (no PDF) with `chat_documents: [pdf]`: nothing kept, no button.

Test: `uv run pytest -q tests/integration/test_pipeline_deliver_google.py`

## Step 11: the [📎 Ver documentos] press `[worker]`

Files: `src/invoicebot/pipeline/questions.py`, `src/invoicebot/pipeline/actions.py`,
`tests/integration/test_pipeline_actions.py`.

- `DOCS_ACTION = "docs"` in `questions.py` (if step 9 added it, leave it). In `actions.py`: `VALID_STATES[DOCS_ACTION]`
  = the terminal states, an `ACTIONS` entry and `READ_ONLY_ACTIONS` membership. The handler queues one document row
  per `kept_documents` file (BR-D24, caption the file name) and, when there are none, BR-D25 with the folder link
  (`folder_url` in the flow data, else no link). Follow `_status` as the pattern. Use the test file that covers
  `handle_press` if the name differs, and say which.

Cases (docstrings start with `D206:`):

1. A `DONE` invoice with two kept files: two document rows, BR-D24, in name order.
2. A `DONE` invoice whose folder is gone: one BR-D25 row with the folder link.
3. A press on a non-terminal invoice: BR-S02, as for any stale press.
4. The press does not clear a pending [Editar] (read-only).

Test: `uv run pytest -q tests/integration/test_pipeline_actions.py`

## Step 12: housekeeping removes old chat documents `[worker]`

Files: `src/invoicebot/ops/housekeeping.py`, `src/invoicebot/app.py`, `tests/integration/test_ops_housekeeping.py`.

- `housekeep` also removes `<data_dir>/docs/<id>/` of a terminal invoice whose last state change is older than
  `ops.retention_days.chat_documents` days (database clock), and of an id with no invoice; the count joins the
  `housekeeping done` log. Pass the retention in from `app.py` where `housekeep` is scheduled.

Cases (docstrings start with `D206:`):

1. A `DONE` invoice changed 31 days ago with retention 30: its docs folder is removed.
2. Changed 1 day ago: kept.
3. A non-terminal invoice: kept.
4. A folder with no invoice: removed.

Test: `uv run pytest -q tests/integration/test_ops_housekeeping.py`

## Step 13: runbook, live config and the live check `[inline: host commands and a live check with the owner]`

Files: `docs/runbook.md`, `specs/spikes/chat-tidy.md` (new).

- `docs/runbook.md`: where a flow's chat documents are set (`chat_documents`) and how long they stay.
- After the merge: build, `./deploy/deploy-live.sh <tag>`; live config adds `chat_documents` (`work_microsoft`
  `[pdf, xml, excel]`, `personal_google` `[pdf]`) and reloads; `forget` the test invoice; the owner sends the La Leña
  receipt in Telegram: one summary with no Revisa line, the final message with [📎 Ver documentos], the press posts
  the three files. Record it in `specs/spikes/chat-tidy.md` (no addresses).

Test: `npx prettier --check docs/runbook.md specs/spikes/chat-tidy.md`
