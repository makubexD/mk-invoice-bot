# Plan: MVP phase 9t6 · ocr-harden

Roadmap: `docs/plan-mvp.md`, row 9t6. Branch `feat/ocr-harden`, from `feat/portal-guide` (it needs D211's merge rule
and date guard, commit `ca9bae2`). 9t4 `portal-guide` is paused at its step 19 (owner, 2026-11-27: PEBSA's portal does
not hold the test ticket, so its captcha rows wait for a ticket it knows or for 9t5's SUNAT fallback).

What live #58 showed (2026-11-27, owner: "a good case since the quality of the image is not the best"): a La Choza
Náutica Minka ticket billed to RK Export, photographed with a shadow across it and glare on the QR. The bot ended in
BR-E01 asking for the issuer RUC, and its BR-E01 message listed a wrong número and a wrong fecha as read. Probing the
live helper on that photo, pass by pass (aggregates only, no values here):

- **QR**: not decoded. Glare covers its centre; CLAHE, adaptive threshold, shadow division and 2x upscaling on the full
  photo and on a crop decode nothing. Out of scope: no preprocessing recovers it.
- **RUC emisor**: every pass reads the keyword-anchored RUC with one or two confusable characters (`U` for `0`, `/` for
  `7`), so `\d{11}` never matches. One pass's text, mapped `U/O/D/Q→0`, `I/L/|→1`, `/→7`, gives the printed RUC, which
  passes the check digit and is the only candidate in the live padrón (18.5 M rows).
- **Número**: the best pass (fewest missing fields) read it at 0.18 and wrong; another pass read it at 0.61 and right.
  D200 keeps the best pass whole and fills only its gaps, so the better value was dropped.
- **Fecha**: the first pass read it right but with spaces after the separators (`dd- mm- yyyy`), which the date pattern
  refuses; the other passes read the year's `6` as `0`, at confidence 0.0, and that value was kept.
- **Message**: BR-E01 shows any kept field as "✅ Leído", whatever its confidence.

Decision this plan proposes (step 2):

- **D212 OCR reads are hardened against a poor photo** (amends D200). (1) A keyword-anchored RUC whose 11 characters
  are digits or the confusable letters `O U D Q I L | /` is repaired (`O U D Q→0`, `I L |→1`, `/→7`) and kept only when
  the repaired value passes the RUC check digit; a repaired RUC is flagged `inferred`. (2) The pipeline drops an
  `inferred` `ruc_emisor` that the padrón does not hold, so a wrong repair becomes a missing field (BR-E01), never a
  wrong issuer. (3) Across the OCR passes of one photo, each required field takes the value of highest confidence (a
  tie keeps the earlier pass); `serie` and `numero` move as a pair scored by the lower of their two confidences;
  `ruc_emisor` still skips the buyer's RUC and brings that pass's buyer when there is none; fields the QR gives are not
  taken (D200 kept the best pass whole and only filled its gaps). (4) A date may have spaces around its separators.
  (5) BR-E01 lists a printed field (source `ocr` or `llm`) below `extraction.review_threshold` as doubtful, apart from
  the fields read.

Out of scope: QR glare recovery, a "retake without glare" tip (needs a detector that finds an undecodable QR), La
Choza's store and portal files (its printed consulta URL is a later portal-link case), PEBSA (9t4), the SUNAT API (9t5).
The buyer RUC misread was out of scope until live #59 (step 9) showed it ends in BR-R03, not BR-R04: steps 10 to 16.

Live #59 (2026-11-27, the same ticket resent): every pass reads the buyer RUC 2 or 3 digits off, so it fails the check
digit and is dropped; with no buyer the route asks BR-R03 with "boleta sin datos" on a factura. Three passes read the
buyer name close to the padrón's razón social for `owner.ruc`; two of them carry the `CLIENTE:` label after other OCR
text on the line, which the anchored `buyer_label` misses. The owner's rule (2026-11-27): a partial RUC alone is not
enough; a partial RUC **and** the expected name together are, for every flow.

Decision this plan adds (step 10):

- **D213 A misread buyer is matched to a routing RUC by a near RUC and its name** (amends D212). When OCR finds no buyer
  RUC that passes the check digit, but a buyer line holds an 11-digit candidate within 3 digits of a RUC a routing rule
  names (`num_doc_adq`), and the buyer name read on the buyer label's line is similar (≥ 0.7) to that RUC's padrón
  razón social, the buyer is taken as that RUC, flagged `inferred`. Exactly one routing RUC must match, else nothing
  is inferred. An `inferred` buyer is always doubtful: BR-R04 asks with the flow pre-selected, never silent routing.
  `CLIENTE` and `ADQUIRIENTE` labels may follow other text on their line. BR-R03's `{buyer_desc}` for a factura with
  no buyer says the buyer could not be read, not "boleta sin datos".

## Step 1: baseline on a private golden set `[inline: private photos, expected values read by eye]`

Files: `specs/spikes/ocr-harden.md`.

- In the session scratchpad (never the repo): copy the owner's 6 photos from the private samples folder and the live
  photos of #56 and #58 (from the live container's `/data/tmp`, while they exist), and write each one's expected JSON
  (`{"has_qr": ..., "fields": {...}}`, D127) by reading the photo.
- Run `uv run python scripts/accuracy_report.py --set <scratch dir>` against the live config's extraction settings
  (`max_image_px: 2000`, `psm: 4`, `timeout_s: 30`), and record the QR rate, OCR-only rate and per-field rates (no
  values, no file names) as the **before** row of `specs/spikes/ocr-harden.md`, with #58's per-pass findings above.

Test: `npx prettier --check specs/spikes/ocr-harden.md`.

## Step 2: D212 and spec rows `[inline: spec text and owner decisions]`

Files: `specs/05-open-questions.md`, `specs/02-requirements.md`, `specs/06-business-rules.md`.

- `specs/05-open-questions.md`: a D212 row after D211 with the decision above; D200's row gets "(amended by D212)"
  after its title.
- `specs/02-requirements.md`: R-2.3's parser bullets gain the RUC repair and the spaced date (D212); R-2.4 notes a
  repaired RUC is `inferred` and dropped when the padrón lacks it; the D200 retry text says each field takes its
  highest-confidence pass.
- `specs/06-business-rules.md`: BR-E01's text gains the doubtful line (D212).

Test: `npx prettier --check specs/05-open-questions.md specs/02-requirements.md specs/06-business-rules.md`.

## Step 3: a date may have spaces around its separators `[worker:low]`

Files: `config/parsing_rules.yaml`, `src/invoicebot/extract/ocr_parse.py`, `tests/unit/test_extract_ocr_parse.py`.

- `config/parsing_rules.yaml` `date`: allow optional spaces around each separator:
  `'(?<!\d)(?P<value>\d{2}\s?[/-]\s?\d{2}\s?[/-]\s?\d{4}|\d{4}\s?-\s?\d{2}\s?-\s?\d{2})(?!\d)'`.
- `ocr_parse.py` `_read_date`: remove whitespace from the matched value before `parse_date`. Update its docstring
  (D212).
- Tests (`D212`): (a) `FECHA DE EMISION: 29- 10- 2026 01: 32 PM` reads `2026-10-29`; (b) `2026 -10- 29` reads
  `2026-10-29`; (c) `29-10-2026` still reads `2026-10-29`; (d) `29 10 2026` (no separator) reads no date.

Test: `uv run pytest -q tests/unit/test_extract_ocr_parse.py tests/unit/test_rules_parity.py`.

## Step 4: repair a keyword-anchored RUC `[worker]`

Files: `config/parsing_rules.yaml`, `src/invoicebot/extract/ocr_parse.py`, `tests/unit/test_extract_ocr_parse.py`,
`tests/synthetic_rucs.txt`.

- `config/parsing_rules.yaml` `ruc`: the keyword-anchored pattern's value becomes `(?P<value>[\dOUDQIL|/]{11})`
  (the pattern is compiled case-insensitive, so `o`, `u`, `l` match too); the bare 11-digit pattern is unchanged, so
  an unanchored string is never repaired.
- `ocr_parse.py`: a `RUC_DIGIT_FIXES` constant (`O U D Q→0`, `I L |→1`, `/→7`, upper-cased first) and a
  `_repair_ruc(raw) -> tuple[str, bool] | None` helper returning the repaired value and whether it changed, or `None`
  when the result is not 11 digits. `_ruc_matches` repairs each match, keeps it only when `is_valid_ruc` passes, and
  carries the changed flag; `_add_rucs` flags a changed RUC `inferred`. Docstrings name D212.
- `tests/synthetic_rucs.txt`: add `20000000079` (synthetic shape, valid check digit) under a comment saying it is the
  `/→7` repair case.
- Tests (`D212`), with synthetic RUCs only: (a) `RUC: 2OOOOOOOOO1` (letter O) reads `20000000001` flagged `inferred`;
  (b) `RUC: 200000000/9` reads `20000000079` flagged `inferred`; (c) `RUC: 2OOOOOOOOO2` (fails the check digit once
  repaired) reads no RUC; (d) `RUC: 20000000001` is unchanged and unflagged; (e) a bare `2OOOOOOOOO1` without the
  `RUC` keyword is not read; (f) a repaired RUC on a `CLIENTE` line still goes to `num_doc_adq`.

Test: `uv run pytest -q tests/unit/test_extract_ocr_parse.py tests/unit/test_rules_parity.py`.

## Step 5: the padrón drops a repaired RUC it lacks `[worker]`

Files: `src/invoicebot/pipeline/extract.py`, `tests/integration/test_pipeline_extract.py`.

- `_with_padron`: when `ruc_emisor` is flagged `inferred` and `lookup` finds no row, drop `ruc_emisor` from the
  reading before `with_padron`, so the invoice fails with BR-E01 naming it as missing (D212). A found row keeps the
  field (still `inferred`) and fills `razon_social` as today. An unflagged RUC is unchanged whether or not the padrón
  has it.
- Tests (`D212`): (a) an `inferred` issuer RUC in the seeded padrón routes on with the padrón's razón social; (b) an
  `inferred` issuer RUC not in the padrón ends in BR-E01 naming the RUC as missing; (c) an unflagged issuer RUC not in
  the padrón behaves as today. Drive them through the fake helper output the file's tests already use.

Test: `uv run pytest -q tests/integration/test_pipeline_extract.py`.

## Step 6: each field takes its best pass `[worker]`

Files: `src/invoicebot/extract/merge.py`, `src/invoicebot/extract/__main__.py`, `tests/unit/test_extract_merge.py`.

- `merge.py`: replace `fill_missing` with `best_of_reads(kept, others, qr_names) -> Reading` implementing D212 (3):
  for each required field not in `qr_names`, the highest-confidence field among `kept` and `others` (a tie keeps
  `kept`, then the earlier other); `serie` and `numero` as a pair from one pass, scored by the lower confidence, a pass
  with only one of them never splitting a pair; `ruc_emisor` equal to the buyer's `num_doc_adq` skipped, and a taken
  `ruc_emisor` brings that pass's `num_doc_adq` when `kept` has none (D199). Fields not required are `kept`'s; URLs
  stay `kept`'s; with nothing better, `kept` itself is returned. Update the module docstring (D200, D212).
- `__main__.py`: `_filled` calls `best_of_reads`; `_retries`'s docstring says each field takes its best pass.
- Tests (`D212`): (a) `kept` numero 0.18 and an other's 0.61 → the other's `serie` and `numero`; (b) `kept` fecha at
  0.0 and an other's at 0.6 → the other's; (c) a tie keeps `kept`'s; (d) an other with only `numero` does not split
  `kept`'s pair; (e) a field in `qr_names` is never taken; (f) the D199 buyer rule still holds (existing tests, renamed
  to the new function); (g) every existing `fill_missing` case still passes under `best_of_reads` (a missing field
  is the lowest confidence of all).

