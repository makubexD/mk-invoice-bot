# Plan: MVP phase 3b · ocr (D81, OCR as the primary reader)

Roadmap: `docs/plan-mvp.md` ("Carried over from the `read` review"). Backlog: `specs/04-tasks.md` spike S0.3, S3.5, S3.6, S3.7, moved forward from `read-more`. Requirements: R-2.1, R-2.3, R-2.6, R-2.9, R-2.10, R-2.13, R-2.14, R-9.2, R-9.16. Design: `specs/03-design.md` §5.

**Why (owner, 2026-11-13):** the QR spike reads 50 % of real WhatsApp photos (`specs/spikes/qr.md`), far below S3.3's 95 %. The owner chose OCR on every photo, with the QR confirming it:

- OCR (Tesseract) gives every field it can read.
- When the QR decodes and a field equals the OCR value, that field's confidence becomes 1.0.
- When the QR disagrees, the OCR value stays, and the field is flagged as a conflict and shown on the D80 warning line.
- A field only the QR carries is filled from the QR, and a field only OCR carries comes from OCR.

**Goal:** a photo with no readable QR gets a summary instead of BR-E01 whenever OCR finds the required fields (R-2.13). A photo with a readable QR still gets every field, now cross-checked.

**Needs from the owner first:**

- Tesseract 5 with Spanish on this machine: `! sudo apt install tesseract-ocr tesseract-ocr-spa`.
- The private samples folder (6 WhatsApp photos and the 2026 PDFs with their XMLs) for steps 1 and 11.

Conventions: everything in `docs/plan-mvp-read.md` "Conventions" still applies. In particular:

- Image work runs in the helper process (D78), and Tesseract is a subprocess of that helper, started with an argument list and a timeout.
- Test images are drawn in the test with `cv2.putText` from synthetic values; no image is committed.
- OCR text is never logged and never stored raw (R-9.16, R-12.11).
- Lint for every step: `uv run ruff check src tests scripts migrations && uv run ruff format --check src tests scripts migrations && uv run mypy src && uv run lint-imports`.

Out of scope here: PDF input, the padrón, the synthetic generator, the golden-set report in CI (`read-more`), and the LLM fallback.

## Step 1: OCR spike on real photos (S0.3) `[inline: measurement on private data]`

Files: `specs/spikes/ocr.md` only. The script is throwaway and stays outside the repo, and the images stay in the private folder.

- Run Tesseract (`--psm 4 --oem 1 -l spa`, and `--psm 6` for comparison) on the 6 WhatsApp photos and on the 50 distorted renders of the S0.2 interim set. Compare the preprocessing variants:
  - none (grayscale only);
  - CLAHE then adaptive Gaussian threshold;
  - the same plus deskew;
  - the same plus a 4-point perspective warp.
- Parse each result with throwaway regexes taken from spec 03 §5 step 4. Compare against each invoice's XML where it can be read (`parse_invoice_xml`), or against the decoded QR.
- Record, per variant:
  - per-field accuracy for the six required fields (R-2.13), plus `base`, `service_charge` and `icbper`;
  - required-field accuracy overall;
  - median and maximum seconds per image on this machine.
- Aggregates only: no RUC, store name or amount.
- **Gate:** if the best variant gets under 60 % of required fields right on the real photos, stop the plan and report to the owner (R-2.9 asks for 80 %). At 60 % or above, record the chosen variant and the PSM mode, which become step 4's pipeline.

Done when `specs/spikes/ocr.md` is committed with the chosen preprocessing, or the gate stops the plan.

## Step 2: Record D81 in the specs `[inline: decision text across specs]`

Files: `specs/05-open-questions.md`, `specs/02-requirements.md`, `specs/03-design.md`, `specs/04-tasks.md`, `docs/plan-mvp.md`.

