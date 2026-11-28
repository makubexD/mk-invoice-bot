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
Choza's store and portal files (its printed consulta URL is a later portal-link case), the buyer RUC misread (a digit
swap no character map can fix; routing asks per BR-R04), PEBSA (9t4), the SUNAT API (9t5).

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
