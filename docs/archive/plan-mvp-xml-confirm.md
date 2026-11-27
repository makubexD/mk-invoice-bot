# Plan: MVP phase 9z8 · xml-confirm

Roadmap: `docs/plan-mvp.md`, row 9z8. Branch `feat/xml-confirm`, from `main` after 9z7's merge. Before step 1 the
owner deploys `main` live (`deploy/deploy-live.sh <tag>`) and checks that the `invoicebot-webhook-watch` journal shows
`webhook ok` (9z7's live check).

What live #43 showed (2026-11-26, `specs/spikes/ocr-buyer.md`): `serie` and `numero` came from OCR at confidence 0.43,
under `review_threshold` 0.8, so the summary said "⚠️ Revisa: serie, número". The portal's XML then matched the
invoice (RUC, serie, número and total, R-5.6), but the reading kept the OCR values and their low confidence. The mail
waited for [Enviar] only because the live config sets `send: review` (D194's trial setting). Between the summary and
the review documents the chat showed nothing while the bot fetched and filed.

The owner's asks from #43: (1) a matching SUNAT XML confirms the fields; (2) the mail goes out by itself when
everything is confirmed, and waits for [Enviar]/[Cancelar] only when a field is missing or still uncertain; (3) show
progress while fetching.

Decisions this plan proposes (step 1):

- **D202 A matching XML confirms the reading.** When the fetch step stores an `ok` result with an XML (which R-5.6
  already matched), each field of the XML's reading updates the stored reading: a field the reading lacks is added
  from the XML; a field whose value agrees with the XML's (`numero` as a number, amounts as decimals, the rest as
  text) is replaced by the XML's field (source `xml`, full confidence, no flag); a field that disagrees keeps the
  reading's value, so it stays doubtful. The updated reading is saved in the same transaction as the `ok` attempt.
- **D203 `send: if_doubtful`.** A third mailer setting next to D194's `review` and `auto`: the mail is held for
  review (BR-D19) only when the reading still has a doubtful field (the summary's "⚠️ Revisa" set: below
  `review_threshold`, `conflict`, a failed R-2.4 check, or a date in the future or older than
  `max_invoice_age_days`) or misses a required field; otherwise it is sent at once, as `auto`. A held BR-D19 says
  why, with the summary's `⚠️ Revisa: {fields_desc}` line under its heading. The live flow B moves from `review` to
  `if_doubtful` once this ships.
- **BR-F16 Documents found.** On an `ok` fetch the bot posts `📄 Encontré {docs_desc}. Lo archivo…`
  (`{docs_desc}`: `el PDF y el XML`, `el PDF` or `el XML`), plus, when D202 cleared doubtful fields, a second line
  `✅ El XML de SUNAT confirma: {fields_desc}`. It goes through the outbox in the `ok` transaction.

Out of scope: a progress message per fetcher attempt (BR-F13 and the wait notices already cover the slow paths), and
changing `auto` or `review`.

## Step 1: D202, D203, BR-F16 and the roadmap row `[inline: spec text and owner decisions]`

Files: `specs/05-open-questions.md`, `specs/06-business-rules.md`, `docs/plan-mvp.md`.

- `specs/05-open-questions.md`: add rows D202 and D203 after D201 with the decisions above, each marked "(9z8 plan,
  proposed 2026-11-26, from live #43)".
- `specs/06-business-rules.md`: add BR-F16 to the fetch table after BR-F15 with the text above; in BR-D19's text,
  note the `⚠️ Revisa` line when the mail is held by `if_doubtful`.
- `docs/plan-mvp.md`: add row 9z8 `xml-confirm` after 9z7 (plan `docs/plan-mvp-xml-confirm.md`, D202, D203).

Test: `npx prettier --check docs/plan-mvp.md specs/05-open-questions.md specs/06-business-rules.md`

## Step 2: the XML confirms a reading `[worker]`

Files: `src/invoicebot/extract/confirm.py` (new), `tests/unit/test_extract_confirm.py` (new).

- Module docstring: updates a photo reading with the fields a matching invoice XML confirms (R-5.6, D202).
- `def confirmed_by_xml(reading: Reading, xml: Reading) -> Reading`: returns a copy (`dataclasses.replace`) whose
  `fields` are built per D202: for each `name, field` in `xml.fields`, when `name` is not in `reading.fields` the XML
  field is added; when the values agree it replaces the reading's field; otherwise the reading's field stays.
  Agreement: `numero` compares as an integer (both parse with `int()`, else they disagree), the amount fields
  (`total`, `base`, `igv`) through `Reading.amount` (both not `None` and equal), every other field by exact `value`.
  Reading-only fields keep their place and value; `document_url`, `printed_url` and `url_strip` are the reading's.
- `def confirmed_names(before: Reading, after: Reading) -> tuple[str, ...]`: the names whose field changed source to
  `xml` from another source, in `after.fields` order (BR-F16's second line).

Cases (docstrings start with `D202:`; synthetic values, RUC `20000000003`):

1. Reading `serie` `F001` at 0.43 (`ocr`), XML `F001`: the result's `serie` is source `xml`, confidence 1.0, flag
   `None`.
2. Reading `numero` `00123`, XML `123`: agree; the result carries the XML's field.
3. Reading `total` `49.9`, XML `49.90`: agree by amount.
4. Reading `fecha_emision` `2026-10-27`, XML `2026-10-28`: the reading's field stays (source `ocr`).
5. Reading without `igv`, XML `igv` `7.61`: added from the XML.
6. A reading-only field (`num_doc_adq` absent from the XML) stays, and `document_url` is the reading's.
7. A reading field flagged `conflict` that agrees with the XML: replaced, flag `None`.
8. `confirmed_names` on case 1 plus an unchanged `ruc_emisor` already `xml`: returns `("serie",)`.

Test: `uv run pytest -q tests/unit/test_extract_confirm.py`

## Step 3: one home for the review set `[worker]`

Files: `src/invoicebot/pipeline/summary.py`, `tests/unit/test_summary.py`.

- Add `def review_fields(reading: Reading, extraction: Extraction, today: date) -> tuple[str, ...]`, docstring "the
  fields the summary's ⚠️ Revisa line names (R-2.6, D80, D203)": the body `summary_text` uses today (doubtful fields
  plus `fecha_emision` when the date is missing or fails `check_date`), moved out unchanged.
- `summary_text` calls it; its output does not change.

Cases (docstrings start with `D203:`):

1. A reading with every field at 1.0 and today's date: `()`.
2. `serie` at 0.43: `("serie",)`.
3. No `fecha_emision`: `("fecha_emision",)`.
4. A date 400 days old with `max_invoice_age_days` 365: contains `fecha_emision`.

Test: `uv run pytest -q tests/unit/test_summary.py`

## Step 4: the `if_doubtful` setting `[worker]`

Files: `src/invoicebot/config/models.py`, `src/invoicebot/pipeline/intake_config.py`,
`tests/unit/test_config_models.py`, `tests/unit/test_intake_config.py`.

- `MailerSink.send: Literal["review", "auto", "if_doubtful"] = "auto"`; its comment adds D203's one-line meaning.
- `intake_config`'s review-flow function counts a mailer on `review` or `if_doubtful` (both can post BR-D19, so
  [Enviar] must be accepted for both); its docstring cites D203.

