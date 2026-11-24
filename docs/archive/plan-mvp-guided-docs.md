# Plan: MVP phase 9s · guided-docs

Roadmap: `docs/plan-mvp.md`. The owner's notes after the 9r live run (`specs/spikes/smart-replies.md`, 2026-11-23):
the bot should guide the sender when it needs something, and stay quiet when it does not. A summary read fully from
the QR has no buttons, so it no longer looks like the bot is waiting. The first "waiting for the mail" notice becomes
a docs-needed reply that says what the flow still needs, where the bot looked and when it looks again, and offers
three ways forward. The portal question can come at that point, before the chain ends, and a URL printed on the
ticket (or a QR link the bot may not fetch) is offered as the portal. A bare `/cancelar` or `/reprocesar` lists the
sender's invoices as buttons, and `/estado` stops showing rule ids.

In scope: the 9s roadmap row. Out of scope: the portal probe and the draft portal file (9t `portal-probe`); asking
which address the merchant mails to and learning per issuer which source works (owner's notes, a later phase:
recorded in D189 as "later"); [Corregir] picking one field by button (a later phase, D189 "later"); WhatsApp live
(9d2).

Needs from the owner: review of the reply texts (step 1), and the live run on Telegram (step 13) with a ticket that
has no known portal.

Decision this plan proposes (step 1):

- **D189 guided docs (9s part):**
  - **The summary's buttons only on doubt.** [✏️ Corregir] and [✖️ Anular] go under the summary only when a required
    field's source is `ocr` or `llm` (read from the printed text). A summary whose required fields all came from the
    QR, an XML, the padrón or the sender carries no buttons; `/cancelar {id}` still works. `SUMMARY_MOVING` drops "Toca
    un botón solo si algo está mal." when there are no buttons.
  - **The docs-needed reply (BR-F13).** The first wait on the mailbox (today `FETCH_WAITING_MAIL`) sends instead:
    `⏳ Comprobante #{id}: me falta {docs_desc} para archivarlo.` + `Ya busqué:` + the BR-F07 bullets of the attempts
    so far + `Vuelvo a mirar el correo a las {next_at:%H:%M}.` + `Si lo tienes, me ayudas:`, with buttons
    [📎 Te envío el PDF] [🔗 Enseñar el portal] [⏳ Espero el correo]. [🔗] is shown only when the issuer has no portal
    entry, no taught link, and `gaps.teach_via_chat` is on. The portal-wait text (`FETCH_WAITING`) is unchanged.
  - **Its buttons.** [📎] moves the invoice from `FETCHING_DOCS` to `MANUAL_REQUIRED` (rule BR-F13, so the fetch job
    stops) and answers `BR-F13_upload`: `Envíame el PDF aquí, como archivo, y lo completo.`; the upload path (R-1.5,
    D101) does the rest and `/reprocesar` resumes the search. [⏳] changes nothing and answers `BR-F13_wait`:
    `Listo. Sigo buscando el correo y te aviso cuando llegue.` [🔗] asks for the portal (next point).
  - **Asking for the portal (BR-F02, BR-F14).** [🔗] offers the reading's printed URL, else its QR link when that host
    is not one `qr_url` may fetch, as BR-F14: `¿Es este el portal de {razon_social}? {url}` [Sí] [No] [No sé]. [Sí]
    teaches that URL (BR-S06, the owner approves it before use); [No] sends BR-F02's question; [No sé] answers
    `BR-F02_dont_know`. With no URL to offer, [🔗] sends BR-F02's question directly. BR-F02 at the end of the chain
    stays as it is (once per issuer, when the gap is first raised). The PORTAL_MISSING gap is still raised only at
    `MANUAL_REQUIRED` (R-11.4); only the question may come earlier. Nothing here blocks the fetch chain.
  - **The printed URL.** The OCR parser keeps the first web address printed on the ticket (`https://…` or a bare
    `host.tld/path`, normalized to `https://`) as `Reading.printed_url`. It is only ever offered to the sender, never
    fetched by the bot (the sender's [Sí] sends it to BR-S06's owner approval).
  - **Command buttons.** A bare `/cancelar` or `/reprocesar` lists up to five of the sender's newest invoices that the
    command can act on, as buttons titled `#{id} · {serie}-{numero}` (or `#{id}`) whose id is the command with that
    id (`/cancelar 12`). A press whose id starts with `/` runs exactly like the typed command, with the same checks.
    With none, it answers `No tienes comprobantes que pueda {cancelar|reprocesar}.`
  - **`/estado` without rule ids.** The last notice reads `Último aviso: {text}`; with no text the line is left out.
    The hint names the newest listed id: `Para ver uno: /estado {id}`.
  - **Later (not 9s):** ask which address the merchant mails to and skip the mail wait when it never comes; learn per
    issuer which source worked and put it in the gap issue; [Corregir] picks one field by button. 9t keeps its row.

