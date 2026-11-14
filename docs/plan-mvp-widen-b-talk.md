# Plan: MVP phase 6b · widen-b-talk

Roadmap: `docs/plan-mvp.md` row 6b. The conversation around an invoice: the questions routing asks (unknown buyer, doubtful buyer, which brand), edits with `campo=valor`, duplicates, the 24 h park, the chat commands and their scoping, and the rule-ID and Spanish-text guarantees. Stories S4.3–S4.10, S5.2, S5.3, BR-R09. Rules: BR-R03, BR-R04, BR-R05, BR-R07, BR-R09, BR-E02, BR-E03, BR-E04, BR-S02, BR-S07, BR-S08. Requirements: R-3.2, R-3.3, R-3.5–R-3.8, R-10, R-10.1, R-11.1, R-9.8.

**Goal:** an invoice whose buyer matches no rule asks which flow, with the sender's allowed flows as buttons. A buyer read badly by OCR asks [Sí] [Cambiar] [Cancelar]. A store with several brands and no matching serie prefix asks which brand, so delivery never fails on it. The summary carries [Editar] [Cancelar]; `campo=valor` lines edit the reading, re-validate and re-route. A second photo of the same invoice stops with BR-E04. A question left 24 h parks the invoice as `PENDING_USER`, and answering resumes it. `/ayuda`, `/estado`, `/pendientes`, `/cancelar` and `/reprocesar` work and act only on the sender's own invoices unless the sender is an admin. Every non-happy transition stores a catalog rule ID, and no user text is written outside `rules/messages_es.py`.

**Needs from the owner first:** nothing. The owner reviews the design choices below, which change two lines of spec (BR-E04's text, S4.6's second check) and add one alias.

## Design choices

Step 1 writes these into the specs (spec 05, D92 to D97).