- Spec 05: add D81 with the owner's rule from "Why" above and the spike numbers.
- Spec 02:
  - R-2.1 and R-2.3: OCR runs on every photo, and the QR is tried alongside it.
  - R-2.6: a field may carry a flag, `inferred` or `conflict`.
  - R-9.2: one summary budget of 60 s. The QR-only budget of 20 s goes, because every photo now runs OCR.
- Spec 03 §5: reorder to load → QR cascade and OCR → merge (D81) → score. Step 4's preprocessing becomes the one S0.3 chose (`specs/spikes/ocr.md`): deskew → CLAHE → adaptive Gaussian threshold, with no perspective warp.
- Spec 04: move S0.3, S3.5, S3.6 and S3.7 to the new phase `ocr`, and narrow M5's milestone to PDFs, the padrón and the accuracy report.
- Roadmap: add row 3b `ocr` before `fetch`, and update the carried-over items.

Done when the specs agree with this plan, and `grep -n "IF no valid QR" specs/02-requirements.md` finds nothing.

## Step 3: Tesseract on the host, in the image and in CI `[inline: system dependency in deploy and CI]`

Files: `deploy/Dockerfile`, `.github/workflows/ci.yml` (or whichever workflow runs the tests), `deploy/README.md`.

- Install `tesseract-ocr` and `tesseract-ocr-spa` with `--no-install-recommends` in the runtime image and in the CI job that runs pytest.
- The README names the packages and the `tesseract --version` check.

Test: `podman build -f deploy/Dockerfile -t invoicebot-ocr-check . && podman run --rm --entrypoint tesseract invoicebot-ocr-check --list-langs | grep -q spa`

## Step 4: Field flags replace the inferred-confidence check `[worker]`

Files: `src/invoicebot/extract/fields.py`, `src/invoicebot/extract/qr_payload.py`, `src/invoicebot/pipeline/route.py`.

- `Field` gets `flag: Literal["inferred", "conflict"] | None = None`, and `reading_to_json` and `reading_from_json` carry it.
- A stored reading without the key reads as `None`, so the readings saved by the `read` phase still load.
- `qr_payload.py` sets `flag="inferred"` where it now uses `INFERRED_CONFIDENCE`.
- `route.py` shows "Deducido" for `flag == "inferred"`, not for `confidence == INFERRED_CONFIDENCE`.

Test cases:

| Case                                                | Expected                     |
| --------------------------------------------------- | ---------------------------- |
| R-2.6: round trip of a reading with each flag       | the reading comes back equal |
| R-2.6: a stored payload without `flag`              | loads with `flag=None`       |
| R-2.6: `flag` set to `"evil"`                       | `ValueError`                 |
| D79: an inferred `tipo` from the QR                 | carries `flag="inferred"`    |
| BR-R04: an OCR field at confidence 0.9 with no flag | is not labelled "Deducido"   |

Test: `uv run pytest -q tests/unit/test_extract_fields.py tests/unit/test_extract_qr_payload.py tests/integration/test_pipeline_route.py`

## Step 5: OCR preprocessing and the Tesseract call (S3.5) `[worker]`

Files: `src/invoicebot/extract/ocr.py` (new), `src/invoicebot/config/models.py`, `tests/unit/test_extract_ocr.py` (new); one line each in `config/example.yaml` and `.importlinter`.

- `preprocess(gray) -> ndarray` applies the variant step 1 chose.
- `read_text(image, ocr_cfg) -> OcrText` writes the image to Tesseract's stdin as PNG and runs `tesseract stdin stdout -l <lang> --psm <psm> --oem 1 tsv` through `subprocess.run`, with an argument list and `timeout=ocr_cfg.timeout_s`.
- `OcrText` is a frozen dataclass of lines, each with its words and their confidences (0–1), built from the TSV.
- A timeout or a non-zero exit returns `None` (the caller treats it as "no OCR"). Stderr is discarded.
- Config: `Ocr` gets `psm: int = Field(ge=0, le=13)`. Set the chosen value in `config/example.yaml`.
- Only the helper imports this module, so add it to the `heavy-in-helper-only` contract's forbidden modules.

