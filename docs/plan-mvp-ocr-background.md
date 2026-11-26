# Plan: MVP phase 9z5 · ocr-background

Roadmap: `docs/plan-mvp.md`, row 9z5. Branch `feat/ocr-background`, stacked on `feat/html-mail` (9z4).

What the live run showed (2026-11-26, invoice #37, recorded aggregate-only in step 6): a sharp, complete receipt photo
on a wood-grain table ended in BR-E01 "No puedo leer este comprobante". Traced stage by stage in the live container:

- **QR:** zxing and OpenCV both fail, even on a crop scaled ×3; Telegram had resized the photo to 1080×1920, so the
  QR is about 230 px wide.
- **OCR:** `binarize` (CLAHE + adaptive threshold) turns the wood grain and the papers around the receipt into dense
  black streaks; Tesseract (`psm 4`) then splits the page into broken columns and the parser finds no field at all.
- The same photo without `binarize` reads serie, numero, tipo, total and IGV, but takes the buyer's RUC as the issuer:
  the issuer line came out as `RUC! 20486765454`, which the anchored RUC pattern refuses (`[:.]?`), so the buyer's
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

## Step 6: private photos, deploy and #37 again `[inline: private samples and the live bot]`

Files: `specs/spikes/ocr-background.md` (new).

- Before and after this branch, run the helper over #37's photo and the six private WhatsApp photos from a throwaway
  script outside the repo; record only counts (photos with every required field, issuer RUC right) in the spike file.
- Build, `./deploy/deploy-live.sh <tag>`, and the owner sends #37's photo again in Telegram; record whether it reads.

Test: the spike file records the before and after counts and the live result.