- **D92 · Answers are data; routing runs once, in order.** A button answer never moves the invoice forward itself. It stores the answer in the invoice's flow data under `answers` (`flow`, `buyer_ok`, `cap_ok`, `comercio`), puts the invoice back in `ROUTING` and enqueues the route job. The route handler runs the same ordered checks every time and skips the ones already answered: duplicate (BR-E04), flow (rule match, else BR-R03), buyer confidence (BR-R04), cap (BR-D06), brand (BR-R09), then the summary and `FETCHING_DOCS`. One place owns the order, and the button handlers need no config. An edit clears `answers`.
- **D93 · Duplicates.** The natural key `ruc_emisor|tipo|serie|numero` is claimed at the start of routing. If another invoice holds it and is not `FAILED` or `CANCELLED`, the new one is `CANCELLED` with BR-E04. A failed or cancelled holder gives the key up. The second invoice cannot be forced through: the same serie and number is the same invoice, so filing it twice would double a reimbursement. BR-E04's text points to `/estado` instead of `/reprocesar`: `Este comprobante ya lo registré el {created_at:%d/%m} (#{id}). Mira cómo va con /estado {id}.` S4.6's second check becomes "`/reprocesar <id>` on the first invoice re-runs it" (D96).
- **D94 · Parking.** A sweep every 5 minutes moves invoices that have sat in `CONFIRMING`, `EXTRACT_FAILED` or `CONFIRM_INVALID` longer than `limits.question_timeout_h` to `PENDING_USER` with BR-S08, storing `waiting_state` and the question's rule in a new `waiting_rule_id`. A button from the parked question, an edit, or the question re-sent by `/pendientes` restores both and then acts. `/pendientes` re-sends a parked invoice's question by copying its last outbox message (needs `outbox.invoice_id`, new). BR-S08's `question_timeout` is the config key `limits.question_timeout_h`.
- **D95 · Late writes lose.** `/cancelar` and an edit may move an invoice while a job is working on it. `set_state` takes an optional `expected` state; when the row has moved on it raises `StateMoved`, the job's transaction rolls back, and the worker completes the job without retrying (`invoice_moved_on` in the log). The extract, route and fetch handlers pass it on the transitions that end their work.
- **D96 · Commands.** `/cancelar <id>` works in any state before `DELIVERING` (the summary's [Cancelar] gets the same set). `/reprocesar <id>` works on `FAILED` with a flow (to `FETCHING_DOCS`), `MANUAL_REQUIRED` (to `FETCHING_DOCS`) and `BATCHED` with a `failed` batch (the batch goes back to `prepared` and its send job is enqueued). A `DONE` invoice is not re-run, so nothing is filed twice. An id may be written `12` or `#12`. A non-admin sees and changes only its own invoices; an unknown id and someone else's get the same BR-S07 answer (R-10.1). `/pendientes` lists the sender's invoices in `PENDING_USER`, `CONFIRMING`, `EXTRACT_FAILED`, `CONFIRM_INVALID` and `MANUAL_REQUIRED`, plus `BATCHED` invoices whose batch has been `prepared` for 2 days or more (S11.4); at most 20 lines, and it re-sends the questions of the first 3 parked ones.
- **D97 · Edits.** `campo=valor` lines, one per line, may change `ruc_emisor` (alias `ruc`), `tipo`, `serie`, `numero`, `fecha_emision` (alias `fecha`, `dd/mm/aaaa`), `total` and `num_doc_adq` (alias `cliente`, new: the sender cannot be expected to type `num_doc_adq`). A changed serie without `tipo` infers it from the first letter (F → 01, B → 03, R-3.5). A bad RUC gets BR-E02, a bad date BR-E03, anything else unreadable one general text. Edits are accepted in `EXTRACT_FAILED`, `ROUTING`, `CONFIRMING`, `CONFIRM_INVALID`, `FETCHING_DOCS`, `MANUAL_REQUIRED` and `PENDING_USER`; they target the invoice the same way `motivo=` does (`find_override_target`, D90). An edited field is stored with source `user` and confidence 1.0.

Not in this phase, listed as drift in step 1: `confirm_mode: always`, the `VALIDATING` state and BR-E03 / `source=llm` asking in `CONFIRMING` (spec 03 §4) are still not built; routing goes straight to `FETCHING_DOCS`. `confirm_mode` stays unused until `read-more` (BR-E03 needs it there).

Conventions: lint is `uv run ruff check src tests scripts migrations && uv run ruff format --check src tests scripts migrations && uv run mypy src && uv run lint-imports`. Integration tests need Postgres (`podman compose -f deploy/docker-compose.yml up -d postgres`). Every new user text is a constant in `rules/messages_es.py` with the exact Spanish given here. States named in new code come from `invoicebot/states.py` (step 3).

Out of scope here: BR-R06 and the gap service (`widen-b-deliver`), `/pagado` (S11.11), `/portal`, `/gaps`, Telegram, the channel-neutral button codec (roadmap, `ops`).

## Step 1: Record D92–D97 and the BR-E04 text `[inline: spec and roadmap text the owner reviews with this plan]`

Files: `specs/05-open-questions.md`, `specs/06-business-rules.md`, `specs/04-tasks.md`, `specs/02-requirements.md`, `docs/plan-mvp.md`, `src/invoicebot/rules/catalog.py`, `src/invoicebot/rules/messages_es.py`.

- Spec 05: rows D92 to D97 with the text from "Design choices".
- Spec 06: BR-E04's effect becomes `CANCELLED` and its text the one in D93; BR-S08 names `limits.question_timeout_h`.
- Spec 02: R-3.5 adds the alias `cliente` = `num_doc_adq`; R-3.6 drops "unless the user sends `/reprocesar <id>`" and says the first invoice can be re-run with it (D93).
- Spec 04: S4.6's second check as in D93; S4.8 and S4.9 cite D94 and D96.
- Catalog: `BR-E04` state `CANCELLED`; `MESSAGES["BR-E04"]` gets the new text.
- Roadmap: mark the `retry_schedule` item `(widen-b-talk)`; add the drift line from "Design choices" under "Known drift"; add under "Carried over" for `ops`: move the remaining state string constants (`actions.py`, `route.py`, `extract.py`, `fetch.py`, `deliver.py`, `handlers.py`) to `invoicebot/states.py`.

Test: `uv run pytest -q tests/unit/test_rules_parity.py`.

## Step 2: Migration 0006 and `outbox.invoice_id` `[worker]`

Files: `migrations/versions/0006_talk.py`, `src/invoicebot/queue/outbox.py`, `tests/integration/test_outbox.py`.

- Expand only, plain SQL through `op.execute`, same style as `0005_flow_b.py`: `ALTER TABLE invoices ADD COLUMN waiting_rule_id TEXT`; `ALTER TABLE outbox ADD COLUMN invoice_id INTEGER REFERENCES invoices(id)`; `CREATE INDEX ix_outbox_invoice ON outbox (invoice_id, id) WHERE invoice_id IS NOT NULL`. `downgrade` drops the index and both columns.
- `outbox.add` takes `invoice_id: int | None = None` (keyword) and stores it.

Test cases: `add(..., invoice_id=<a real invoice>)` stores it; without it the column is NULL; an id with no invoice raises a foreign-key error.

Test: `uv run pytest -q tests/integration/test_outbox.py tests/integration/test_schema.py`.

## Step 3: States module and the `set_state` guards (S5.2, D95) `[worker]`

Files: `src/invoicebot/states.py`, `src/invoicebot/store/invoices.py`, `tests/integration/test_invoices_store.py`.

- `states.py` (module docstring: the invoice state names and the sets the conversation uses, spec 03 §4): one `Final` string constant per state in `STATE_DESC`'s list, and frozensets `TERMINAL` (`DONE`, `CANCELLED`, `FAILED`), `WAITING_FOR_USER` (`CONFIRMING`, `EXTRACT_FAILED`, `CONFIRM_INVALID`), `NEEDS_RULE` (`CANCELLED`, `FAILED`, `CONFIRMING`, `EXTRACT_FAILED`, `CONFIRM_INVALID`, `PENDING_USER`, `MANUAL_REQUIRED`, `PENDING_AUTH`, `WAITING_CAPTCHA`), `CANCELLABLE` (every non-terminal state before `DELIVERING`: `RECEIVED`, `EXTRACTING`, `EXTRACT_FAILED`, `ROUTING`, `CONFIRMING`, `VALIDATING`, `CONFIRM_INVALID`, `FETCHING_DOCS`, `WAITING_CAPTCHA`, `MANUAL_REQUIRED`, `PENDING_USER`, `PENDING_AUTH`), `EDITABLE` (D97's list). And `class StateMoved(Exception)` carrying `invoice_id`, `expected`, `actual`.
- `set_state(conn, invoice_id, state, *, rule_id=None, error=None, expected=None)`: raises `ValueError` when `state in NEEDS_RULE` and `rule_id` is None, or when `rule_id` is not a key of `rules.catalog.RULES`. With `expected`, the UPDATE adds `AND state = %s`; zero rows updated raises `StateMoved` with the actual state (read in the same transaction). Docstring updated.

