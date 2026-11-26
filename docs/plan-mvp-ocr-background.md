# Plan: MVP phase 9z5 · ocr-background

Roadmap: `docs/plan-mvp.md`, row 9z5. Branch `feat/ocr-background`, stacked on `feat/html-mail` (9z4).

What the live run showed (2026-11-26, invoice #37, recorded aggregate-only in step 6): a sharp, complete receipt photo
on a wood-grain table ended in BR-E01 "No puedo leer este comprobante". Traced stage by stage in the live container:

- **QR:** zxing and OpenCV both fail, even on a crop scaled ×3; Telegram had resized the photo to 1080×1920, so the
  QR is about 230 px wide.
- **OCR:** `binarize` (CLAHE + adaptive threshold) turns the wood grain and the papers around the receipt into dense
  black streaks; Tesseract (`psm 4`) then splits the page into broken columns and the parser finds no field at all.
- The same photo without `binarize` reads serie, numero, tipo, total and IGV, but takes the buyer's RUC as the issuer:
  the issuer line came out as `RUC!` followed by the store's RUC, which the anchored RUC pattern refuses (`[:.]?`), so the buyer's
  `RUC: …` line was the first anchored match.

No extract code changed since 11-25; #35 (same restaurant, dark plain background) read fine the same day.

Out of scope: a better QR decoder (the 1080-px resize is Telegram's; sending the photo as a file keeps full size, and
the runbook says so in step 6); an LLM reader.

Decision this plan proposes (step 1), **D198 a photo's background does not break OCR**:

- **Crop first.** Before OCR the helper crops the grayscale photo to the receipt: the largest bright region (blur,
  Otsu threshold, morphological close, largest external contour, its bounding box plus a small margin). The crop is
  used only when that region covers between 20 % and 95 % of the image; otherwise the whole photo is read as today.
  The QR is still read on the whole photo.
- **Retry plain.** When the binarized read misses a required field, the helper reads the deskewed crop again without
  `binarize` and keeps the reading with more required fields (a tie keeps the binarized one). Both reads share one
  OCR budget, `extraction.ocr.timeout_s`: the retry gets only the time left, and is skipped under one second.
- **RUC labels as OCR reads them.** The anchored RUC pattern accepts `:`, `;`, `.`, `,`, `!`, `|` or nothing after
  `RUC`, so the issuer's label still anchors when OCR misreads its colon.
- The synthetic golden set gains receipts photographed on a striped, textured background, so the accuracy report
  covers the case.

**Amended after the first private run (owner GO 2026-11-26).** Before deploying, the helper was run over #37 and the
six private photos at the previous live image and at step 5's commit (aggregates in step 10's spike). Photos with
every required field went from 4/7 to 5/7, but the issuer RUC got worse:

- #37 went from BR-E01 to five fields with the **buyer's** RUC as issuer: the crop did not fire (the light wood counts
  as bright, share 1.00), the plain read never printed the issuer's RUC line, and the only `RUC:` line it read sits
  right under `CLIENTE:`. Without a QR nothing catches that.
- A private photo that read in full before regressed: its receipt fills the frame with a shadow down one side, the
  crop (share 0.89) cut about 100 px of paper, the issuer line lost digits, and the buyer's RUC won (the QR conflict
  sends it to review).
- Another photo improved, though its label came out as `RUC]`, a separator step 4 does not accept.

So D198 changes three ways (step 6): the crop is a fallback candidate, never the first read; a RUC printed under a
buyer label is never the issuer; and `[` and `]` join the separators.

## Step 1: D198, the roadmap row and the runbook note `[inline: spec text and an owner decision]`

Files: `specs/05-open-questions.md`, `docs/plan-mvp.md`, `docs/runbook.md`.

- `specs/05-open-questions.md`: add row D198 after D197 with the decision above, marked "(9z5 plan, proposed
  2026-11-26, from live #37)".