Cases (docstrings start with `D203:`): `send: if_doubtful` validates; `send: maybe` is rejected; a flow on
`if_doubtful` is in the review flows.

Test: `uv run pytest -q tests/unit/test_config_models.py tests/unit/test_intake_config.py`

## Step 5: the deliver gate holds only doubtful mail `[worker]`

Files: `src/invoicebot/pipeline/deliver.py`, `src/invoicebot/pipeline/review.py`,
`src/invoicebot/rules/messages_es.py`, `tests/integration/test_pipeline_deliver.py`.

- In `_deliver`, replace `reviewed = sinks.mailer.send == "review"` with a helper `_held(mailer, reading,
extraction, today) -> tuple[str, ...] | None`: `None` when the mail is not held (`auto`, or `if_doubtful` with an
  empty `review_fields` and every `REQUIRED` value present); otherwise the doubtful names (`()` for `review`).
  `today` comes from `owner_today(conn, cfg.owner.timezone)` read with the invoice. The `_was_reviewed` check, the
  `reviewed` reset and the file removal stay as they are for a held mail.
- `review_text` takes the doubtful names; when they are not empty, BR-D19 carries `SUMMARY_REVIEW` under its first
  line (a `{review_line}` placeholder, empty otherwise).
- Docstrings cite D203.

Cases (docstrings start with `D203:`; real Postgres, the fake channel):

1. `if_doubtful`, every field confident: the mail is sent through the first mode; no BR-D19.
2. `if_doubtful`, `serie` at 0.43: BR-D19 is posted with `⚠️ Revisa: serie`; nothing is sent.
3. `if_doubtful`, after [Enviar] on that mail: it is sent (D194's digest path).
4. `review`, every field confident: still held, BR-D19 without a Revisa line.

Test: `uv run pytest -q tests/integration/test_pipeline_deliver.py`

## Step 6: the fetch step confirms and says so `[worker]`

Files: `src/invoicebot/pipeline/fetch.py`, `src/invoicebot/rules/messages_es.py`,
`tests/integration/test_pipeline_fetch.py`.

- In `_record_result`'s `ok` branch, before the state change: when `result.files` has an `xml` file, parse it
  (`parse_invoice_xml`, already matched by R-5.6), build `confirmed_by_xml(reading, xml_reading)` and
  `save_reading` it; then queue BR-F16 through the outbox (the same reply path `_wait_notice` uses), with the
  `✅ El XML de SUNAT confirma` line when `confirmed_names` intersects the summary's `review_fields`. All in the
  existing transaction.
- `messages_es.py`: `BR-F16`, `BR-F16_confirmed` and the `{docs_desc}` texts (reuse `DOC_DESC` for the single
  kinds).
- Docstrings cite D202 and BR-F16.

Cases (docstrings start with `D202:` or `BR-F16:`):

1. An `ok` result with PDF and a matching XML, reading `serie` at 0.43: the stored reading's `serie` is source `xml`
   at 1.0; BR-F16 says `el PDF y el XML` and confirms `serie`.
2. An `ok` result with only a PDF: the stored reading is unchanged; BR-F16 says `el PDF`, no second line.
3. An XML that confirms only fields that were already confident: BR-F16 has no second line.

Test: `uv run pytest -q tests/integration/test_pipeline_fetch.py`

## Step 7: runbook and live run `[inline: host config and a live check with the owner]`

Files: `docs/runbook.md`, `config/example.yaml`, `specs/spikes/xml-confirm.md` (new).

- `config/example.yaml`: the D194 comment at `send: auto` gains `if_doubtful` (holds only when a field needs review).
- `docs/runbook.md`: where flow B's `send` lives in the host config and what `if_doubtful` does.
- After the merge and deploy, the owner sets the live flow B to `send: if_doubtful` and sends a receipt; record in
  the spike file whether BR-F16 arrived, the XML confirmed `serie`/`número`, and the mail went out without [Enviar]
  (this is also 9z4 step 3's live HTML send).

Test: `npx prettier --check docs/runbook.md specs/spikes/xml-confirm.md`