Test cases: `CANCELLED` without a rule → `ValueError`; rule `BR-X99` → `ValueError`; `DELIVERING` without a rule → ok, `rule_id` NULL; `expected="FETCHING_DOCS"` on a `CANCELLED` row → `StateMoved` with `actual == "CANCELLED"`, row unchanged; `expected` matching → updated.

Test: `uv run pytest -q tests/integration/test_invoices_store.py` and the existing `tests/integration/` suite for the pipeline (`uv run pytest -q tests/integration -k "pipeline or worker"`), which must still pass: a call site that now raises is a step failure to report, not to paper over.

## Step 4: Late writes lose (D95) `[worker]`

Files: `src/invoicebot/queue/worker.py`, `src/invoicebot/pipeline/extract.py`, `src/invoicebot/pipeline/route.py`, `src/invoicebot/pipeline/fetch.py`, tests in `tests/integration/test_worker.py` and `tests/integration/test_pipeline_fetch.py`.

- The worker catches `StateMoved` from a handler: the handler's transaction is already rolled back; it logs `invoice_moved_on` (extra: `job_id`, `invoice_id`, `expected`, `actual`) at INFO and completes the job without counting a failure.
- Pass `expected=` on: `extract.py`'s `EXTRACTING → ROUTING` and `EXTRACTING → EXTRACT_FAILED`; `route.py`'s transitions out of `ROUTING`; `fetch.py`'s transitions out of `FETCHING_DOCS` (`:241`, `:258` and any other). Each handler already returns early when its invoice is not in its state; add that check where it is missing.

Test cases: a handler that raises `StateMoved` → job done, attempts unchanged, log line present; a fetch job whose invoice was set `CANCELLED` mid-run (fake fetcher that cancels the invoice in its own connection) → invoice stays `CANCELLED`, no outbox row from the fetch.

Test: `uv run pytest -q tests/integration/test_worker.py tests/integration/test_pipeline_fetch.py`.

## Step 5: One way to queue a message (S5.3) `[worker]`

Files: `src/invoicebot/pipeline/outgoing.py`, `src/invoicebot/pipeline/route.py`, `src/invoicebot/pipeline/actions.py`, `src/invoicebot/pipeline/replies.py`.

- `outgoing.py` (docstring: the one function the pipeline uses to queue a chat message, R-1.8, R-11.1): `async def queue_message(conn, *, channel, profile_id, text, invoice_id=None, rule_id=None, buttons=()) -> int`, with the body `route.py`'s `_reply` uses today (`OutboundMessage` + `outbox.add`), passing `invoice_id` to `outbox.add`.
- `route.py`, `actions.py` and `replies.py` call it instead of building `OutboundMessage` themselves. `replies._send_stale` is deleted and its caller uses `actions.send_stale`, so the BR-S02 text is built in one place.

Test: `uv run pytest -q tests/integration/test_pipeline_route.py tests/integration/test_pipeline_actions.py tests/integration/test_intake.py`.

## Step 6: Move extract, fetch and handlers to `queue_message` `[worker:low]`

Files: `src/invoicebot/pipeline/extract.py`, `src/invoicebot/pipeline/fetch.py`, `src/invoicebot/pipeline/handlers.py`.

- Replace each `OutboundMessage(...)` + `outbox.add(...)` pair with `queue_message(...)`, passing the invoice id. No behavior change.

Test: `uv run pytest -q tests/integration/test_pipeline_extract.py tests/integration/test_pipeline_fetch.py tests/integration/test_pipeline_handlers.py`.

## Step 7: Move deliver, intake and overrides to `queue_message` `[worker:low]`

Files: `src/invoicebot/pipeline/deliver.py`, `src/invoicebot/pipeline/intake.py`, `src/invoicebot/pipeline/overrides.py`.

- Same as step 6. Intake's back-pressure message (BR-S01) has no invoice and passes none.

Test: `uv run pytest -q tests/integration/test_pipeline_deliver.py tests/integration/test_intake.py tests/integration/test_pipeline_overrides.py`.

## Step 8: Test that user text and log events have one home (S5.3) `[worker]`

Files: `tests/unit/test_user_text.py`, plus any module the test catches (move its literal into `rules/messages_es.py`).

- Walk every `.py` under `src/invoicebot/` except `rules/messages_es.py` with `ast`. Fail on a call to `queue_message`, `OutboundMessage` or `Button` whose `text=` or `title=` argument is a string literal, or an f-string with a literal part that holds a letter. Fail on a `logger.<debug|info|warning|error|exception>(...)` whose first argument is a literal not matching `^[a-z][a-z0-9_]*$`, unless `tests/unit/test_logging.py` already checks this (then only the text rule).
- Docstrings start with `R-9.8` / `R-11.1` (S5.3).

Test cases: the real tree passes; a synthetic source string with `queue_message(conn, text="Hola")` fails; one with `text=MESSAGES["BR-S01"]` passes; `logger.info("Sent it")` fails; `logger.info("outbox_sent")` passes.

Test: `uv run pytest -q tests/unit/test_user_text.py`.

## Step 9: More than three choices become a list message `[worker]`