## Step 1: D189, spec texts, roadmap row `[inline: decision and spec wording]`

1. `specs/05-open-questions.md`, after D188: D189 worded as above, "(9s plan, proposed)".
2. `specs/06-business-rules.md`:
   - BR-F02 row: trigger also "or the sender pressed [🔗 Enseñar el portal] (BR-F13) and there is no URL to offer";
     the gap stays tied to `MANUAL_REQUIRED`.
   - New rows BR-F13 (docs-needed reply, its texts, its buttons and their answers `BR-F13_upload`, `BR-F13_wait`) and
     BR-F14 (the printed-URL offer and its three answers), worded as in D189.
   - The mailbox-wait line under the BR-F07 notes points to BR-F13.
   - "Summary message": buttons only when a required field came from the printed text (D189).
3. `specs/02-requirements.md`: R-3.4 says the summary carries [Corregir] [Anular] only when a required field was read
   from the printed text (D189); R-11.4 adds that the portal question may also be asked from BR-F13 before the chain
   ends; the `/cancelar` and `/reprocesar` rows add "without an id, the sender's invoices as buttons".
4. `docs/plan-mvp.md`, 9s row: link this plan (`docs/plan-mvp-guided-docs.md`) and D189.

Test: `grep -n "D189" specs/05-open-questions.md specs/06-business-rules.md specs/02-requirements.md docs/plan-mvp.md && uv run pytest -q tests/unit/test_docs.py`

## Step 2: The summary's buttons only on doubt `[worker]`

Files: `src/invoicebot/pipeline/questions.py`, `src/invoicebot/pipeline/validate.py`,
`src/invoicebot/rules/messages_es.py`, `tests/unit/test_questions.py`.

1. `questions.py`: `summary_buttons(invoice_id, reading)` returns `[]` unless a field named in `REQUIRED` (from
   `invoicebot.extract.fields` or wherever `REQUIRED` lives today; import it, do not copy it) has `source` in a new
   module constant `PRINTED_SOURCES = frozenset({"ocr", "llm"})`; otherwise the same two buttons as today.
2. `validate.py`: both callers pass `inputs.reading`, and the summary's last line is `SUMMARY_MOVING` when buttons are
   returned, else `SUMMARY_MOVING_QUIET`.
3. `messages_es.py`: `SUMMARY_MOVING_QUIET = "⏳ Lo estoy archivando."` next to `SUMMARY_MOVING`.
4. Tests, docstring `D189` (and `R-3.4`): every required field from `qr` → `[]`; one required field from `ocr` → two
   buttons with today's titles and ids; one from `llm` → two buttons; a non-required field from `ocr` with all required
   from `qr` → `[]`; required fields from `qr` and `user` → `[]`.

Test: `uv run pytest -q tests/unit/test_questions.py tests/integration/test_pipeline_validate.py`

## Step 3: `/estado` without rule ids `[worker]`

Files: `src/invoicebot/rules/messages_es.py`, `src/invoicebot/pipeline/status.py`,
`src/invoicebot/pipeline/commands.py`, `tests/integration/test_commands.py`.

1. `messages_es.py`: `STATUS_RULE = "Último aviso: {text}"`; delete `STATUS_RULE_ONLY`; `ESTADO_HINT = "Para ver uno:
/estado {id}"`.
2. `status.py`: the last-notice line is `STATUS_RULE.format(text=last)` when there is a last text, and no line at all
   otherwise (the rule id is never shown).