- `docs/plan-mvp.md`: add row 9z5 `ocr-background` after 9z4 (plan `docs/plan-mvp-ocr-background.md`, D198).
- `docs/runbook.md`: in the section on sending invoices (or the troubleshooting section for BR-E01), one paragraph:
  a plain dark background reads best, and in Telegram a photo sent as a file (📎 → File) keeps its full size, which
  helps the QR.

Test: `npx prettier --check docs/runbook.md docs/plan-mvp.md specs/05-open-questions.md`

## Step 2: crop to the receipt `[worker]`

Files: `src/invoicebot/extract/image.py`, `tests/unit/test_extract_image.py`.

- `crop_receipt(gray: np.ndarray) -> np.ndarray` in `extract/image.py`: Gaussian blur, Otsu threshold, morphological
  close with a rectangular kernel, the largest external contour, its bounding rectangle grown by a margin and clipped
  to the image. Return that crop when the rectangle's area is between `MIN_RECEIPT_SHARE = 0.20` and
  `MAX_RECEIPT_SHARE = 0.95` of the image, else `gray` unchanged. Every tuning number is a named module constant.
- Docstring cites D198.

Cases (docstrings start with `D198:`; synthetic images built with numpy and `cv2.putText`, no files):

1. A white 600×1200 receipt with black text pasted at (200, 300) on a 1080×1920 background of dark diagonal stripes:
   the crop contains the whole receipt and is at most 15 % larger than it on each side.
2. A plain white image with text (a receipt filling the frame): returned unchanged (same shape).
3. A small white 150×150 square on the striped background (under 20 %): returned unchanged.
4. A uniformly gray image: returned unchanged.

Test: `uv run pytest -q tests/unit/test_extract_image.py`

## Step 3: crop before OCR and the plain retry `[worker]`

Files: `src/invoicebot/extract/__main__.py`, `src/invoicebot/extract/ocr.py`, `tests/unit/test_extract_ocr.py`,
`tests/unit/test_helper.py`.

- `read_text` gains a keyword `timeout_s: float | None = None` that overrides `ocr_cfg.timeout_s` when given.
- In `_read_photo`: the QR is read on `gray` as today; then `straight = deskew(crop_receipt(gray))`, the binarized
  read as today, and when its reading misses a field of `REQUIRED` (from `extract/fields.py`), a second `read_text` on
  `straight` without `binarize`, with `timeout_s` set to what is left of `ocr_cfg.timeout_s` after the first read
  (measured with `time.monotonic()`), skipped when under `MIN_RETRY_S = 1.0`. Keep the reading with more required
  fields; a tie keeps the first. The URL-strip re-read and the merge with the QR reading run on the kept one.
- Keep `_read_photo` under the complexity cap: the retry is its own function.

Cases (docstrings start with `D198:`):

1. `read_text(..., timeout_s=2)` passes 2 to the subprocess timeout, not `ocr_cfg.timeout_s` (fake command, as the
   existing tests do).
2. Helper photo path with a fake reader: a first read with no required field and a second with all of them returns
   the second reading.
3. A first read with every required field never runs the second read.
4. A first read that used up the budget (fake clock past `timeout_s - 1`) skips the retry and returns the first.
5. A tie keeps the binarized reading.

Test: `uv run pytest -q tests/unit/test_extract_ocr.py tests/unit/test_helper.py`

## Step 4: the RUC label as OCR reads it `[worker]`

Files: `config/parsing_rules.yaml`, `tests/unit/test_extract_ocr_parse.py`.

- The anchored `ruc` pattern's separator becomes `[:;.,!|]?` (still optional). Synthetic RUCs only
  (`tests/synthetic_rucs.txt`).

Cases (docstrings start with `D198:`):

1. Text with `RUC! <issuer>` on line 3 and `CLIENTE: …` then `RUC: <buyer>` below: `ruc_emisor` is the issuer and the
   buyer becomes `num_doc_adq`.
2. `RUC| <issuer>` and `RUC; <issuer>` anchor the same way.
3. The existing anchored and bare cases still pass unchanged.