Files: `src/invoicebot/channels/whatsapp/transport.py`, `src/invoicebot/rules/messages_es.py`, `src/invoicebot/config/checks.py`, tests in `tests/integration/test_whatsapp_transport.py` and `tests/unit/test_config_checks.py`.

- Transport: a message with more than `BUTTONS_MAX_COUNT` buttons is sent with `list_payload(to, text, LIST_BUTTON, buttons)` (BR-R03, BR-R09).
- `messages_es.py`: `LIST_BUTTON = "Elegir"`.
- Config check: every flow `label` and every brand `comercio` is 1–20 characters (they become button titles, `BUTTONS_LABEL_MAX_CHARS`); a longer one fails startup naming the flow or store file.

Test cases: 3 buttons → button payload; 4 → list payload with 4 rows; a 21-character flow label → check error.

Test: `uv run pytest -q tests/integration/test_whatsapp_transport.py tests/unit/test_config_checks.py`.

## Step 10: Buttons carry a choice `[worker]`

Files: `src/invoicebot/channels/whatsapp/payloads.py`, `src/invoicebot/pipeline/replies.py`, `src/invoicebot/pipeline/actions.py`, test in `tests/unit/test_whatsapp_payloads.py`.

- `button_id(invoice_id, action, choice=None)` → `"<id>:<action>"`, or `"<id>:<action>:<choice>"` with `choice` an int 0–9 (else `ValueError`). `parse_button_id` returns `ButtonPress(invoice_id, action, choice)` (a `NamedTuple`, `choice: int | None`), or None for anything else.
- `resolve_reply` and `handle_press` pass `choice` through; the `Action` type gains a `choice: int | None` argument (existing actions ignore it).

Test cases: `button_id(7, "pick", 2)` → `"7:pick:2"` → parses back; `"7:pick"` → choice None; `"7:pick:10"`, `"7:pick:x"`, `"7:Pick"` → None; `button_id(7, "pick", 10)` → `ValueError`.

Test: `uv run pytest -q tests/unit/test_whatsapp_payloads.py tests/integration/test_pipeline_actions.py`.

## Step 11: Routing asks: flow, doubtful buyer, brand (S4.3, S4.4, BR-R09, D92) `[worker:high: reasoning: routing becomes an ordered, resumable check list over stored answers, and each question must agree with the button handler of step 12]`

Files: `src/invoicebot/pipeline/route.py`, `src/invoicebot/pipeline/questions.py`, `src/invoicebot/rules/messages_es.py`, tests in `tests/integration/test_pipeline_route.py`.

- `questions.py` (docstring: the questions routing asks and their buttons): pure functions returning `(text, buttons)`:
  - `flow_question(invoice_id, reading, candidates)` → BR-R03's text with `buyer_desc`, one button per candidate flow (`pick`, choice = index, title = `label`), then [Cancelar].
  - `buyer_question(invoice_id, reading, flow)` → BR-R04's text (`num_doc_adq`, `flow.label`), buttons [Sí] (`yes`), [Cambiar] (`change`), [Cancelar].
  - `brand_question(invoice_id, reading, store)` → BR-R09's text (`razon_social`), one button per brand (`brand`, choice = index, title = `comercio`), then [Cancelar].
  - The BR-D06 question moves here from `route._cap_question` unchanged.
  - `candidate_flows(profile_flows, flows, tipo)`: the profile's flows in config order whose `accepted_tipos` holds `tipo`.
- `messages_es.py`: `BUTTON_YES = "Sí"`, `BUTTON_CHANGE = "Cambiar"`.
- The route handler, in `ROUTING`, reads `answers = flow_data.get("answers", {})` and runs in order (D92), each question setting `CONFIRMING` with its rule and queuing the question with the invoice id:
  1. Flow: `answers["flow"]` if set and still among the candidates; else `router.route(...)`. A no-match with `unmatched: ask` asks BR-R03 (no candidates → `CANCELLED` with BR-R07). Refusals cancel as today. The shortcut comment at `:93` goes.
  2. Buyer: if `answers.get("buyer_ok")` is None and `num_doc_adq` has source `ocr` and confidence below `extraction.review_threshold`, store the flow (`set_flow`) and ask BR-R04. `buyer_ok` False asks BR-R03.
  3. Cap: BR-D06 as today, unless `answers.get("cap_ok")`.
  4. Brand: unless `answers.get("comercio")`: the issuer's store from the store directory (the lookup `deliver.py` uses); when it has more than one brand and `comercio_for(...)` is None, ask BR-R09.
  5. Summary and `FETCHING_DOCS` as today.
- An `answers["flow"]` that is no longer a candidate, or a brand index out of range, is dropped and the question asked again.

Test cases (cite the rule): a DNI buyer with `unmatched: ask` and two allowed flows → `CONFIRMING`, `rule_id` BR-R03, 3 buttons; same with `answers.flow` = the second flow → summary, `FETCHING_DOCS`, flow set; no candidate for the `tipo` → `CANCELLED` BR-R07; buyer from OCR at 0.5 (threshold 0.8) → BR-R04 with [Sí] [Cambiar] [Cancelar]; same with `buyer_ok` true → continues; `buyer_ok` false → BR-R03; a two-brand store with an unknown serie prefix → BR-R09 with 2 brand buttons; with `answers.comercio` → continues; a one-brand store → no question.