Test cases:

| Case                                                                                               | Expected                        |
| -------------------------------------------------------------------------------------------------- | ------------------------------- |
| R-2.3: an image drawn with `cv2.putText` (`RUC 20000000001`, `TOTAL S/ 12.50`)                     | both strings are in the lines   |
| R-2.3: a blank image                                                                               | no words, or `None`             |
| R-9.2: `timeout_s` far below the run time (monkeypatch `subprocess.run` to raise `TimeoutExpired`) | `None`                          |
| R-2.6: the TSV parser on a fixed TSV string                                                        | the right words and confidences |

Test: `uv run pytest -q tests/unit/test_extract_ocr.py tests/unit/test_config_models.py`

## Step 6: OCR rule parser and printed URLs (S3.6) `[worker]`

Files: `config/parsing_rules.yaml` (new), `src/invoicebot/extract/ocr_parse.py` (new), `tests/unit/test_extract_ocr_parse.py` (new), and `src/invoicebot/extract/qr_payload.py` (its date and amount helpers become public, so both parsers share one shape).

- The rules file holds the keyword-anchored regexes of spec 03 §5 step 4, plus the printed-URL regex (R-2.10).
- `parse_ocr(text: OcrText, rules) -> OcrReading` returns a `Reading` whose fields have `source="ocr"`, plus `printed_urls`. A `Reading` has no slot for the URLs, and nothing consumes them before the gap issue (`widen-b`), so they stay beside it. Each field's confidence is the mean confidence of the words its match covers.
- The first valid-check-digit RUC near the header is the issuer and the next one is the buyer. `tipo` comes from `FACTURA` or `BOLETA ELECTRÓNICA`. Amounts go through the QR parser's amount shape.
- The serie's last three characters map `O Q D` to `0` and `I L` to `1` before validation (S0.3 finding 1).
- It also reads `base`, `service_charge`, `icbper` and `discounts` when they are printed (R-2.14). `printed_urls` is reduced to `https://host/path` and capped at 3.
- Rules load once through a function that takes the path; there is no module-level state.
- The rules are validated when they load (a pydantic model, and every regex compiles). `scripts/check_config.py` does not check the file: `ocr_parse` depends on `extract.ocr`, which imports OpenCV, and `config/` may not import it (D78).

Test cases (each an `OcrText` built from synthetic lines; parametrize):

| Case                                                               | Expected                                      |
| ------------------------------------------------------------------ | --------------------------------------------- |
| R-2.3: a factura with all required fields                          | the six fields with `source="ocr"`            |
| R-2.3: a boleta with `B001-123`                                    | `tipo` `03`, serie `B001`, numero `123`       |
| S0.3: `FOO1-123` read with letter O for the digits                 | serie `F001`, numero `123`                    |
| R-2.13: a total missing                                            | no `total` field                              |
| R-2.4: an invalid-check-digit 11-digit number before the valid RUC | the valid one is the issuer                   |
| R-2.4: two valid RUCs                                              | the first is the issuer, the second the buyer |
| R-2.14: `RECARGO AL CONSUMO 5.00` and `ICBPER 0.50`                | both read                                     |
| R-2.10: `Consulte su documento en https://x.example/p?q=1`         | `printed_urls` holds `https://x.example/p`    |
| R-2.6: low word confidences                                        | lower the field's confidence                  |

Test: `uv run pytest -q tests/unit/test_extract_ocr_parse.py`

## Step 7: Merge OCR and QR per D81 `[worker]`

Files: `src/invoicebot/extract/merge.py` (new), `tests/unit/test_extract_merge.py` (new).

`merge(ocr: Reading | None, qr: Reading | None) -> Reading`. Fields are compared after normalising: amounts as decimals, dates as ISO dates, and the rest as stripped upper-case text.

Test cases:

