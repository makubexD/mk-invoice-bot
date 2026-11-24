# Plan: MVP phase 9r · smart-replies

Roadmap: `docs/plan-mvp.md`. The owner's ask after the Telegram live run (2026-11-23): when a photo is only partly
read, say which required fields were read (with their values) and which are missing, and let the sender either send
a better photo or type only what is missing. Around that, the replies the live run showed to be thin get richer: the
summary names the issuer and how the fields were read, BR-E04 names the invoice it duplicates and offers its status,
`/estado` with no id lists recent invoices, `/pendientes` says what each waiting invoice lacks and re-asks on the
channel the command came from.

In scope: BR-E01 and its re-ask, the edits parser, the routed summary, BR-E04, `/estado` without an id, `/pendientes`
lines and re-asks. Out of scope: an LLM pass to fill the missing fields (D126 already covers `llm` fields); a new photo
replacing the failed invoice (it stays a new invoice; the sender cancels the old one with [Cancelar]); WhatsApp live
(9d2).

Needs from the owner: review of the reply texts (step 1), and the live run (step 12) with a cropped or blurry photo.

Decision this plan proposes (step 1):

- **D188 smart replies:**
  - **BR-E01 names the fields.** When extraction keeps a partial reading, BR-E01 lists the required fields it read,
    with their values, and the ones still missing, and asks for a better photo or only the missing fields, one
    `key=value` per line. With nothing read it keeps the generic text, now one key per line. It carries [Editar] and
    [Cancelar]. A partial answer re-asks with the same text, listing what is still missing (R-3.5).
  - **The parser takes several pairs on one line.** `ruc=… serie=F001 numero=123` on one line parses as three edits,
    so BR-E01's old one-line example no longer fails as a bad RUC.
  - **The summary names the issuer and the sources.** The issuer line is `Emisor: {razon_social} (RUC {ruc_emisor})`
    when the padrón gave a name, and a line `🔎 Leído de: {sources_desc}` says where the required fields came from
    (e.g. `QR` or `QR; texto: fecha, total`).
  - **BR-E04 names the original.** It adds the issuer and total and a [Ver #{id}] button that answers like
    `/estado {id}`.
  - **`/estado` with no id** lists the sender's five newest invoices in `/pendientes` line form, then
    `Para ver uno: /estado 12`. `/cancelar` and `/reprocesar` still need the id.
  - **`/pendientes`** writes `faltan: {fields}` instead of `emisor sin leer` for an invoice waiting for a better photo,
    and re-sends a parked question on the channel the command came from, to the same profile (R-3.7).

## Step 1: D188, spec texts, roadmap row `[inline: decision and spec wording]`

1. `specs/05-open-questions.md`, after D187: D188 worded as above, "(9r plan, proposed)".
2. `specs/06-business-rules.md`:
   - BR-E01 row: trigger "No QR, and OCR is missing required fields"; message cell names the two texts:
     partial: `Leí parte del comprobante #{id}.` + `✅ Leído: {read_desc}` + `❓ Falta: {missing_desc}` +
     `Envíame otra foto más cerca y con buena luz, o escríbeme solo lo que falta, un dato por línea:` +
     `{missing_lines}`; nothing read: `No pude leer este comprobante. Envíame una foto más cerca y con buena luz, o
escríbeme los datos, uno por línea:` + `ruc=…` `serie=F001` `numero=123` `fecha=dd/mm/aaaa` `total=…`;
     buttons [Editar] [Cancelar] (D188).
   - BR-E04 row: `Este comprobante ya lo registré el {created_at:%d/%m} (#{id}): {issuer}, S/ {total}.` + [Ver #{id}].
   - "Summary message": the issuer line and the `🔎 Leído de:` line (D188).
3. `specs/02-requirements.md`: R-3.7 adds "on the channel the `/pendientes` came from"; the `/estado` table row adds
   "without an id, the five newest invoices".
4. `docs/plan-mvp.md`: a `9r` row `smart-replies` (plan `docs/plan-mvp-smart-replies.md`, D188).
5. `docs/plan-telegram.md`, T3 row: drop the `/pendientes` re-ask note (moved here).

Test: `grep -n "D188" specs/05-open-questions.md specs/06-business-rules.md docs/plan-mvp.md && uv run pytest -q tests/unit/test_docs.py`

## Step 2: Several edits on one line `[worker]`

Files: `src/invoicebot/pipeline/edits.py`, `tests/unit/test_edits_parse.py`.

1. In `parse_edits`, split each line into pairs before validating, but only when the line's own first key (before
   its first `=`) is one `_canonical_key` knows: then a further pair starts after whitespace, at a word that
   `_canonical_key` knows followed by `=`, and each value runs to the next pair's start, stripped. A line whose first
   key is unknown stays one pair, exactly as today (one error, nothing split out). Each pair counts as one
   assignment toward `MAX_EDIT_LINES`. One line with one pair behaves exactly as today. A small helper
   (`_split_pairs(line) -> list[str]`) keeps `parse_edits` under the complexity cap.
2. Tests, docstring `D188` (and `R-3.5`):
   - `ruc=20100070970 serie=F001 numero=123 fecha=05/11/2026 total=30.90` → five fields, no errors;
   - `fecha=05/11/2026 total=30.90` → two fields;
   - `total=30.90` alone → one field (unchanged);
   - two lines, one pair each → two fields (unchanged);
   - `ruc=123 total=30.90` → `total` parsed, one BR-E02 error for `ruc` whose value is `123` (not the rest of the line);
   - `nota=hola total=30.90` with an unknown key → the same result as today for `nota=hola total=30.90` when `nota`
     is unknown: only known keys start a pair, so the whole line is one pair for `nota` (assert the current behavior
     for an unknown key, whatever it is, and that `total` is not split out of it).

Test: `uv run pytest -q tests/unit/test_edits_parse.py`

## Step 3: BR-E01 texts and the field lines `[worker]`

Files: `src/invoicebot/rules/messages_es.py`, `src/invoicebot/pipeline/edits.py`, `tests/unit/test_unreadable_text.py`
(new).

1. `messages_es.py`: `MESSAGES["BR-E01"]` becomes the nothing-read text of step 1 (one key per line); add
   `UNREADABLE_PARTIAL` with the partial text and `{id}`, `{read_desc}`, `{missing_desc}`, `{missing_lines}`, cited as
   BR-E01 / D188.
2. `edits.py`: `def edit_key(field: str) -> str` returning the key a sender types for a field (`ruc_emisor` → `ruc`,
   `fecha_emision` → `fecha`, …), from the same table `_canonical_key` uses, so the keys have one home.
3. `edits.py`: `def unreadable_text(invoice_id: int, reading: Reading | None) -> str`: with no reading, or no required
   field read, the generic text; else `UNREADABLE_PARTIAL` where `read_desc` is `"; ".join(f"{FIELD_DESC[name]}
{shown}")` over the read required fields in `REQUIRED` order (`fecha_emision` shown `dd/mm/aaaa`, `total` as
   `S/ 30.90`, `tipo` through `TIPO_DESC`), `missing_desc` is `fields_desc(missing)` and `missing_lines` is one
   `{edit_key}=…` line per missing field (`fecha=dd/mm/aaaa` for the date).
4. Tests, docstring `D188, BR-E01`: no reading → generic text; a reading with every required field but `fecha_emision`
   and `total` → `#14`, the four read fields with values, `fecha, total` missing, lines `fecha=dd/mm/aaaa` and
   `total=…`; a reading with only `ruc_emisor` → five missing lines; `edit_key` round-trips with `_canonical_key` for
   every field in `REQUIRED`.

Test: `uv run pytest -q tests/unit/test_unreadable_text.py tests/unit/test_edits_parse.py`

## Step 4: [Editar] and [Cancelar] answer BR-E01 `[worker]`

Files: `src/invoicebot/pipeline/actions.py`, `src/invoicebot/pipeline/questions.py`,
`tests/integration/test_pipeline_actions.py`.

1. `questions.py`: `def unreadable_buttons(invoice_id: int) -> list[Button]` → [Editar] [Cancelar] with the existing
   edit and cancel actions (same codec as `summary_buttons`).
2. `actions.py`: the edit and cancel actions accept a press while the invoice is in `EXTRACT_FAILED`, and in
   `PENDING_USER` waiting on BR-E01 (`VALID_STATES`, `ASKED_RULES`). [Editar] on `EXTRACT_FAILED` pins the invoice
   (`mark_editing`) and answers with `unreadable_text` for its stored reading, not `EDIT_PROMPT`.
3. Tests, docstring `D188`: [Editar] on an `EXTRACT_FAILED` invoice → pinned as the edit target, reply lists its
   missing fields; [Cancelar] on it → `CANCELLED`; [Editar] on a `PENDING_USER` invoice waiting on BR-E01 → resumes as
   R-3.7 says; a press on a `DONE` invoice → the existing late answer.

Test: `uv run pytest -q tests/integration/test_pipeline_actions.py`

## Step 5: Extraction and the re-ask send the new BR-E01 `[worker]`

Files: `src/invoicebot/pipeline/extract.py`, `src/invoicebot/pipeline/edits.py`,
`tests/integration/test_pipeline_extract.py`, `tests/integration/test_pipeline_edits.py` (or the existing file that
tests `apply_edits`; find it).

1. `extract._finish`, BR-E01 branch: the text is `unreadable_text(invoice.id, failure.reading)` and the message carries
   `unreadable_buttons(invoice.id)`. Other rules unchanged.
2. `edits.apply_edits`, the `EXTRACT_FAILED` and still-missing branch: the same text and buttons over the merged reading.
3. Tests, docstring `D188, BR-E01`: a partial reading (QR-less, OCR missing `fecha_emision` and `total`) → one outbox
   row with the partial text and two buttons; no reading → the generic text with buttons; answering `fecha=05/11/2026`
   only → state stays `EXTRACT_FAILED`, the re-ask lists only `total`; then `total=30.90` → `ROUTING`.

Test: `uv run pytest -q tests/integration/test_pipeline_extract.py <the apply_edits test file>`

## Step 6: Summary names the issuer and the sources `[worker]`

Files: `src/invoicebot/pipeline/summary.py`, `src/invoicebot/rules/messages_es.py`, `tests/unit/test_summary.py` (or
the existing summary test file; find it).

1. `SUMMARY`'s issuer line becomes `Emisor: {issuer}`, where `issuer` is `{razon_social} (RUC {ruc_emisor})` when
   `reading.value("razon_social")` is set, else `RUC {ruc_emisor}`.
2. Add `SUMMARY_SOURCES = "🔎 Leído de: {sources_desc}"` and `SOURCE_DESC` (`qr` → `QR`, `xml` → `XML`, `pdf` → `PDF`,
   `ocr` → `texto`, `llm` → `IA`, `user` → `tú`, `padron` → `padrón`). `sources_desc` groups the `REQUIRED` fields by
   source: the source holding most fields is named alone first; each other source follows as `{desc}: {fields_desc}`,
   `; ` separated. The line goes after the total line, before `{flow_line}`.
3. Tests, docstring `D188`: all from QR with `razon_social` → `Emisor: ACME S.A.C. (RUC 20100070970)` and
   `🔎 Leído de: QR`; no `razon_social` → `Emisor: RUC 20100070970`; QR plus `ocr` for `fecha_emision` and `total` →
   `🔎 Leído de: QR; texto: fecha, total`; the existing inferred and review lines still come out (unchanged tests pass).

Test: `uv run pytest -q <the summary test file> tests/integration/test_pipeline_route.py`

## Step 7: A status button `[worker]`

Files: `src/invoicebot/pipeline/actions.py`, `src/invoicebot/pipeline/commands.py`,
`tests/integration/test_pipeline_actions.py`.

1. A `status` action in `ACTIONS` whose press answers exactly what `/estado {id}` answers (call the function `_estado`
   uses, do not copy it), valid in every state and with no asked rule. The button label is `Ver #{id}`
   (`BUTTON_STATUS` in `messages_es.py`, allowed as a fourth file).
2. Tests, docstring `D188`: a press on `<12>:status` for the sender's own invoice → the same text as `/estado 12`; for
   a `CANCELLED` and a `DONE` invoice → answered too; for another profile's invoice → the answer `/estado` gives a
   stranger's id.

Test: `uv run pytest -q tests/integration/test_pipeline_actions.py`

## Step 8: BR-E04 names the original `[worker]`

Files: `src/invoicebot/pipeline/route.py`, `src/invoicebot/rules/messages_es.py`,
`tests/integration/test_pipeline_route.py`.

1. `MESSAGES["BR-E04"]` as in step 1; `_cancel_duplicate` fills `{issuer}` (`razon_social`, else `RUC {ruc_emisor}`)
   and `{total}` from the new invoice's reading (same natural key) and attaches a `status` button for the original id.
2. Tests, docstring `D188, BR-E04`: a duplicate with `razon_social` → text has the name, `S/ 30.90` and the original
   `#id`, and one button `<original id>:status`; without `razon_social` → `RUC …`.

Test: `uv run pytest -q tests/integration/test_pipeline_route.py`

## Step 9: `/estado` without an id `[worker]`

Files: `src/invoicebot/pipeline/commands.py`, `src/invoicebot/store/invoices.py`, `src/invoicebot/rules/messages_es.py`,
`tests/integration/test_commands.py`.

1. `store/invoices.py`: `list_recent(conn, profile_id, limit)` → the profile's newest invoices by `created_at`, as the
   same row type `list_pending` returns.
2. `/estado` with no argument (only `/estado`; `/cancelar` and `/reprocesar` keep `COMMAND_NEEDS_ID`): one
   `PENDING_LINE` per invoice from `list_recent(..., 5)`, then `ESTADO_HINT = "Para ver uno: /estado 12"`; with no
   invoices, `ESTADO_NONE = "Aún no tengo comprobantes tuyos."`.
3. Tests, docstring `D188`: seven invoices → five lines newest first and the hint; none → `ESTADO_NONE`; another
   profile's invoices never listed; `/cancelar` with no id → `COMMAND_NEEDS_ID` (unchanged).

Test: `uv run pytest -q tests/integration/test_commands.py`

## Step 10: `/pendientes` says what is missing `[worker]`

Files: `src/invoicebot/pipeline/commands.py`, `src/invoicebot/rules/messages_es.py`,
`tests/integration/test_commands.py`.

1. In `_pending_line`, for an invoice whose state or `waiting_state` is `EXTRACT_FAILED`: the issuer cell is
   `PENDING_MISSING = "faltan: {fields_desc}"` over the stored reading's missing required fields (`load_reading`);
   other rows keep `razon_social or PENDING_NO_ISSUER`.
2. Tests, docstring `D188`: an `EXTRACT_FAILED` invoice missing `fecha_emision` and `total` → `faltan: fecha, total`;
   one with no stored reading → every required field listed; a `MANUAL_REQUIRED` row without `razon_social` →
   `emisor sin leer` (unchanged).

Test: `uv run pytest -q tests/integration/test_commands.py`

## Step 11: `/pendientes` re-asks on the command's channel `[worker]`

Files: `src/invoicebot/queue/outbox.py`, `src/invoicebot/pipeline/commands.py`, `tests/integration/test_commands.py`.

1. `requeue_last` takes the `channel` and `to` of the new row from its caller; `_requeue_parked` passes the
   `/pendientes` message's channel and the sender's profile. Payload, buttons and the minimum interval unchanged.
2. Tests, docstring `D188, R-3.7`: a parked invoice that came in on `console` and a `/pendientes` from `telegram` →
   the re-ask row is on `telegram` for the same profile; from the same channel → unchanged behavior.

Test: `uv run pytest -q tests/integration/test_commands.py`

## Step 12: Live run `[inline: live run]`

Deploy this branch's image. From the owner's Telegram: a cropped or blurry photo → BR-E01 names the read and missing
fields; [Editar] → the missing lines; answer one field, then the rest → routed summary with the issuer and the
`Leído de` line. Send the same invoice again → BR-E04 with [Ver #id], press it. `/estado` alone; `/pendientes`. The
owner cancels the console rehearsal invoices #1–#7 and #11 with `/cancelar`.

Test: each answer as the spec texts of step 1 say; a mismatch becomes a finding.

## Step 13: Record the live run `[inline: live results]`

`specs/spikes/smart-replies.md` (new): date, image, what each step-12 item showed, the owner's notes on the texts.

Test: `uv run pytest -q tests/unit/test_docs.py`