Test: `uv run pytest -q tests/unit/test_extract_merge.py tests/unit/test_helper.py`.

## Step 7: BR-E01 names doubtful fields `[inline: user-facing copy]`

Files: `src/invoicebot/pipeline/extract.py`, `src/invoicebot/rules/messages_es.py`,
`tests/integration/test_pipeline_extract.py`.

- `messages_es.py`: BR-E01's partial text gains a line between "✅ Leído" and "❓ Falta":
  `⚠️ Dudoso (revísalo): {doubtful_desc}`, shown only when a field is doubtful; doubtful fields leave the "Leído" line.
- `extract.py`: `_failure_reply` takes the review threshold from the handler's config; a required field whose source
  is in `PRINTED_SOURCES` (import it from `pipeline/questions.py`) and whose confidence is below
  `extraction.review_threshold` is doubtful (D212).
- Tests (`D212`): (a) a partial reading with numero at 0.18 shows it on the doubtful line and not on "Leído"; (b) all
  fields at or above the threshold → no doubtful line (today's text, exactly); (c) a QR field at 1.0 is never doubtful.

Test: `uv run pytest -q tests/integration/test_pipeline_extract.py tests/unit/test_messages_es.py`.

## Step 8: measure after `[inline: private photos, expected values read by eye]`

Files: `specs/spikes/ocr-harden.md`.

- Rerun step 1's report on the same scratch set with this branch, and add the **after** row. Rerun the live helper
  probe on #58's photo and note which fields each pass gives now (field names and right or wrong, no values). Any
  per-field rate that dropped is a finding: stop and report it before the live check.

Test: `npx prettier --check specs/spikes/ocr-harden.md`.

## Step 9: live check `[inline: live run, needs the owner's Telegram]`

Files: `specs/spikes/ocr-harden.md`.

Tag the live commit, back up the live config, build and deploy this branch's image. **Ask the owner** to resend the La
Choza Náutica Minka ticket (flow A). Rows: (a) the issuer RUC is repaired, found in the padrón and named with its
razón social; (b) número and fecha are right; (c) any field still below the threshold is shown as doubtful, not read;
(d) the invoice routes to RK Export (Drive) or asks BR-R04 for the buyer; (e) what the fetch step does with the
printed consulta URL (recorded, not fixed here); (f) flow B untouched. Record the rows with no invoice values.

Test: `npx prettier --check specs/spikes/ocr-harden.md`.

## Step 10: D213 and spec rows `[inline: spec text and owner decisions]`

Files: `specs/05-open-questions.md`, `specs/02-requirements.md`, `specs/06-business-rules.md`.

- `specs/05-open-questions.md`: a D213 row after D212 with the decision above; D212's row gets "(amended by D213)".
- `specs/02-requirements.md`: R-2.4 gains the near-RUC-and-name buyer match (D213); R-3.3 also asks for an `inferred`
  buyer.
- `specs/06-business-rules.md`: BR-R04's trigger becomes "the buyer was read by OCR below `review_threshold`, or was
  inferred (D212, D213)"; the `{buyer_desc}` line gains the factura case: `de un cliente que no pude leer` when `tipo`
  is `01` and there is no buyer.

Test: `npx prettier --check specs/05-open-questions.md specs/02-requirements.md specs/06-business-rules.md`.

## Step 11: OCR keeps the buyer name and an unchecked buyer RUC `[worker]`

Files: `config/parsing_rules.yaml`, `src/invoicebot/extract/ocr_parse.py`, `tests/unit/test_extract_ocr_parse.py`.

- `config/parsing_rules.yaml` `buyer_label`: `SEÑOR(ES)` and `SR(ES)` still start the line; `CLIENTE` and
  `ADQUIRIENTE` may follow other text:
  `'^\W*(?:SE[ÑN]OR(?:\(?ES\)?)?|SR(?:\(?ES\)?)?\.?)\s*[:;.,!|]|\b(?:CLIENTE|ADQUIRIENTE)\s*[:;.,!|]'`.
- `ocr_parse.py`: when no buyer RUC is found (no valid buyer-line match and no second valid RUC), add two fields, both
  `source="ocr"`, `flag="inferred"`:
  - `num_doc_adq_unchecked`: the first RUC-pattern match on a buyer line whose repaired value (`_repair_ruc`) is 11
    digits but fails `is_valid_ruc`; confidence as for a RUC.
  - `nombre_adq`: on the first buyer-label line, the text after the label's punctuation, stripped, capped at
    `MAX_BUYER_NAME_CHARS = 100`; only when it holds at least 3 letters; confidence is the mean of its words.
  - A valid buyer RUC adds neither field. Docstrings cite D213.
- Tests (`D213`): (a) `XX AN CLIENTE: RK EXPORT PERU SAC` matches `buyer_label`; (b) `MI CLIENTE` with no punctuation
  does not; (c) `XX SR.: EMPRESA` does not (SR stays line-start); (d) the existing label forms still match; (e)
  `CLIENTE: RC EXPORT PLRU SAC` then `RUC: 20000000001` where that RUC fails the check digit gives
  `num_doc_adq_unchecked` `20000000001` and `nombre_adq` `RC EXPORT PLRU SAC`, both `inferred`, and no `num_doc_adq`;
  (f) a buyer RUC that passes the check digit gives `num_doc_adq` and neither new field; (g) `CLIENTE: -` gives no
  `nombre_adq`; (h) a 150-letter name is cut to 100 characters. Use synthetic RUCs only (compute the check digit in
  the test helper for valid ones).

Test: `uv run pytest -q tests/unit/test_extract_ocr_parse.py`.

## Step 12: the buyer hints take their best pass `[worker]`

Files: `src/invoicebot/extract/merge.py`, `tests/unit/test_extract_merge.py`.

- `best_of_reads`: after the `REQUIRED` groups, when the result has no `num_doc_adq`, take `num_doc_adq_unchecked`
  and `nombre_adq` each from its highest-confidence pass among `kept` and `others` (a tie keeps the earlier pass). A
  result that has `num_doc_adq` carries neither hint (drop them). Docstring cites D213.
- Tests (`D213`): (a) hints read only by another pass are taken; (b) of two passes, the higher-confidence `nombre_adq`
  wins; (c) a tie keeps `kept`'s; (d) when the merged result has `num_doc_adq`, no hint is returned; (e) the existing
  merge tests still pass.

Test: `uv run pytest -q tests/unit/test_extract_merge.py`.

## Step 13: match a misread buyer to a routing RUC `[worker]`

Files: `src/invoicebot/extract/buyer_match.py` (new), `tests/unit/test_extract_buyer_match.py` (new).

- Module of pure functions (no store, no config import), docstring cites D213:
  `match_buyer(reading: Reading, names_by_ruc: Mapping[str, str]) -> Reading`. With `num_doc_adq` present, or either
  hint missing, return `reading` unchanged. Else a RUC in `names_by_ruc` matches when it has 11 digits, differs from
  `num_doc_adq_unchecked` in at most `MAX_BUYER_RUC_DIGIT_DIFFS = 3` positions, and
  `difflib.SequenceMatcher(None, read, expected).ratio() >= MIN_BUYER_NAME_RATIO = 0.7`, where both names are
  upper-cased with everything but `A-Z0-9` removed and `read` is cut to `expected`'s length. Exactly one match: return
  the reading with `num_doc_adq` = that RUC and `tipo_doc_adq` = `6`, both `source="ocr"`, `flag="inferred"`,
  confidence the lower of the two hints', and the two hints removed. Zero or several: unchanged.
- Tests (`D213`), synthetic RUCs and names: (a) a RUC 2 digits off with name `RC EXPORT PLRU-S:ADOS` against
  `RK EXPORT PERU S.A.C.` matches; (b) 3 digits off matches, 4 does not; (c) a near RUC with name
  `CORPORACION BARLOVENTO S.A.C.` does not; (d) the exact name with a RUC 5 digits off does not; (e) two routing RUCs
  that both match give no match; (f) a reading with `num_doc_adq` is returned unchanged; (g) a missing hint returns it
  unchanged; (h) the inferred buyer's confidence is the lower hint's, and the hints are gone.

Test: `uv run pytest -q tests/unit/test_extract_buyer_match.py`.

## Step 14: the extract step matches the buyer; an inferred buyer asks BR-R04 `[worker]`

Files: `src/invoicebot/pipeline/extract.py`, `src/invoicebot/pipeline/route.py`, `tests/integration/test_pipeline_extract.py`, `tests/integration/test_pipeline_route.py`.

- `extract.py`: after `_with_padron`, `_with_buyer_match(conn, reading, cfg)`: collect the 11-digit values in the
  routing rules' `num_doc_adq` lists (resolved as the router resolves `{owner.ruc}` and `{work.ruc}`; reuse that
  function, do not copy it), look each up with `store.padron.lookup`, skip those without a row, and call
  `match_buyer`. Only when the reading has `num_doc_adq_unchecked`, so a normal read does no lookup.