| Case                                               | Expected                                         |
| -------------------------------------------------- | ------------------------------------------------ |
| D81: OCR `total` 12.50 at 0.7, QR `total` 12.5     | `12.50`, `source="ocr"`, confidence 1.0, no flag |
| D81: OCR `total` 12.50, QR `total` 21.50           | OCR value, OCR confidence, `flag="conflict"`     |
| D81: `num_doc_adq` only in the QR                  | the QR field                                     |
| D81: `base` only in OCR                            | the OCR field                                    |
| D81: `qr` is None                                  | the OCR reading                                  |
| D81: `ocr` is None                                 | the QR reading                                   |
| D81: both None                                     | an empty reading                                 |
| D81: the QR has only `document_url`                | the OCR fields plus that URL                     |
| D81: an inferred QR `tipo` equal to the OCR `tipo` | confidence 1.0, no flag                          |

Test: `uv run pytest -q tests/unit/test_extract_merge.py`

## Step 8: Conflicts on the warning line (S3.7) `[worker]`

Files: `src/invoicebot/extract/validate.py`, `src/invoicebot/pipeline/route.py`, `tests/unit/test_extract_validate.py`.

- `doubtful_fields` also returns any field with `flag == "conflict"`, and any field below `extraction.review_threshold` (R-2.6, S3.7).
- The summary's `⚠️ Revisa` line (D80) names them. The invoice still goes on (D80).

Test cases:

| Case                                           | Expected                                          |
| ---------------------------------------------- | ------------------------------------------------- |
| D80: a conflict field at confidence 1.0        | doubtful                                          |
| S3.7: an OCR field at 0.5 with threshold 0.8   | doubtful                                          |
| D80: a field at 0.9 with no flag               | not doubtful                                      |
| D80: a summary for a reading with one conflict | contains the warning line with that field's label |

Test: `uv run pytest -q tests/unit/test_extract_validate.py tests/integration/test_pipeline_route.py`

## Step 9: The helper reads both and merges `[worker]`

Files: `src/invoicebot/extract/__main__.py`, `src/invoicebot/pipeline/extract.py`, `config/example.yaml`, `src/invoicebot/app.py`, `tests/integration/conftest.py`, and the three test files below.

- The rules path is `config/parsing_rules.yaml` beside the loaded `config.yaml`: `app.py` passes `settings.config_path.parent / "parsing_rules.yaml"` to `make_extract_handler` as a new `rules_path` argument; tests pass the repo's `config/parsing_rules.yaml`.
- Run as two sub-steps: (a) the wiring, with existing tests still green; (b) the new test cases.
- The helper loads the image once. It runs the QR cascade, then preprocessing and `read_text` and `parse_ocr`, then `merge`. It prints the merged reading.
- The helper takes the OCR config as arguments: lang, psm, timeout, and the rules path.
- The helper result reports `decoder` for the QR and a new `ocr: bool` for whether OCR returned text. `_log_outcome` logs both, never a value (R-9.16).
- `helper_timeout_s` in `config/example.yaml` becomes 50, which covers the OCR timeout (30 s) plus the QR cascade within R-9.2's 60 s.

Test cases (integration, real helper, real Tesseract):

| Case                                                                | Expected                              |
| ------------------------------------------------------------------- | ------------------------------------- |
| R-2.3: a photo drawn with the six required fields as text and no QR | `ROUTING` with OCR-sourced fields     |
| D81: a photo with a QR and the same values as text                  | fields at confidence 1.0              |
| D81: a photo whose printed total disagrees with its QR              | `ROUTING`, `total` flagged `conflict` |
| BR-E01: a blank photo                                               | still ends in BR-E01                  |
| R-9.16: the extraction log on the OCR path                          | no RUC, amount or OCR word            |

Test: `uv run pytest -q tests/integration/test_pipeline_extract.py tests/integration/test_read_logging.py tests/integration/test_conversation_fake.py`

## Step 10: QR payload repairs and the schemaLocation XML `[worker]`