Test: `uv run pytest -q tests/integration/test_pipeline_route.py`.

## Step 12: Button answers (S4.3, S4.4, BR-R09, D92) `[worker]`

Files: `src/invoicebot/pipeline/actions.py`, `src/invoicebot/store/flow_b.py` (only if a helper is missing), tests in `tests/integration/test_pipeline_actions.py`.

- New actions, each valid only in `CONFIRMING` with the given `rule_id` (otherwise BR-S02, as stale buttons are answered today):
  - `pick` (BR-R03): `answers.flow` = the id of candidate `choice` (recompute `candidate_flows`; out of range → BR-S02).
  - `yes` (BR-R04): `answers.buyer_ok = true`. `change` (BR-R04): `answers.buyer_ok = false`.
  - `brand` (BR-R09): `answers.comercio` = the brand at `choice` (out of range → BR-S02).
  - `continue` (BR-D06): now `answers.cap_ok = true` instead of moving to `FETCHING_DOCS` itself.
- Each stores the answer with `merge_flow_data`, sets `ROUTING` (`expected="CONFIRMING"`) and enqueues the route step. The actions that need config (`pick`, `brand`) get it through the action registry's construction (a factory taking `flows`, `profiles` and the store directory), not a global.
- `cancel` becomes valid in every `CANCELLABLE` state (the summary's [Cancelar], R-3.4).

Test cases: `pick` choice 1 on BR-R03 → `answers.flow` = second candidate, `ROUTING`, one route job; `pick` on a BR-R04 invoice → BR-S02, no change; `yes`/`change` store `buyer_ok`; `brand` choice 5 of 2 → BR-S02; `continue` on BR-D06 → `cap_ok`, `ROUTING`; `cancel` in `FETCHING_DOCS` → `CANCELLED`; `cancel` in `DELIVERING` → BR-S02.

Test: `uv run pytest -q tests/integration/test_pipeline_actions.py`.

## Step 13: Delivery uses the chosen brand (BR-R09) `[worker]`

Files: `src/invoicebot/pipeline/deliver.py`, tests in `tests/integration/test_pipeline_deliver.py`.

- Where `deliver.py` calls `comercio_for` (`:316`), use `flow_data["answers"]["comercio"]` first when present.

Test cases: a two-brand store, unknown prefix, `answers.comercio` = second brand → files under that brand's folder; no answer and no prefix match → the current error path (unchanged).

Test: `uv run pytest -q tests/integration/test_pipeline_deliver.py`.

## Step 14: Duplicates (S4.6, BR-E04, D93) `[worker]`

Files: `src/invoicebot/store/invoices.py`, `src/invoicebot/pipeline/route.py`, tests in `tests/integration/test_invoices_store.py` and `tests/integration/test_pipeline_route.py`.

- `claim_natural_key(conn, invoice_id, key) -> Duplicate | None` (`Duplicate(invoice_id, created_at)`, frozen dataclass): clears `natural_key` on any other invoice holding `key` in `FAILED` or `CANCELLED`; if another invoice still holds it, returns that one; else sets `key` on this invoice (also when it had a different key before) and returns None. A unique violation from a concurrent claim propagates (the job retries and then finds the holder).
- Route handler, first check: the key is `f"{ruc_emisor}|{tipo}|{serie}|{numero}"` from the reading; a duplicate → `CANCELLED` with BR-E04 and its text (`created_at`, `id` of the first).

Test cases: first invoice claims → None, key stored; a second with the same key while the first is `DONE` → BR-E04 with the first's id, second `CANCELLED`; the first `FAILED` → the second takes the key; the same invoice claiming again (re-route) → None; an edited `numero` → the key changes.

Test: `uv run pytest -q tests/integration/test_invoices_store.py tests/integration/test_pipeline_route.py`.

## Step 15: Parse edits (S4.5, D97) `[worker]`

Files: `src/invoicebot/pipeline/edits.py`, `src/invoicebot/rules/messages_es.py`, `tests/unit/test_edits_parse.py`.

- `parse_edits(text) -> Edits` (frozen dataclass: `fields: Mapping[str, str]`, `errors: tuple[EditError, ...]` with `EditError(rule_id: str | None, line: str, field: str | None, value: str)`). Pure, no I/O. Lines are split on newlines; each is `campo=valor` with spaces around `=` allowed; keys are case-insensitive and accent-free after `strip`. Keys `motivo` and `descripcion` are skipped (they stay with `overrides.py`). Lines without `=` are ignored. At most 10 lines and 120 characters per value (`MAX_OVERRIDE_CHARS`), else an error for that line.
- Aliases: `ruc` → `ruc_emisor`, `fecha` → `fecha_emision`, `cliente` → `num_doc_adq`. Values: `ruc_emisor` 11 digits passing `is_valid_ruc`, else error BR-E02; `num_doc_adq` 8 or 11 digits; `fecha_emision` `dd/mm/aaaa` (also `-`), stored ISO, a bad date → error BR-E03; `total` a decimal with `.` or `,` and at most 2 decimals; `serie` passing `check_serie`, upper-cased; `numero` 1–8 digits, leading zeros dropped; `tipo` `01` or `03`. A `serie` without `tipo` adds `tipo` from its first letter (F → 01, B → 03). An unknown key or a bad value without a rule → error with `rule_id` None.
- `messages_es.py`: `EDIT_INVALID = "No entendí «{line}». Escribe cada cambio así: campo=valor, por ejemplo total=45.50. Puedes cambiar ruc, serie, numero, fecha, total y cliente."`

Test cases (R-3.5): `"total=45,50\nfecha=05/11/2026"` → `total` `45.50`, `fecha_emision` `2026-11-05`; `"RUC = <valid synthetic RUC>"` → `ruc_emisor`; a RUC with a bad check digit → error BR-E02; `fecha=31/02/2026` → error BR-E03; `serie=b001` → `serie` `B001`, `tipo` `03`; `color=rojo` → error, rule None; `motivo=almuerzo` → no fields, no errors; 11 lines → error on the 11th.

Test: `uv run pytest -q tests/unit/test_edits_parse.py`.

## Step 16: Apply edits and re-route (S4.5, D97) `[worker]`

Files: `src/invoicebot/pipeline/edits.py`, `src/invoicebot/pipeline/intake.py`, `src/invoicebot/store/invoices.py`, tests in `tests/integration/test_intake.py`.

- `apply_edits(conn, msg, edits) -> int | None`: target via `find_override_target`. No target → nothing. Errors → queue the first error's text (BR-E02 with `ruc`, BR-E03 with `fecha`, else `EDIT_INVALID` with `line`) and change nothing. A state outside `EDITABLE` → `EDIT_LATE = "El comprobante #{id} ya está {state_desc}; ya no puedo cambiar sus datos."` (new constant). Otherwise load the stored reading, replace the edited fields with source `user` and confidence 1.0, save it, clear `answers`, and: from `EXTRACT_FAILED` with required fields still missing → queue BR-E01 again and stay; else set `ROUTING` (`expected` = the current state) and enqueue the route step, which re-validates, re-claims the natural key and sends the summary again.
- Intake: a text goes to `apply_edits` when `parse_edits` finds fields or errors, and to the existing override path for `motivo`/`descripcion` as today (a text may hold both).

Test cases: a `CONFIRMING` invoice + `total=10.00` → `ROUTING`, route job, field source `user`; an `EXTRACT_FAILED` invoice + all of ruc, serie, numero, fecha, total → `ROUTING`; the same with only `total` → BR-E01, stays; `DELIVERING` → `EDIT_LATE`; `ruc=<bad>` → BR-E02 text, nothing changed; `cliente=<the work RUC>` on a BR-R03 invoice → re-routed to the work flow.

Test: `uv run pytest -q tests/integration/test_intake.py tests/unit/test_edits_parse.py`.

## Step 17: [Editar] on the summary (S4.5, R-3.4) `[worker]`

Files: `src/invoicebot/pipeline/route.py`, `src/invoicebot/pipeline/actions.py`, `src/invoicebot/rules/messages_es.py`, tests in `tests/integration/test_pipeline_route.py` and `tests/integration/test_pipeline_actions.py`.

- The summary gets buttons [Editar] (`edit`) and [Cancelar] (`cancel`); the shortcut comment at `route.py:103` goes.
- Action `edit`, valid in `EDITABLE` states (else BR-S02): queues `EDIT_PROMPT` for the invoice; no state change.
- `messages_es.py`: `BUTTON_EDIT = "Editar"`; `EDIT_PROMPT = "Envíame los cambios, uno por línea, así: campo=valor. Por ejemplo: total=45.50 o fecha=05/11/2026. Puedes cambiar ruc, serie, numero, fecha, total y cliente."`

Test cases: a routed invoice's summary has 2 buttons, `edit` then `cancel`; `edit` in `FETCHING_DOCS` → `EDIT_PROMPT`, state unchanged; `edit` in `DONE` → BR-S02.

Test: `uv run pytest -q tests/integration/test_pipeline_route.py tests/integration/test_pipeline_actions.py`.

## Step 18: Park unanswered questions (S4.8, BR-S08, D94) `[worker]`

Files: `src/invoicebot/store/invoices.py`, `src/invoicebot/pipeline/park.py`, `src/invoicebot/app.py`, tests in `tests/integration/test_invoices_store.py`.

- `park_stale(conn, timeout_h) -> list[InvoiceRef]` (`id`, `profile_id`, `origin_channel`): one `UPDATE ... RETURNING` that sets `waiting_state = state`, `waiting_rule_id = rule_id`, `state = 'PENDING_USER'`, `rule_id = 'BR-S08'`, `updated_at = now()` for invoices in `WAITING_FOR_USER` with `updated_at < now() - make_interval(hours => %s)`. Database clock only.
- `park.py` (docstring: parks invoices whose question went unanswered, R-3.7): `async def park_stale_questions(pool, timeout_h) -> int` runs it and queues BR-S08 per row in the same transaction; logs `invoices_parked` with the count.
- `app.py`: a `_park_forever(pool, cfg)` loop under the existing supervisor, every `PARK_SWEEP_S = 300` seconds (constant in `park.py`), with `cfg.limits.question_timeout_h`.

Test cases: a `CONFIRMING` row with `updated_at` 25 h ago → `PENDING_USER`, `waiting_state` `CONFIRMING`, `waiting_rule_id` its old rule, BR-S08 queued with the invoice id; 23 h ago → untouched; `FETCHING_DOCS` 48 h ago → untouched.

Test: `uv run pytest -q tests/integration/test_invoices_store.py`.

## Step 19: Answering a parked invoice resumes it (S4.8, D94) `[worker]`

Files: `src/invoicebot/store/invoices.py`, `src/invoicebot/pipeline/replies.py`, `src/invoicebot/pipeline/edits.py`, tests in `tests/integration/test_pipeline_actions.py`.

- `unpark(conn, invoice_id) -> bool`: for a `PENDING_USER` row, restores `state = waiting_state`, `rule_id = waiting_rule_id`, clears both waiting columns; returns whether it did.
- A button press on a `PENDING_USER` invoice: when `waiting_state` and `waiting_rule_id` are what the action is valid for, `unpark` first, then the action runs as usual; otherwise BR-S02.
- `apply_edits` on a `PENDING_USER` invoice `unpark`s first.

Test cases: an invoice parked from BR-R03 + `pick` → `ROUTING` with `answers.flow`; parked from BR-R03 + `yes` → BR-S02, still parked; parked from `EXTRACT_FAILED` + a complete edit → `ROUTING`.

Test: `uv run pytest -q tests/integration/test_pipeline_actions.py tests/integration/test_intake.py`.

## Step 20: Commands, `/ayuda` and scoping (S4.9, S4.10, BR-S07) `[worker]`

Files: `src/invoicebot/pipeline/commands.py`, `src/invoicebot/pipeline/intake.py`, `src/invoicebot/api/webhook.py`, `src/invoicebot/rules/messages_es.py`, tests in `tests/integration/test_commands.py`.

- `commands.py` (docstring: chat commands, R-10): `parse_command(text) -> tuple[str, str] | None` (a text starting with `/`: lower-cased name, the rest stripped); `COMMANDS: Mapping[str, Command]` where `Command` holds the handler and its help line; `handle_command(conn, msg, name, arg, *, admins)`; an unknown name answers with the help. `resolve_target(conn, arg, profile_id, admins) -> InvoiceHead | None`: `arg` is `12` or `#12`; missing → `COMMAND_NEEDS_ID`; not found, or another profile's while the sender is not an admin → BR-S07 (same text either way, R-10.1).
- `/ayuda`: `HELP_HEADER` then each command's help line, in `COMMANDS` order.
- Intake: a text that `parse_command` accepts goes to `handle_command` before edits and overrides. `handle_inbound` gets `admins: frozenset[str]` (profile ids); the webhook passes the admin profiles it already has in `deps.admin_profiles`.
- `messages_es.py`:
  - `HELP_HEADER = "Envíame la foto de un comprobante y yo me encargo. También entiendo:"`
  - `HELP_AYUDA = "/ayuda: esta lista"`
  - `COMMAND_NEEDS_ID = "Falta el número del comprobante. Por ejemplo: /{command} 12"`

Test cases (R-10.1, BR-S07): `/ayuda` → header + `/ayuda` line; `/AYUDA` works; `/foo` → help; `/estado` (registered in step 21) is not needed here: test `resolve_target` directly: own id → head; `#id` → head; another profile's id → BR-S07; unknown id → the same BR-S07 text; an admin with another profile's id → head; missing arg → `COMMAND_NEEDS_ID`.

Test: `uv run pytest -q tests/integration/test_commands.py tests/integration/test_intake.py tests/integration/test_webhook.py`.

## Step 21: `/estado` (S4.9) `[worker]`

Files: `src/invoicebot/pipeline/commands.py`, `src/invoicebot/queue/outbox.py`, `src/invoicebot/rules/messages_es.py`, tests in `tests/integration/test_commands.py`.

- `outbox.last_text(conn, invoice_id, rule_id) -> str | None`: the newest outbox row's `payload_json->>'text'` for that invoice whose payload `rule_id` matches.
- `/estado <id>` → `STATUS` with `id` and `state_desc`; when the invoice has a `rule_id`, add `STATUS_RULE` with the rule and the last text (or `STATUS_RULE_ONLY` when there is no stored text).
- `messages_es.py`:
  - `HELP_ESTADO = "/estado 12: cómo va el comprobante #12"`
  - `STATUS = "El comprobante #{id} está {state_desc}."`
  - `STATUS_RULE = "Último aviso ({rule_id}): {text}"`
  - `STATUS_RULE_ONLY = "Motivo: {rule_id}."`

Test cases (S4.9, R-11.1): a BR-R03 invoice → state text + `Último aviso (BR-R03): ` + the question text; a `FETCHING_DOCS` invoice with no rule → only the state line; a rule with no stored text → `Motivo: …`; another profile's id → BR-S07.

Test: `uv run pytest -q tests/integration/test_commands.py`.

## Step 22: `/cancelar` and `/reprocesar` (S4.9, D96) `[worker]`

Files: `src/invoicebot/pipeline/commands.py`, `src/invoicebot/pipeline/actions.py`, `src/invoicebot/rules/messages_es.py`, tests in `tests/integration/test_commands.py`.

- `/cancelar <id>`: the same cancel function the [Cancelar] button uses (make it callable with an invoice id); outside `CANCELLABLE` → `CANCEL_LATE`.
- `/reprocesar <id>`: `FAILED` with a flow, or `MANUAL_REQUIRED` → `FETCHING_DOCS` (`expected` = the current state) + fetch job + `REPROCESS_STARTED`; `BATCHED` whose batch is `failed` → batch `prepared` (`set_batch_state`) + the batch send job the deliver step enqueues today + `REPROCESS_STARTED`; anything else, or `FAILED` without a flow → `REPROCESS_NOT_ALLOWED`.
- `messages_es.py`:
  - `HELP_CANCELAR = "/cancelar 12: cancela el comprobante #12"`
  - `HELP_REPROCESAR = "/reprocesar 12: reintenta el comprobante #12 si falló"`
  - `CANCEL_LATE = "El comprobante #{id} ya está {state_desc}; ya no se puede cancelar."`
  - `REPROCESS_STARTED = "Vuelvo a intentar el comprobante #{id}."`
  - `REPROCESS_NOT_ALLOWED = "El comprobante #{id} está {state_desc}. /reprocesar solo sirve para comprobantes que fallaron o que esperan sus archivos."`

Test cases: `/cancelar` in `CONFIRMING` → `CANCELLED`, the button's text; in `BATCHED` → `CANCEL_LATE`; `/reprocesar` on `FAILED` with a flow → `FETCHING_DOCS` + fetch job; on `MANUAL_REQUIRED` → the same; on `BATCHED` with a `failed` batch → batch `prepared` + send job; on `DONE` → `REPROCESS_NOT_ALLOWED`; another profile's id → BR-S07 for both.

Test: `uv run pytest -q tests/integration/test_commands.py tests/integration/test_pipeline_actions.py`.

## Step 23: `/pendientes` (S4.8, S4.9, S11.4, D94) `[worker]`

Files: `src/invoicebot/pipeline/commands.py`, `src/invoicebot/store/invoices.py`, `src/invoicebot/queue/outbox.py`, `src/invoicebot/rules/messages_es.py`, tests in `tests/integration/test_commands.py`.

- `list_pending(conn, profile_id, *, limit) -> list[PendingRow]` (`id`, `state`, `waiting_state`, `razon_social`, `total`, `batch_prepared_at`): the profile's invoices in `PENDING_USER`, `WAITING_FOR_USER` or `MANUAL_REQUIRED`, plus `BATCHED` ones whose batch is `prepared` since at least 2 days (`PREPARED_REMIND_DAYS = 2`, S11.4, constant in `commands.py`), oldest first.
- `outbox.requeue_last(conn, invoice_id, rule_id) -> bool`: copies the newest matching row's channel, recipient and payload into a new pending row (for re-sending a parked question).
- `/pendientes` → `PENDING_NONE`, or `PENDING_HEADER` plus one `PENDING_LINE` (or `PENDING_BATCH_LINE`) per row, at most 20 and then `PENDING_MORE`; then `requeue_last` for the first 3 `PENDING_USER` rows with their `waiting_rule_id`.
- `messages_es.py`:
  - `HELP_PENDIENTES = "/pendientes: lo que espera tu respuesta"`
  - `PENDING_NONE = "No tienes comprobantes pendientes."`
  - `PENDING_HEADER = "Esto espera algo de ti:"`
  - `PENDING_LINE = "#{id} · {issuer} · {total} · {state_desc}"` (`issuer` is `razon_social` or `PENDING_NO_ISSUER = "emisor sin leer"`; `total` is the amount or `—`; a parked row shows its waiting state's description)
  - `PENDING_BATCH_LINE = "#{id} · {issuer} · el correo está listo desde el {prepared_at:%d/%m}; falta enviarlo"`
  - `PENDING_MORE = "…y {count} más."`

Test cases (S4.8, S4.9): nothing pending → `PENDING_NONE`; a parked BR-R03 invoice → its line + the BR-R03 question re-queued with its buttons; a `BATCHED` invoice prepared 3 days ago → batch line; prepared 1 day ago → not listed; another profile's invoices → not listed; 25 rows → 20 lines + `…y 5 más.`

Test: `uv run pytest -q tests/integration/test_commands.py`.

## Step 24: One delay type for `retry_schedule` (carried over) `[worker]`

Files: `src/invoicebot/config/models.py`, `tests/unit/test_config_models.py`.

- An `Annotated[timedelta, BeforeValidator(...), PlainSerializer(format_delay)]` delay type next to `parse_delay`; the validator rejects a non-string with `ValueError`, so pydantic reports a `ValidationError`. `EmailInbox.retry_schedule` and `Portal.retry_schedule` become `tuple[Delay, ...]`, and their duplicated validators and serializers go.

Test cases: `["5m", "1h"]` → two timedeltas, round-trips to the same strings; `[5]` → `ValidationError` (not `TypeError`) for both models; `["5x"]` → `ValidationError`.

Test: `uv run pytest -q tests/unit/test_config_models.py`.

## Step 25: The conversation end to end on the fake channel `[worker]`

Files: `tests/integration/test_conversation_fake.py`.

- Using the existing fake channel and fixtures: (1) a photo whose buyer is a DNI → BR-R03 question → press the second flow → summary with [Editar] [Cancelar], `FETCHING_DOCS`; (2) a second photo of the same invoice → BR-E04 with the first's id; (3) `total=12.30` on the first → summary again with the new total; (4) an invoice left in `CONFIRMING`, `park_stale_questions` with `updated_at` moved back 25 h → BR-S08; `/pendientes` → its line and the question again; pressing it → resumes; (5) another profile's `/estado <id>` → BR-S07.
- Docstrings cite R-3.2, R-3.5, R-3.6, R-3.7, R-10.1.

Test: `uv run pytest -q tests/integration/test_conversation_fake.py`.