3. `commands.py`, `_list_recent`: the hint is `ESTADO_HINT.format(id=rows[0].id)` (the newest listed invoice; use the
   row's id attribute as `PendingRow` names it).
4. Tests, docstring `D189` (and `S4.9`): `/estado {id}` for an invoice with a stored rule notice shows `Último aviso:`
   and no `BR-` text; one with a rule id but no notice text shows no `Último aviso` line and no `BR-`; `/estado` with
   no id ends with `Para ver uno: /estado {newest id}`. Update any existing assertion on the old texts to the new ones
   (same intent, new wording; do not delete a test).

Test: `uv run pytest -q tests/integration/test_commands.py && ! grep -rn "STATUS_RULE_ONLY" src tests`

## Step 4: `Reading.printed_url` `[worker]`

Files: `src/invoicebot/extract/fields.py`, `src/invoicebot/extract/merge.py`, `tests/unit/test_extract_fields.py`,
`tests/unit/test_extract_merge.py`.

1. `fields.py`: `Reading` gains `printed_url: str | None = None` (last, with the default, so every existing
   constructor keeps working). `reading_to_json` writes `"printed_url"`. `reading_from_json` accepts the keys `fields`,
   `document_url` and, optionally, `printed_url` (an old stored reading has no `printed_url` and reads as None); a
   non-string, non-null value raises `ValueError` like `document_url`; any other key still raises.
2. `merge.py`: the merged reading's `printed_url` is the OCR reading's (a QR reading has none); update the docstring.
3. Tests, docstring `D189`: round trip with a `printed_url`; an old payload without the key → None; a payload with
   `printed_url: 5` → `ValueError`; an unknown extra key still → `ValueError`; `merge(ocr_with_url, qr)` keeps the OCR
   URL; `merge(None, qr)` → None; `merge(ocr_with_url, None)` keeps it.

Test: `uv run pytest -q tests/unit/test_extract_fields.py tests/unit/test_extract_merge.py`

## Step 5: The OCR parser reads the printed URL `[worker]`

Files: `src/invoicebot/extract/ocr_parse.py`, `tests/unit/test_extract_ocr_parse.py`.

1. `ocr_parse.py`: a helper `_read_printed_url(searched: str) -> str | None` finds the first web address in the text:
   either `https?://` followed by a host, or a bare host (labels of letters, digits and hyphens, at least one dot, a
   last label of 2–24 letters) followed by `/` and a path. The match stops at whitespace and drops trailing `.,;:)`.
   The scheme becomes `https://` (an `http://` one too), the host is lowercased, the whole URL is capped at
   `MAX_PRINTED_URL_CHARS = 200` (longer → None). A host that ends in `sunat.gob.pe` is skipped (that is SUNAT's
   generic query page, not the issuer's portal). An e-mail address is not a URL (`@` before the host → skip). The
   regex is a named module constant. `parse_ocr` passes the result as `Reading(..., printed_url=...)`.
2. Tests, docstring `D189`:
   - `Consulte su comprobante en https://sy.ecomprobantes.pe/pebsa` → `https://sy.ecomprobantes.pe/pebsa`;
   - `sy.ecomprobantes.pe/pebsa.` (no scheme, trailing dot) → `https://sy.ecomprobantes.pe/pebsa`;
   - `HTTP://Facturas.Example.PE/consulta` → `https://facturas.example.pe/consulta`;
   - `www.sunat.gob.pe/consulta` and then `https://portal.example.pe/x` → the second one;
   - `ventas@example.pe` alone → None;
   - a text with no address → None; a 250-char URL → None;
   - the existing fields of a fixture text are unchanged when a URL is added to it.

Test: `uv run pytest -q tests/unit/test_extract_ocr_parse.py`

## Step 6: BR-F13's text and the new button titles `[worker]`

Files: `src/invoicebot/rules/messages_es.py`, `src/invoicebot/pipeline/manual_message.py`,
`tests/unit/test_manual_message.py`.

Step 1 already added `BR-F13`, `BR-F13_upload`, `BR-F13_wait` and `BR-F14` to `MESSAGES` and the catalog (spec
parity needs them with the spec rows). `BR-F13` is `"⏳ Comprobante #{id}: me falta {docs_desc} para archivarlo.\n"
"{searched_lines}\nVuelvo a mirar el correo a las {next_at:%H:%M}.\nSi lo tienes, me ayudas:"`, where `next_at` is
a `datetime`. Do not change those texts.

1. `messages_es.py`: button titles next to the others: `BUTTON_SEND_PDF = "📎 Te envío el PDF"`,
   `BUTTON_TEACH_PORTAL = "🔗 Enseñar el portal"`, `BUTTON_WAIT_MAIL = "⏳ Espero el correo"`, `BUTTON_NO = "No"`,
   `BUTTON_DONT_KNOW = "No sé"` (reuse `BUTTON_YES` for [Sí]; if a `No` title already exists, reuse it). Each title is
   at most 20 characters. Add `SEARCHED_HEADER = "Ya busqué:"`. Delete `FETCH_WAITING_MAIL` only in step 8.
2. `manual_message.py`: rename `_fetcher_lines` to `fetcher_lines` (public, docstring says BR-F07 and BR-F13 use it)
   and add `docs_needed_text(invoice_id: int, reading, flow, attempts, directory, next_at: datetime) -> str` that
   formats `MESSAGES["BR-F13"]` with `docs_desc(...)` of the flow's required docs (the same helper and argument BR-F07's
   `manual_docs_desc` uses) and `searched_lines` = `SEARCHED_HEADER` and the `fetcher_lines(...)` joined by newlines.
   With no bullet (only a `qr_url` with no link ran), `searched_lines` is empty and the empty line it leaves is
   dropped from the result (no blank line in the text).
3. Tests, docstring `D189` (and `BR-F13`): attempts `qr_url: no_url`, `portal_registry: no_portal` → the text names
   the docs, has `Ya busqué:` and `• Portal del comercio: no lo conozco`, then `Vuelvo a mirar el correo a las 14:05.`
   for `next_at` 14:05, and ends with `Si lo tienes, me ayudas:`; attempts with only `qr_url: no_url` → no
   `Ya busqué:` line and no blank line; BR-F07's text is unchanged by the rename (an existing test covers it; run it).

Test: `uv run pytest -q tests/unit/test_manual_message.py tests/unit/test_rules_parity.py`

## Step 7: The portal offer `[worker]`

Files: `src/invoicebot/pipeline/portal_offer.py` (new), `src/invoicebot/pipeline/intake.py`,
`tests/integration/test_pipeline_portal_offer.py` (new).

1. `portal_offer.py`, module docstring "Ask the sender for the issuer's portal, offering a URL from the ticket first
   (BR-F14, BR-F02, D189).":
   - `OFFER_BUTTON_PREFIX = "u"`; button ids `u<invoice_id>:yes`, `u<invoice_id>:no`, `u<invoice_id>:dk`, matched by a
     full-match pattern like `teach._PRESS_PATTERN`.
   - `offer_url(reading) -> str | None`: `reading.printed_url`, else `reading.document_url` when its host is not
     SUNAT's (reuse `SUNAT_DOMAIN` from `fetchers/qr_url.py`), else None. No allowed-hosts check: [🔗] is only shown
     when the issuer has no portal entry, so a QR link to a known portal does not reach here in practice.
   - `async def ask_portal(conn, addressee, invoice_id, reading) -> None`: with an offer URL, queue
     BR-F14 (rule `BR-F14`, the three buttons, `{razon_social}` = the reading's or `UNKNOWN_ISSUER`); without one,
     queue BR-F02's question (rule `BR-F02`, so a typed answer goes through `teach.answer`).
   - `async def handle_press(conn, msg, setup: TeachSetup) -> None`: parse the id; the invoice must belong to the
     sender and the sender's newest question must be that invoice's BR-F14 (`outbox.last_question`, as
     `teach.answer` does), else log and do nothing. `yes` → `teach.teach(...)` with the offered URL recomputed from
     the stored reading; `no` → queue BR-F02's question for that invoice; `dk` → queue `BR-F02_dont_know`.
2. `intake.py`: a press whose id starts with `OFFER_BUTTON_PREFIX` and `roles.teach is not None` goes to
   `portal_offer.handle_press`, next to the teach prefix branch. Check the prefix cannot match an invoice button id
   (those start with a digit), a payment one or a teach one.
3. Tests (real Postgres), docstring `D189` (and `BR-F14`):
   - `ask_portal` with a printed URL → one outbox row, rule `BR-F14`, the URL in the text, three buttons;
   - `ask_portal` with no printed URL and a `document_url` on a SUNAT host → BR-F02's question;
   - `ask_portal` with no printed URL and a `document_url` on another host → BR-F14 offering it;
   - `ask_portal` with no URL at all → BR-F02's question;
   - [Sí] → a `proposed` taught link with the offered URL and BR-S06's owner alert;
   - [No] → BR-F02's question queued, no taught link;
   - [No sé] → `BR-F02_dont_know`, no taught link;
   - a press from another profile, or after a newer question, → nothing queued.

Test: `uv run pytest -q tests/integration/test_pipeline_portal_offer.py tests/integration/test_intake.py`

## Step 8: The fetch sends BR-F13 at the first mailbox wait `[worker]`

Files: `src/invoicebot/pipeline/fetch.py`, `src/invoicebot/pipeline/questions.py`,
`src/invoicebot/rules/messages_es.py`, `tests/integration/test_pipeline_fetch.py`.

1. `questions.py`: action constants `SEND_PDF_ACTION = "send_pdf"`, `TEACH_PORTAL_ACTION = "teach_portal"`,
   `WAIT_MAIL_ACTION = "wait_mail"`, `RULE_DOCS_NEEDED = "BR-F13"`, and `docs_needed_buttons(invoice_id, offer_portal:
bool) -> list[Button]` → [📎] [🔗 only when `offer_portal`] [⏳].
2. `fetch.py`: in `_record_result`, the first wait of the email inbox fetcher (where `_waiting_text` returns
   `FETCH_WAITING_MAIL` today) queues `docs_needed_text(...)` with rule `BR-F13` and `docs_needed_buttons`; `next_at`
   is the job's `run_after` as a `datetime` in `cfg.timezone` (the owner's zone; reuse the helper other notices use,
   if there is one), formatted by the message's `{next_at:%H:%M}`. `offer_portal` is True when `gaps.teach_via_chat` is on, `portal_for(ruc_emisor, directory)` is
   None and `taught_links.exists(conn, ruc)` is False (False with no RUC). Every other fetcher keeps `FETCH_WAITING`.
   Remove `_WAITING_TEXTS` if it is left with no entry, and delete `FETCH_WAITING_MAIL` from `messages_es.py`.
3. Tests, docstring `D189` (and `BR-F13`): the inbox's first `not_found_yet` with an unknown issuer → one BR-F13 row
   with three buttons, the `next_at` time and the no-portal bullet; the same with a taught link for the issuer → two
   buttons (no [🔗]); the second wait → no new message; a portal fetcher's first wait → `FETCH_WAITING` text, no
   buttons. Update the existing assertions on `FETCH_WAITING_MAIL`.

Test: `uv run pytest -q tests/integration/test_pipeline_fetch.py && ! grep -rn "FETCH_WAITING_MAIL" src tests`

## Step 9: BR-F13's buttons `[worker]`

Files: `src/invoicebot/pipeline/docs_help.py` (new), `src/invoicebot/pipeline/actions.py`,
`tests/integration/test_pipeline_docs_help.py` (new).

1. `docs_help.py`, module docstring "The actions behind BR-F13's buttons (D189).": three functions with the
   `Action` signature from `actions.py` (move the `Action` alias to `questions.py` or a small shared module if
   importing it from `actions.py` would be circular):
   - `send_pdf`: `set_state(conn, id, MANUAL_REQUIRED, rule_id="BR-F13", expected=FETCHING_DOCS)` and reply
     `BR-F13_upload`;
   - `wait_mail`: reply `BR-F13_wait`, no state change;
   - `teach_portal`: load the reading and call `portal_offer.ask_portal`, addressed to the sender's channel and
     profile.
2. `actions.py`: register the three actions in `ACTIONS`; `VALID_STATES`: all three `{FETCHING_DOCS}`;
   `ASKED_RULES`: all three `{RULE_DOCS_NEEDED}`; `wait_mail` and `teach_portal` join `READ_ONLY_ACTIONS` (they must
   not clear a pending edit).
3. Tests (real Postgres), docstring `D189` (and `BR-F13`):
   - [📎] on a `FETCHING_DOCS` invoice → `MANUAL_REQUIRED` with rule `BR-F13`, `BR-F13_upload` queued; the pending
     fetch job then runs and changes nothing (`_is_current` is False);
   - [⏳] → `BR-F13_wait` queued, state unchanged;
   - [🔗] with a printed URL in the stored reading → BR-F14 queued; without one → BR-F02 queued;
   - [📎] on an invoice already `DELIVERING` → the stale-button answer (BR-S02), no state change.

Test: `uv run pytest -q tests/integration/test_pipeline_docs_help.py tests/integration/test_pipeline_actions.py`

## Step 10: List the invoices a command can act on `[worker]`

Files: `src/invoicebot/store/invoices.py`, `tests/integration/test_invoices_store.py`.

1. `invoices.py`: `list_recent_in(conn, profile_id, states: Collection[str], limit) -> list[PendingRow]`, the same
   columns and order as `list_recent` but only invoices whose state is in `states` (plain SQL, `state = ANY(%s)`).
   Share the column list and row mapping with `list_recent` instead of copying them.
2. Tests, docstring `D189` (and `S4.9`): three invoices in different states → only the ones in `states`, newest
   first; `limit` caps; another profile's invoices never appear; an empty `states` → `[]`.

Test: `uv run pytest -q tests/integration/test_invoices_store.py`

## Step 11: Bare `/cancelar` and `/reprocesar` list buttons `[worker]`

Files: `src/invoicebot/pipeline/commands.py`, `src/invoicebot/pipeline/intake.py`,
`src/invoicebot/rules/messages_es.py`, `tests/integration/test_commands.py`.

1. `messages_es.py`: `COMMAND_PICK = "¿Cuál quieres {verb}?"`, `COMMAND_PICK_NONE = "No tienes comprobantes que
pueda {verb}."`, `COMMAND_PICK_TITLE = "#{id} · {serie}-{numero}"`, `COMMAND_PICK_TITLE_SHORT = "#{id}"`;
   `COMMAND_NEEDS_ID` stays for the other commands.
2. `commands.py`: with an empty `arg`, `/cancelar` lists `list_recent_in(..., CANCELLABLE, ESTADO_RECENT)` and
   `/reprocesar` lists the states `_requeue` acts on (`FAILED`, `MANUAL_REQUIRED`, `BATCHED`; one constant, used by
   both). Each row is a button whose id is `f"/{command} {id}"` and whose title is `COMMAND_PICK_TITLE`, or the short
   one when serie or numero is missing or the title is over 20 characters. No rows → `COMMAND_PICK_NONE`. A non-empty
   `arg` behaves exactly as today. `COMMAND_BUTTON_PREFIX = "/"` lives here.
3. `intake.py`: a press whose id starts with `COMMAND_BUTTON_PREFIX` is parsed with `parse_command(msg.button_id)` and
   run through `handle_command` with the same `CommandContext` the typed command gets (extract a small
   `_command_context(roles, timezone)` helper so the two paths build it once). A press that does not parse does
   nothing.
4. Tests, docstring `D189` (and `S4.9`): bare `/cancelar` with two cancellable invoices and one `DONE` → two buttons,
   ids `/cancelar {id}`; pressing one cancels that invoice; bare `/reprocesar` with a `MANUAL_REQUIRED` invoice → its
   button, the press answers `REPROCESS_STARTED`; bare `/cancelar` with nothing cancellable → `COMMAND_PICK_NONE`; a
   pressed `/cancelar {id}` of another profile's invoice → the same BR-S07 refusal as typing it; `/estado` (bare) is
   unchanged.

Test: `uv run pytest -q tests/integration/test_commands.py tests/integration/test_intake.py`

## Step 12: Architecture notes `[worker:low]`

Files: `docs/architecture.md`.

1. In the fetch section, the first mailbox wait sends BR-F13 with its buttons; [📎] moves `FETCHING_DOCS` →
   `MANUAL_REQUIRED` (add that edge to the state diagram, labelled `BR-F13 [📎]`).
2. In the intake section, the two new press prefixes: `u` (portal offer, BR-F14) and `/` (command buttons).

Test: `grep -n "BR-F13" docs/architecture.md && grep -n "BR-F14" docs/architecture.md`

## Step 13: Live run on Telegram `[inline: needs the owner and the live bot]`

1. Rebuild and redeploy the image (runbook), then with the owner on Telegram:
   - a QR-only ticket → summary with no buttons;
   - a ticket whose OCR fills a required field → summary with [Corregir] [Anular];
   - a ticket from an issuer with no portal entry (e.g. the PEBSA ticket) → BR-F13 with three buttons and the next
     time; [🔗] → BR-F14 with the printed URL; [Sí] → the owner's BR-S06 alert; [⏳] and [📎] on other invoices;
   - bare `/cancelar` and `/reprocesar` from Telegram's menu → buttons; `/estado` shows no `BR-` text.
2. Record the results in `specs/spikes/guided-docs.md` (new), in the form of `specs/spikes/smart-replies.md`, with
   any owner notes for 9t or a later phase. Fix what the run breaks before the finish.

Test: `test -f specs/spikes/guided-docs.md && grep -n "BR-F13" specs/spikes/guided-docs.md`
