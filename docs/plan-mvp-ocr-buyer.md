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
D199 covers this case without it); merging fields across retry reads.

Decision this plan proposes (step 1), **D199 A configured buyer RUC is never the issuer**:

- `owner.ruc` and `work.ruc` are the RUCs the bot files invoices for. When a reading has one of them as `ruc_emisor`
  and a `num_doc_adq` that is not one of them, the two values are swapped (each keeps its confidence and source),
  before the reading is classified, looked up in the padrón and saved.
- A reading whose issuer is a configured RUC and that has no other buyer is left as it is.

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

## Step 4: deploy and re-read #40 `[inline: needs the owner and the live bot]`

Files: `specs/spikes/ocr-buyer.md` (new).

Build the image and `./deploy/deploy-live.sh <tag>`. The owner presses [Anular] on #40 if it is still open; then
`invoice forget 40` releases its key and the owner sends the same photo again. Record (no RUCs, no names): the issuer
is the store, the flow is picked without BR-R03, and the store portal fetch starts. The 9z4 step 3 send follows with
this invoice.

Test: the spike file records the re-read with the issuer and buyer in their places and no flow question.
