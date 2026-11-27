# Plan: MVP phase 9z6 · ocr-buyer

Roadmap: `docs/plan-mvp.md`, row 9z6. Branch `fix/ocr-buyer`, stacked on `feat/html-mail` (9z4), whose step 3 (the
live send) waits for this fix.

What the live run showed (#40, 2026-11-26): a restaurant receipt made out to the employer was read with the roles
swapped: the employer's RUC as the issuer and the store's as the buyer. The bot then asked which flow to file it in
(BR-R03, the store's RUC matches no rule) and found no store portal (it looked up the employer). Re-reading the photo
locally showed why:

- The first OCR read was right: the employer's `RUC:` line sits under `CLIENTE:`, so D198's buyer-line rule kept it
  as the buyer. That read missed the date, so the retry cascade ran.
- The block-mode retry (`psm` 6) read the label line as `NOIA CLIENTE: …` (noise from the photo's edge), so
  `buyer_label`, anchored at the line start, did not match; and it read the store's `RUC` as `Rue?`, so the only
  keyword-anchored RUC was the employer's, which the parser takes first as the issuer.
- That retry was the only read with every required field, so D198 kept it whole, swapped roles included.

Out of scope: loosening `buyer_label` for a noise prefix (a one-word prefix would also match `ATENCION AL CLIENTE:`;
D199 covers this case without it). Merging fields across retry reads was out of scope at first; the
live re-read (#41, 2026-11-26) brought it in as D200 below.

Decision this plan proposes (step 1), **D199 A configured buyer RUC is never the issuer**:

- `owner.ruc` and `work.ruc` are the RUCs the bot files invoices for. When a reading has one of them as `ruc_emisor`
  and a `num_doc_adq` that is not one of them, the two values are swapped (each keeps its confidence and source),
  before the reading is classified, looked up in the padrón and saved.
- A reading whose issuer is a configured RUC and that has no other buyer is left as it is.

Amendment after #41 (owner GO 2026-11-26). Telegram's copy of the same receipt came in as a different file and its QR
did not decode, even upscaled ×3. The first read missed only the date; the block-mode retry read
`27/10/2026 22:10:16` but missed serie and número. Each read lacked one field, the tie kept the first, and the date
that had been read was dropped: BR-E01 asked the owner for a date printed on the photo. #40 had passed only because
its block-mode read happened to have everything.

Decision this amendment proposes (step 4), **D200 Retry reads fill each other's gaps**:

- After the retries, the reading kept (the one with the fewest missing required fields, as in D198) takes each
  required field it lacks from the earliest other OCR read that has it, with that read's value, confidence and
  source. A field the kept reading has is never replaced.
- `serie` and `numero` are one invoice number: they are filled together, from the earliest read that has both, and
  only when the kept reading has neither.
- `ruc_emisor` is never filled with the kept reading's `num_doc_adq` value, so a buyer RUC read as an issuer in
  another pass does not come back as the issuer (D198 buyer label, D199).
- The retries go on while the kept reading, filled this way and merged with the QR reading, still misses a required
  field; they stop as soon as it has them all.

## Step 1: D199 and the roadmap row `[inline: spec text and an owner decision]`

Files: `specs/05-open-questions.md`, `docs/plan-mvp.md`.

- `specs/05-open-questions.md`: add row D199 after D198 with the decision above, marked "(9z6 plan, proposed
  2026-11-26, from live #40)".
- `docs/plan-mvp.md`: add row 9z6 `ocr-buyer` after 9z5 (plan `docs/plan-mvp-ocr-buyer.md`, D199).

Test: `npx prettier --check docs/plan-mvp.md specs/05-open-questions.md`

## Step 2: the swap `[worker]`

Files: `src/invoicebot/extract/parties.py` (new), `tests/unit/test_extract_parties.py` (new).

- Module docstring: decides which read RUC is the issuer and which the buyer (R-2.4, D199).
- `def with_own_buyer(reading: Reading, own_rucs: frozenset[str]) -> Reading`: when `reading.value("ruc_emisor")` is
  in `own_rucs` and `reading.value("num_doc_adq")` is set and not in `own_rucs`, return a copy
  (`dataclasses.replace`) whose `fields` has the two `Field` objects swapped: the old `num_doc_adq` field becomes
  `ruc_emisor` and the old `ruc_emisor` field becomes `num_doc_adq`. Every other field, field order aside, and
  `document_url`, `printed_url`, `url_strip` are unchanged. Otherwise return `reading` itself.

Cases (docstrings start with `D199:`; synthetic 11-digit RUCs, e.g. `OWN = "20000000001"`, `WORK = "20000000002"`,
`STORE = "20000000003"`):

1. Issuer `WORK`, buyer `STORE`, `own_rucs={OWN, WORK}`: issuer is `STORE`, buyer is `WORK`, and each keeps the
   confidence it had under its old name.
2. Issuer `STORE`, buyer `WORK`: the same object is returned (`is`).
3. Issuer `OWN`, buyer `WORK` (both configured): returned unchanged.
4. Issuer `WORK`, no `num_doc_adq`: returned unchanged.
5. `tipo_doc_adq`, `total` and `document_url` are the same after a swap.

Test: `uv run pytest -q tests/unit/test_extract_parties.py`

## Step 3: the extract handler applies it `[inline: one signature change across five call sites, over the four-file cap]`

Files: `src/invoicebot/pipeline/extract.py`, `src/invoicebot/app.py`, `tests/integration/conftest.py`,
`tests/integration/test_pipeline_extract.py`, `tests/integration/test_flow_dni.py`.

- `make_extract_handler` already has six arguments (the lint cap). Add a frozen dataclass `ExtractInputs` in
  `pipeline/extract.py` with `extraction: Extraction`, `media: MediaLimits`, `rules_path: Path` and
  `own_rucs: frozenset[str]`, and pass it in place of the first three of those arguments.
- In `handle`, right after `_classify` turns the helper JSON into a reading and before `check_issuer`, apply
  `with_own_buyer(reading, inputs.own_rucs)`. `_classify` takes `own_rucs` for this; the BR-E01 partial reading
  goes through it too, so a PARSER_MISS sighting names the store, not the employer.
- `app.py` builds `ExtractInputs(cfg.extraction, cfg.limits.media, settings.parsing_rules_path,
frozenset({cfg.owner.ruc, cfg.work.ruc}))`; the three test call sites build it from their own config.
- New integration case in `tests/integration/test_pipeline_extract.py` (docstring `D199:`): a fake helper reading
  with the work RUC as issuer and a store RUC as buyer is saved with `ruc_emisor` the store's and `ruc_adq` the work
  RUC, and the invoice moves to `ROUTING`.

Test: `uv run pytest -q tests/integration/test_pipeline_extract.py tests/integration/test_flow_dni.py`

## Step 4: D200 `[inline: spec text and an owner decision]`

Files: `specs/05-open-questions.md`.

- Add row D200 after D199 with the decision above, marked "(9z6 plan, proposed 2026-11-26, from live #41; owner:
  no workaround, the bot must not ask for a field it read)".

Test: `npx prettier --check specs/05-open-questions.md`

## Step 5: fill a reading's gaps from other reads `[worker]`

Files: `src/invoicebot/extract/merge.py`, `tests/unit/test_extract_merge.py`.

- Add `def fill_missing(kept: Reading, others: Sequence[Reading]) -> Reading` (docstring cites D200). For each
  required field (`REQUIRED` from `extract/fields.py`) whose value `kept` lacks, take the `Field` from the first
  reading in `others` whose value for it is set. `serie` and `numero` are one unit: when `kept` has neither, take
  both from the first reading in `others` that has both; when `kept` has one of them, fill neither. Skip a
  `ruc_emisor` candidate equal to `kept.value("num_doc_adq")` and look further. Return a copy
  (`dataclasses.replace`) with the filled fields added after the existing ones; `document_url`, `printed_url` and
  `url_strip` stay `kept`'s. With nothing to fill, return `kept` itself.

Cases (docstrings start with `D200:`; `Field(value, "ocr", confidence)`, synthetic RUCs `20000000001`,
`20000000010`):

1. `kept` lacks `fecha_emision`, `others[0]` has it at 0.7: the result has that value at 0.7, and every field of
   `kept` is unchanged.
2. `kept` has `total` `"49.90"`, `others[0]` has `total` `"12.50"`: the result keeps `"49.90"`, and `kept` is returned
   itself (`is`).
3. `others[0]` and `others[1]` both have `fecha_emision` with different values: the result has `others[0]`'s.
4. `kept` lacks `serie` and `numero`; `others[0]` has only `serie`, `others[1]` has both: both come from
   `others[1]`.
5. `kept` has `serie` but no `numero`; `others[0]` has both: `numero` stays missing.
6. `kept` has `num_doc_adq` `20000000010` and no `ruc_emisor`; `others[0]` has `ruc_emisor` `20000000010`,
   `others[1]` has `20000000001`: the result's `ruc_emisor` is `20000000001`.
7. `kept` lacks `igv` (not required) and `others[0]` has it: `igv` stays missing.
8. `others` empty: `kept` is returned itself.

Test: `uv run pytest -q tests/unit/test_extract_merge.py`

## Step 6: the retries fill gaps across reads `[worker]`

Files: `src/invoicebot/extract/__main__.py`, `tests/unit/test_helper.py`.

- In `_retries`, keep every OCR reading read (the first one included, `None` skipped) in read order. The loop
  condition and the "is better" comparison stay on the best reading alone, except that the loop stops when
  `_missing_count(fill_missing(best_reading, the other readings), qr_reading)` is 0. The function returns the best
  triple with its reading replaced by `fill_missing(best_reading, the other readings)`; the image it returns is
  still the best read's. Update the docstring (D200). `_missing_count` and `merge` are unchanged.

Cases (docstrings start with `D200:`; use the existing `_Spy`, `_fields`, `SMALL`; with `_Spy` the QR reads
nothing):

1. #41's shape: reads `_fields` without `fecha_emision`, then `_fields("ruc_emisor", "tipo")`, then
   `_fields("ruc_emisor", "tipo")`, then `_fields` without `serie` and `numero`: the payload has all six required
   fields, `fecha_emision` comes from the fourth read, `serie` from the first, and exactly four reads ran with no
   crop.
2. Reads: first without `fecha_emision` and `total`, second `_fields("total")`, third `_fields("fecha_emision")`:
   the payload has all six, and exactly three reads ran.
3. No read has `fecha_emision` (six reads, each `_fields` of the other five): the payload lacks only
   `fecha_emision`.
4. The existing D198 tests still pass unchanged.

Test: `uv run pytest -q tests/unit/test_helper.py tests/unit/test_extract_merge.py`

## Step 7: deploy and re-read the receipt `[inline: needs the owner and the live bot]`

Files: `specs/spikes/ocr-buyer.md` (new).

Build the image and `./deploy/deploy-live.sh <tag>`. #40 is already cancelled and forgotten (2026-11-26). The owner
presses [Cancelar] on #41; then `invoice forget 41 --yes` releases its key and the owner sends the same photo again,
typing nothing. Also record the Telegram webhook that held #41's photo (pending 1, no error) until it was set again. Record (no RUCs, no names): the issuer
is the store, the flow is picked without BR-R03, and the store portal fetch starts. The 9z4 step 3 send follows with
this invoice.

Test: the spike file records the re-read with the issuer and buyer in their places, the date read from the photo,
no question to the owner and no flow question.