Files: `src/invoicebot/extract/qr_payload.py`, `src/invoicebot/extract/ubl.py`, `tests/unit/test_extract_qr_payload.py`, `tests/unit/test_extract_ubl.py`.

These are the spike S0.2 findings 3 and 4.

QR payload test cases:

| Case                            | Expected                              |
| ------------------------------- | ------------------------------------- |
| D79: date `2026-01-05 13:45:00` | `fecha_emision` `2026-01-05`          |
| D79: `XXX - 20000000001\|01\|…` | the RUC is read                       |
| D79: buyer type `RUC`           | `tipo_doc_adq` `6`, `flag="inferred"` |
| D79: buyer type `DNI`           | `tipo_doc_adq` `1`, `flag="inferred"` |

XML test cases:

| Case                                                                     | Expected                         |
| ------------------------------------------------------------------------ | -------------------------------- |
| R-2.12: an invoice whose root declares `xmlns:schemaLocation="..\x.xsd"` | parses; the attribute is ignored |
| R-2.12: the DOCTYPE rejections                                           | still hold                       |

Test: `uv run pytest -q tests/unit/test_extract_qr_payload.py tests/unit/test_extract_ubl.py`

## Step 11: Accuracy on the private set `[inline: measurement on private data]`

Files: `specs/spikes/ocr.md`.

- Run the real helper (`python -m invoicebot.extract`) on the 6 WhatsApp photos and the 50 renders. Compare each merged reading with its XML.
- Record, under a new heading:
  - required-field accuracy;
  - how many photos reach `ROUTING` instead of BR-E01;
  - conflicts per field;
  - median and maximum helper seconds.
- Aggregates only.
- If required-field accuracy on the real photos is below R-2.9's 80 %, say so in the record and in the roadmap. Don't change the threshold.

Done when the numbers are committed.

## Step 12: Ship the parsing rules and load them at startup `[worker]`

Finish review (code review, High). Files: `src/invoicebot/settings.py`, `src/invoicebot/app.py`, `deploy/Dockerfile`.

- The rules belong to the code, not to the operator's config folder. `Settings` gains `parsing_rules_path: Path = Path("/app/config/parsing_rules.yaml")`; the Dockerfile copies `config/parsing_rules.yaml` there, next to `example.yaml`.
- `app.py` passes `settings.parsing_rules_path` to `make_extract_handler` (drop `PARSING_RULES_FILE`), and calls `load_rules` once in `_runtime` before the worker starts, so a missing or bad file stops startup (fail fast).
- Test: `uv run pytest -q tests/integration/test_conversation_fake.py tests/unit/test_settings.py` (add a settings default case if that file exists; otherwise the run covers it).

## Step 13: OCR failures never crash the helper `[worker]`

Finish review (code review High, security Medium). Files: `src/invoicebot/extract/ocr.py`, `src/invoicebot/extract/ocr_parse.py`, `tests/unit/test_extract_ocr.py`, `tests/unit/test_extract_ocr_parse.py`.

| Case                                                    | Expected                  |
| ------------------------------------------------------- | ------------------------- |
| D81: `read_text` when the `tesseract` binary is missing | `None` (caught `OSError`) |
| D81: `read_text` when Tesseract exits non-zero          | `None`                    |
| D81: a TSV row whose conf or position is not a number   | the row is skipped        |
| R-2.10: printed text `http://[x`                        | no URL, no exception      |

Test: `uv run pytest -q tests/unit/test_extract_ocr.py tests/unit/test_extract_ocr_parse.py`

## Step 14: One canonical numero `[worker]`

Finish review (code review Medium, spike finding 9). Files: `src/invoicebot/extract/qr_payload.py`, `src/invoicebot/extract/ubl.py`, `tests/unit/test_extract_qr_payload.py`, `tests/unit/test_extract_ubl.py`.