- `route.py` `_doubtful_buyer`: also true for an `ocr` buyer flagged `inferred` (D213). Docstring cites D212, D213.
- Tests (`D213`, real Postgres, synthetic padrón rows): (a) an extract with a near `num_doc_adq_unchecked` and a close
  `nombre_adq` for `owner.ruc` stores `ruc_adq` = `owner.ruc`; (b) the same with a far name stores no `ruc_adq`; (c)
  an inferred buyer at confidence 0.95 routes to BR-R04 with the owner flow pre-selected, not silently; (d) a
  non-inferred buyer at 0.95 still routes silently (BR-R01).

Test: `uv run pytest -q tests/integration/test_pipeline_extract.py tests/integration/test_pipeline_route.py`.

## Step 15: BR-R03 names an unread factura buyer `[inline: user-facing copy]`

Files: `src/invoicebot/rules/messages_es.py`, `src/invoicebot/pipeline/route.py`, `src/invoicebot/pipeline/questions.py`, `tests/unit/test_messages_es.py`.

- `buyer_desc` takes `tipo`: no buyer on a `01` gives `de un cliente que no pude leer`; other tipos keep
  `de ningún cliente (boleta sin datos)`. Both callers pass `reading.value("tipo")`.
- Tests (`D213`): (a) tipo `01`, no buyer → the unread text; (b) tipo `03`, no buyer → boleta sin datos; (c) a RUC
  buyer is unchanged.

Test: `uv run pytest -q tests/unit/test_messages_es.py`.

## Step 16: live check of D213 `[inline: live run, needs the owner's Telegram]`

Files: `specs/spikes/ocr-harden.md`.

Deploy this branch's image (tag and back up the config first, as in step 9). **Ask the owner** to resend the La Choza
Náutica Minka ticket. Rows: (a) BR-R04 asks with RK Export (Drive) pre-selected; (b) the issuer still reads as in step
9; (c) a ticket whose buyer RUC reads cleanly still routes silently (any earlier flow A photo); (d) flow B untouched.
Also re-run step 8's golden set and record that no per-field rate dropped. No invoice values in the file.

Test: `npx prettier --check specs/spikes/ocr-harden.md`.