Test: `uv run pytest -q tests/unit/test_extract_ocr_parse.py`

## Step 5: busy backgrounds in the golden set `[worker]`

Files: `scripts/make_golden.py`, `tests/golden/` (regenerated), `specs/spikes/accuracy.md`.

- `make_golden.py` pastes every third receipt photo onto a 1.4× larger canvas of dark diagonal stripes with noise
  (seeded, like the rest), so the set keeps its size and the background cases are reproducible. Regenerate the set
  with the script's usual command and rerun `scripts/accuracy_report.py --set tests/golden --write`.

Test: `uv run python scripts/accuracy_report.py --set tests/golden` passes (QR rate and OCR-only required-field rate at
or above the 2026-11-16 numbers in `specs/spikes/accuracy.md`).

## Step 6: D198 amended `[inline: spec text and an owner decision]`

Files: `specs/05-open-questions.md`.

- D198's row: **Crop first** becomes **Crop as a fallback**: the first read is `binarize(deskew(gray))` as before this
  phase. **Retry plain** becomes **Retries**: when that read misses a required field, the helper tries, in order, the
  plain `deskew(gray)`, then the binarized and the plain crop when the crop differs from the photo, each only with at
  least one second of the shared budget left, and stops at the first reading with every required field; it keeps the
  reading with the most required fields, a tie keeping the earlier one. New **Buyer label**: a RUC printed on a line
  that matches the buyer-label rule (`CLIENTE`, `SEÑOR(ES)`, `SR(ES).`, `ADQUIRIENTE`) or on one of the next two
  lines is the buyer's and never the issuer's; with no other RUC the issuer stays empty, so the invoice gets BR-E01 or
  a question instead of a wrong issuer. **RUC labels** also accept `[` and `]`. Mark it "(amended 2026-11-26 after the
  private run)".

Test: `npx prettier --check specs/05-open-questions.md`

## Step 7: the crop becomes a fallback read `[worker]`

Files: `src/invoicebot/extract/__main__.py`, `tests/unit/test_helper.py`.

- `_read_photo`: the QR on `gray` as today; `straight = deskew(gray)` and the first read on `binarize(straight)`, as
  before this phase. `_retry_unbinarized` becomes `_retries`: when the first reading misses a required field, it tries
  the candidates in order: `straight` plain, then, only when `crop_receipt(gray)` has a different shape from `gray`,
  `binarize(deskew(crop))` and `deskew(crop)` plain. Each candidate runs only with at least `MIN_RETRY_S` of
  `ocr_cfg.timeout_s` left (measured from before the first read) and gets only the time left; the loop stops at the
  first reading with every required field. It keeps the reading with the fewest missing required fields, a tie
  keeping the earlier; the URL-strip re-read runs on the image the kept reading came from. A `cv2.error` or
  `MemoryError` in one candidate skips it.
- The candidates are a lazy generator, so a skipped crop costs nothing.

Cases (docstrings start with `D198:`; fake `read_text`/`parse_ocr`, as the existing helper tests do):

1. A complete first read runs no other read and never calls `crop_receipt`.
2. First read missing a field, plain full read complete: two reads, the plain one kept, `crop_receipt` not called.
3. First and plain reads missing fields, `crop_receipt` returning a smaller image, binarized crop complete: three
   reads, the crop reading kept.
4. `crop_receipt` returning the same shape: only two reads.
5. The budget used up after the first read (fake clock): one read, the first kept.
6. Ties keep the earlier reading; the URL-strip re-read gets the image of the kept reading.

Test: `uv run pytest -q tests/unit/test_helper.py`

## Step 8: a RUC under a buyer label is never the issuer `[worker]`

Files: `config/parsing_rules.yaml`, `src/invoicebot/extract/parsing_rules.py`, `src/invoicebot/extract/ocr_parse.py`,
`tests/unit/test_extract_ocr_parse.py`.