- One public helper `canonical_numero(text) -> str` (`str(int(text))`) in `qr_payload.py`, beside the shared date and amount helpers; the QR parser, the UBL reader and the OCR parser (already `str(int(...))`: switch it to the helper) all use it, so the stored `numero` does not depend on the reader.
- Cases: QR `|00000123|` gives `numero` `123`; a UBL `F001-00000123` gives `123`.
- Test: `uv run pytest -q tests/unit/test_extract_qr_payload.py tests/unit/test_extract_ubl.py tests/unit/test_extract_ocr_parse.py tests/unit/test_extract_merge.py`

## Step 15: Amount field names in one place `[worker]`

Finish review (code review Medium). Files: `src/invoicebot/extract/fields.py`, `src/invoicebot/extract/merge.py`, `src/invoicebot/extract/ocr_parse.py`, `tests/unit/test_extract_ocr_parse.py`.

- `AMOUNT_FIELDS` moves to `fields.py`; `merge.py` imports it. `ParsingRules` rejects an `amounts` key outside `AMOUNT_FIELDS` and a `tipo` key outside the supported type codes it maps to.
- Cases: an `amounts` key `totl` fails to load; a `tipo` key mapping to `99` fails to load; the shipped rules load.
- Test: `uv run pytest -q tests/unit/test_extract_ocr_parse.py tests/unit/test_extract_merge.py`

## Step 16: Receipt lines the rules misread `[worker]`

Finish review (code review Medium). Files: `config/parsing_rules.yaml`, `tests/unit/test_extract_ocr_parse.py`.

| Case                                   | Expected                |
| -------------------------------------- | ----------------------- |
| R-2.14: `I.G.V. 18.00% 9.00`           | `igv` `9.00`            |
| R-2.14: `IGV (18%) S/ 9.00`            | `igv` `9.00`            |
| R-2.14: `SUB-TOTAL 50.00` and no total | no `total`              |
| R-2.14: `SERVICIO DE LAVADO 25.00`     | no `service_charge`     |
| R-2.14: `RECARGO POR SERVICIO 5.00`    | `service_charge` `5.00` |

Test: `uv run pytest -q tests/unit/test_extract_ocr_parse.py`

## Step 17: Bounded namespace repair in UBL `[worker]`

Finish review (security High). Files: `src/invoicebot/extract/ubl.py`, `tests/unit/test_extract_ubl.py`.

- The retry strips a backslash `xmlns:` declaration only inside the root start tag, found in a bounded prefix, with bounded patterns; it never rewrites the rest of the document.
- Cases: the schemaLocation invoice still parses; a 2 MB input of repeated `xmlns:a="\\\\…` after the root fails as malformed in under 1 s; a backslash `xmlns:` inside element text is not touched.
- Test: `uv run pytest -q tests/unit/test_extract_ubl.py`

## Step 18: No Tesseract outlives its helper `[worker]`

Finish review (security Medium). Files: `src/invoicebot/pipeline/extract.py`, `tests/integration/test_pipeline_extract.py`.

- Start the helper with `start_new_session=True` and kill its process group (`os.killpg`, SIGKILL) on timeout, crash or cancel. Add `OMP_THREAD_LIMIT=1` to the helper env.
- Case: R-9.2: a helper that started a child is killed on timeout and the child is gone too.
- Test: `uv run pytest -q tests/integration/test_pipeline_extract.py`

Deferred to the roadmap: a cgroup memory cap for helper plus Tesseract on the 2 GB host; a cap on OCR text size and matches per rule; documenting `RLIMIT_FSIZE=0` for Tesseract; catching OpenCV errors in the helper as unreadable; dropping `printed_urls` until `widen-b`.

## Finish

- Run `uv run pytest -q --cov` (branch coverage floor 90 %), then the lint line above.
- Review with `py-code-reviewer` on the whole diff, and `py-security-auditor`, because this phase parses images and OCR text and runs a new subprocess.
- Update `docs/plan-mvp.md`: mark `ocr` done with its date, and write `docs/plan-mvp-fetch.md` with the carried-over `fetch` items.