- `parsing_rules.yaml` gains `buyer_label:` (one pattern, compiled like the others):
  `'\b(?:CLIENTE|SE[ÑN]OR\(?ES\)?|SR\(?ES\)?\.|ADQUIRIENTE)\b'`. `ParsingRules` loads it as a required key.
- `_add_rucs`: a RUC match whose line is a buyer-label line or one of the next `BUYER_LINES = 2` lines is a buyer
  match. `ruc_emisor` is the first non-buyer match (none: no `ruc_emisor`); `num_doc_adq` is the first buyer match,
  else the second non-buyer match as today. Matches keep their order (anchored first, then bare).

Cases (docstrings start with `D198:`; RUCs from `tests/synthetic_rucs.txt` only, picked so the bare pattern matches
them):

1. `CLIENTE: EMPRESA SAC` then `RUC: <buyer>` and no other RUC: no `ruc_emisor`, `num_doc_adq` is the buyer.
2. `RUC: <issuer>` at the top, `CLIENTE: …` then `RUC: <buyer>`: issuer and buyer as expected.
3. `SEÑOR(ES): …` with the buyer's RUC on the same line: the buyer, not the issuer.
4. A buyer label three lines above a RUC does not make it the buyer.
5. The existing RUC cases pass unchanged.

Test: `uv run pytest -q tests/unit/test_extract_ocr_parse.py tests/unit/test_golden_set.py`

## Step 9: square brackets after RUC `[worker:low]`

Files: `config/parsing_rules.yaml`, `tests/unit/test_extract_ocr_parse.py`.

- The anchored `ruc` separator becomes `[:;.,!|\[\]]?`. Add `]` and `[` to step 4's parametrized separator test.

Test: `uv run pytest -q tests/unit/test_extract_ocr_parse.py`

## Step 10: D198, a field the QR has is not missing `[inline: spec text from the second private run]`

The second private run (step 9's commit) met the bar on #37 (issuer empty, buyer's RUC as `num_doc_adq`) but not on one
private photo: its first read missed only `ruc_emisor`, which the QR had, so the old merge was right; the retry ran
anyway, and a plain read with the buyer's RUC as issuer replaced it (a QR conflict).

Files: `specs/05-open-questions.md`.

- D198's **Retries**: "misses a required field" becomes "misses a required field that the QR reading does not
  have"; readings are compared after the merge with the QR reading.

Test: `npx prettier --check specs/05-open-questions.md`

## Step 11: retries count the QR reading `[worker]`

Files: `src/invoicebot/extract/__main__.py`, `tests/unit/test_helper.py`.

- `_read_photo` passes `None if hit is None else hit.reading` to `_retries`. `_retries` counts the missing required
  fields of `merge(reading, qr_reading)` (from `extract/merge.py`, as the payload does) both for "is a retry needed"
  and for "is this candidate better". The kept OCR reading is still the unmerged one; the merge for the payload runs
  as today.

Cases (docstrings start with `D198:`):

1. A first read missing only `ruc_emisor` with a QR reading that has it: no retry.
2. A first read missing `ruc_emisor` and `total`, the QR having `ruc_emisor`, a plain read with every field: the plain
   read is kept.
3. With no QR, the step 7 cases still pass unchanged.

Test: `uv run pytest -q tests/unit/test_helper.py`

## Step 12: private photos, deploy and #37 again `[inline: private samples and the live bot]`

Files: `specs/spikes/ocr-background.md` (new).

- Before and after this branch, run the helper over #37's photo and the six private WhatsApp photos from a throwaway
  script outside the repo; record only counts (photos with every required field, issuer RUC right) in the spike file.
- The bar before deploying: no photo whose issuer RUC was right before is wrong after, and #37's issuer is either
  right or missing, never the buyer's. Below the bar, stop: no deploy.
- Build, `./deploy/deploy-live.sh <tag>`, and the owner sends #37's photo again in Telegram; record whether it reads.

Test: the spike file records the before and after counts and the live result.
