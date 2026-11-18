# Plan: MVP phase 3 · read (thin slice, part 1)

Roadmap: `docs/plan-mvp.md` (D64). Backlog: `specs/04-tasks.md` S3.1, S3.2, S3.3, S3.11, S3.12, S7.3, S7.7, S4.1, S4.2, spike S0.2. Requirements: R-2.1, R-2.2, R-2.4, R-2.6, R-2.7, R-2.12, R-2.13, R-3.1, R-3.4, R-5.6, R-9.16, R-9.17. Design: `specs/03-design.md` §4, §5, §12.

**Goal (M1):** send a photo of an invoice with a QR code; the bot replies with what it read and the flow it picked. A photo with no readable QR gets BR-E01, which asks for the data as text. Nothing is fetched or filed yet.

Conventions for every step:

- Everything in `docs/plan-mvp-foundation.md` "Conventions" still applies (Python 3.12, `uv`, psycopg 3 async, plain-SQL Alembic, flat `tests/unit/` and `tests/integration/`, every test docstring cites its requirement or rule ID, no real RUCs, names or photos).
- New packages: `src/invoicebot/extract/` (pure reading and checking; imports nothing from `api`, `events`, `pipeline`, `queue`, `store` or `channels`) and `src/invoicebot/routing/` (pure). Step 1 adds both to `.importlinter`.
- Test data is built in the test: QR images are written with `cv2.QRCodeEncoder`, XML is an inline string, RUCs come from `tests/synthetic_rucs.txt` (add values there when a test needs more issuers or buyers). No image or XML file is committed in this phase.
- **Image reading runs in a helper process (D78).** Lint forbids imports inside functions (ruff `PLC0415`), so `extract/image.py` and `extract/qr.py` import `cv2` and `zxingcpp` at module top, and the bot process never imports those two modules: the extract handler starts `python -m invoicebot.extract <file>` for each photo and reads one JSON object from its stdout. Memory returns to the system when the helper exits, and a malformed image can only crash the helper. Step 1 adds an import-linter contract that keeps `invoicebot.pipeline`, `invoicebot.app`, `invoicebot.routing` and `invoicebot.store` from importing `invoicebot.extract.image` or `invoicebot.extract.qr`.
- The helper is started with `asyncio.create_subprocess_exec` (an argument list, never a shell), holds `HEAVY` (`queue/worker.py`) while it runs, and is killed after `extraction.helper_timeout_s`.
- Logs carry ids, rule ids, field names and confidences, never an extracted value (R-9.16).
- Lint for every step: `uv run ruff check src tests scripts migrations && uv run ruff format --check src tests scripts migrations && uv run mypy src && uv run lint-imports`.

Out of scope here (each has its phase in the roadmap): OCR, PDF input, the padrón, the synthetic receipt generator and SUNAT validation (`read-more`); the unknown-buyer question, edits, duplicates, timeouts and commands (`widen-b`); fetching (`fetch`).

Known shortcuts in this phase, each marked with a `shortcut:` comment in the code:

- `routing.unmatched: ask` is answered like `reject` (BR-R07) until S4.3 lands in `widen-b`.
- The summary has no [Editar] / [Cancelar] buttons until S4.5 and S4.9 land in `widen-b`.
- `natural_key` stays empty until S4.6 (duplicates) in `widen-b`, so the same invoice sent twice makes two rows.
- A no-QR photo stops at BR-E01; the `campo=valor` reply is handled in `widen-b`.
- A doubtful value (a date out of bounds, a failed check) is a warning line in the summary and the invoice goes on; the question-and-answer step (BR-E03, BR-E09 → `CONFIRMING`) replaces it in `widen-b`, when replies are handled (D80). No invoice enters `CONFIRMING` in this phase.

Open points for the owner (they do not block steps 1–12):

- Step 13 (spike S0.2) first runs on an interim set: the owner's 2026 PDFs rendered to images and distorted (D77). It runs again on real photos when the owner has sent at least 10 through WhatsApp; any receipt with a QR counts, and no XML is needed.
- Step 1 adds dependencies. Each goes through the `py-add-package` skill, which checks CVEs, maintenance and licence first.

## Step 1: Dependencies and package boundaries `[inline: dependency vetting and an import contract]`

Files: `pyproject.toml` (+ `uv.lock`), `.importlinter`, `src/invoicebot/extract/__init__.py` and `src/invoicebot/routing/__init__.py` (docstrings only).

- Add `zxing-cpp`, `opencv-python-headless` and `lxml` with `py-add-package`, and `lxml-stubs` to the dev group (mypy is strict). If `zxingcpp` ships no types, add a `[[tool.mypy.overrides]]` for that one module with the reason on the same line. Use the plain OpenCV wheel, not `contrib`: the WeChat decoder and `pyzbar` are added only if the spike (step 13) shows the decoders of step 6 are under 95%.
- Bandit runs in CI on `src`: if it flags the `lxml` import, the parser settings of step 7 are the mitigation; record the exception with its reason rather than switching silently.
- `.importlinter`: add `invoicebot.extract` and `invoicebot.routing` to the `zero-consumer` contract; a new contract `pure-readers` that forbids both from importing `invoicebot.pipeline`, `invoicebot.queue`, `invoicebot.store` and `invoicebot.channels`; and a new contract `heavy-in-helper-only` (see Conventions).
- No Dockerfile change is expected (the wheels are self-contained); prove it by importing them inside the built image, not only by building it.

Test: `uv run lint-imports && uv run pytest -q tests/unit/test_import_boundary.py && podman build -t invoicebot:read -f deploy/Dockerfile . && podman run --rm invoicebot:read python -c "import cv2, zxingcpp, lxml"`

## Step 2: Config keys for reading and routing `[worker]`

Files: `src/invoicebot/config/models.py`, `src/invoicebot/config/checks.py`, `config/example.yaml`, `tests/unit/test_config_models.py`.

- `Extraction`: add `igv_rates: list[Decimal]` (example `["0.18", "0.105", "0.10"]`, written as strings so no float rounding reaches `Decimal`), `supported_tipos: list[str]` (example `["01", "03", "07", "08"]`) and `helper_timeout_s: int` (example `30`). All required; the lists non-empty.
- `Flow`: add `accepted_tipos: list[str]` (example: `["01", "03"]` for `personal_google`, `["01"]` for `work_microsoft`). Every value must be in `supported_tipos`; the model validator rejects one that isn't.
- `checks.py`: every `routing.rules[].id` must be a rule in `RULES` (R-11.1), because the invoice records the id of the rule that routed it. The commented `BR-R10` example in `example.yaml` gets a note saying a new rule needs its row in spec 06 first.
- Nothing else in `example.yaml` changes in this phase.

Test cases (R-2.4, R-3.1, R-11.1): the example loads; an empty `igv_rates` fails; an `accepted_tipos` value outside `supported_tipos` fails with the flow named in the error; a routing rule with an id that isn't in the catalog fails `check_config`.

Test: `uv run pytest -q tests/unit/test_config_models.py tests/unit/test_config_checks.py`

## Step 3: Field model and validators (S3.1) `[worker]`

Files: `src/invoicebot/extract/fields.py`, `src/invoicebot/extract/validate.py`, `tests/unit/test_extract_validate.py`.

- `fields.py`: frozen dataclasses. `Field(value: str, source: Literal["qr","xml","ocr","padron","llm","user"], confidence: float)`; `Reading(fields: Mapping[str, Field], document_url: str | None)` with `REQUIRED = ("ruc_emisor", "tipo", "serie", "numero", "fecha_emision", "total")` (R-2.13) and `missing_required() -> tuple[str, ...]`.
- `validate.py`, pure functions that return a rule id or `None`:
  - `is_valid_ruc(text) -> bool` (mod 11; check digits 10 → 0 and 11 → 1). This becomes the one home for the algorithm: step 14 makes `scripts/check_fixtures.py` import it.
  - `check_issuer(ruc) -> "BR-E02" | None`.
  - `check_serie(serie) -> bool` (`[FBE][A-Z0-9]{3}` or 4 digits).
  - `check_date(fecha, today, max_age_days) -> "BR-E03" | None`. `today` is an argument; the caller takes it from the database clock.
  - `check_tipo(tipo, supported) -> "BR-E05" | None`.
  - `check_totals(total, base, igv, charges, discounts) -> bool`: true when base or igv is missing, else `abs(total - (base + igv + charges - discounts)) <= Decimal("0.05")`.
  - `check_igv_rate(base, igv, rates) -> bool`: some rate gives `abs(igv - base * rate) <= Decimal("0.05")`.
  - `doubtful_fields(reading, threshold) -> tuple[str, ...]`: required fields under the threshold, plus `serie`, `total` or `igv` when their check fails. In this phase the caller turns them into a warning line (D80); in `widen-b` they drive BR-E09.
- Amounts are `Decimal`, never `float`.

Test cases (R-2.4), table-driven: RUC check digits including the 10 → 0 and 11 → 1 cases; serie accepted and rejected forms; date today, tomorrow, 366 days old; totals at 18 %, 10.5 % and 10 % with and without a service charge (use amounts shaped like 40.90 + 7.36 + 1.64 = 49.90 and 45.16 + 4.74 = 49.90); a total off by 0.06 fails; a missing base passes; `doubtful_fields` names exactly the failing fields.

Test: `uv run pytest -q tests/unit/test_extract_validate.py`

## Step 4: SUNAT QR payload parser (S3.2) `[worker]`

Files: `src/invoicebot/extract/qr_payload.py`, `tests/unit/test_extract_qr_payload.py`.

- `parse_qr_payload(text: str) -> Reading | None`. Pipe format, by position: `ruc_emisor | tipo | serie | numero | igv | total | fecha | tipo_doc_adq | num_doc_adq | …` (extra trailing parts ignored, each part stripped). Every field is `source="qr"`, `confidence=1.0`.
- Dates `yyyy-mm-dd` and `dd/mm/yyyy` both become ISO. Amounts accept `49.90` and `49,90`.
- Text starting with `http://` or `https://` → `Reading` with no fields and `document_url` set (R-2.8).
- Anything else (fewer than 7 parts, a non-numeric RUC, an unparseable date or amount) → `None`. Never raises.
- `numero` keeps its digits as written; `tipo` is kept as two digits.
- **Non-standard payloads are repaired, not rejected (D79).** The owner's 2026 invoices show three real shapes: 11 and 10 parts in the standard order, and a 9-part shape (three stores, 6 of 35 invoices) where part 2 holds `6` in place of the document type and part 8 is empty. So: when part 2 is not a two-digit document type, `tipo` is inferred from the serie's first letter (`F` → `01`, `B` → `03`); when part 8 is empty, `tipo_doc_adq` is inferred from the buyer number (11 digits with a valid check digit → `6`, 8 digits → `1`). An inferred field has `confidence=0.9`, so the summary can say it was inferred. A serie that starts with neither letter leaves `tipo` missing (BR-E01).

Test cases (R-2.2, R-2.8): at least 20 payloads in a table: both date formats, comma decimals, a trailing pipe, a hash part at the end, spaces around parts, a buyer with `tipo_doc_adq` 6, 1 and `-`, a URL payload, an empty buyer, the 9-part shape with `6` in part 2 and an empty part 8 (tipo and buyer type inferred, confidence 0.9), a 9-part shape with a serie starting with `E` (tipo missing), and broken ones (5 parts, letters in the RUC, `32/13/2026`, empty string) that return `None`.

Test: `uv run pytest -q tests/unit/test_extract_qr_payload.py`

## Step 5: Image loading (R-1.11 follow-up, spec 03 §5 step 1) `[worker]`

Files: `src/invoicebot/extract/image.py`, `tests/unit/test_extract_image.py`.

- `load_gray(data: bytes, max_px: int) -> numpy.ndarray`: `cv2.imdecode` in grayscale, downscaled so the long side is at most `max_px`. Undecodable bytes raise `ImageUnreadable` (a `ValueError` subclass).
- `variants(gray) -> Iterator[tuple[str, ndarray]]`: lazily yields `original`, `upscaled_2x`, `upscaled_3x` of the bottom 40 %, `sharpened`, then rotations 90/180/270 of the original. Upscaling matters: on the owner's PDFs rendered at screen size, most QR codes decoded only after enlarging. Lazy, so a hit on the first variant does no further work.
- The megapixel cap was already enforced at download (`channels/media_limits.py`); this function does not repeat it.

Test cases: a generated 3000 × 1500 image comes back with long side `max_px`; a small image is not enlarged; garbage bytes raise `ImageUnreadable`; `variants` yields the names in order and is lazy (consume one, nothing else computed).

Test: `uv run pytest -q tests/unit/test_extract_image.py`

## Step 6: QR decoding (S3.3: zxing, then OpenCV's detector) `[worker]`

Files: `src/invoicebot/extract/qr.py`, `tests/unit/test_extract_qr.py`.

- `read_invoice_qr(gray) -> QrHit | None`: for each variant from step 5, try the decoders in order: `zxingcpp.read_barcodes` (QR and PDF417), then `cv2.QRCodeDetector().detectAndDecodeMulti` (already installed with OpenCV, so it costs no dependency); for each decoded text, `parse_qr_payload`; return the first non-`None` `Reading`. A QR that decodes but is not a SUNAT payload (a menu link, a Yape code) is skipped, unless it is a URL, which is kept only if no data payload is found on any variant.
- `QrHit(reading, variant, decoder)` names the variant and the decoder that hit, so the spike and the logs can count what works.
- Known from the owner's 2026 PDFs: 4 of 34 (two chains and one small store) carry no QR that OpenCV's detector can read. Whether zxing reads them (they may use PDF417) is a question for the spike; until OCR exists those invoices take the typed-data path (BR-E01).

Test cases (R-2.1), images made in the test with `cv2.QRCodeEncoder` and pasted on a white canvas: a clean QR; the same rotated 90° and 180°; small in the bottom corner of a 2000 px image; blurred lightly; two codes on one image (a URL and a SUNAT payload) → the payload wins; only a non-SUNAT text QR → `None`; a blank image → `None`.

Test: `uv run pytest -q tests/unit/test_extract_qr.py`

## Step 7: Hardened XML reading and match (S7.7, S7.3) `[worker]`

Files: `src/invoicebot/extract/ubl.py`, `tests/unit/test_extract_ubl.py`.

- `parse_invoice_xml(data: bytes) -> Reading`: reject over 2 MB and any `DOCTYPE` before parsing; `lxml` parser with `resolve_entities=False`, `no_network=True`, `load_dtd=False`, `huge_tree=False`. Failures raise `XmlRejected(reason)` with an enum reason (`too_large`, `doctype`, `malformed`, `not_invoice`); the caller maps it to BR-F09.
- Reads, by local name so the namespace prefix doesn't matter: `ID` (split into serie and numero), `IssueDate`, `InvoiceTypeCode`, supplier and customer `PartyIdentification/ID` (with the customer's `schemeID` as `tipo_doc_adq`), supplier `RegistrationName`, `LegalMonetaryTotal/LineExtensionAmount` (base), `ChargeTotalAmount` (service charge), `AllowanceTotalAmount` (discounts), `PayableAmount` (total), and the IGV `TaxTotal/TaxAmount`. Every field is `source="xml"`, `confidence=1.0`.
- `matches_invoice(xml: Reading, invoice: Reading) -> bool` (R-5.6): same `ruc_emisor`, `serie`, `int(numero)` and `total`. `F014-15160` and `F014-00015160` match.
- No signature check (D57).

Test cases (R-2.12, R-2.7, R-5.6), XML built inline with synthetic RUCs: a full invoice with a service charge; one at a reduced IGV rate with no charge; a `DOCTYPE` with an external entity (XXE) → `doctype`; a billion-laughs payload → `doctype`; 2 MB + 1 byte → `too_large`; truncated XML → `malformed`; a credit note root → `not_invoice`; `matches_invoice` true with zero-padded numbers, false on another total, serie or issuer.

Test: `uv run pytest -q tests/unit/test_extract_ubl.py`

## Step 8: Routing engine (S4.1) `[worker]`

Files: `src/invoicebot/routing/router.py`, `tests/unit/test_routing_router.py`.

- `compile_rules(routing: Routing) -> tuple[Rule, ...]` once at startup, values as frozensets. `{owner.ruc}` and `{work.ruc}` are already resolved by `config/loader.py`; nothing is resolved here.
- `route(reading, profile_flows, rules, flows, unmatched) -> RouteResult(flow: str | None, rule: str)`:
  1. the first rule whose every `when` key matches wins (keys: `num_doc_adq`, `tipo_doc_adq`, `ruc_emisor`, `tipo`);
  2. no match → `rule="BR-R03"` when `unmatched == "ask"`, `"BR-R07"` when `"reject"`, `flow=None`;
  3. the flow is not in `profile_flows` → `rule="BR-R05"`, `flow=None`;
  4. the invoice's `tipo` is not in the flow's `accepted_tipos` → `rule="BR-R08"`, `flow=None`.
- No flow name appears in the code: flows and rules come only from config (D53).

Test cases (R-3.1), with a config that has a fake third flow: owner RUC → A with BR-R01; work RUC → B with BR-R02; a rule on `ruc_emisor` routes to the fake flow C with no code change; first match wins when two rules match; a DNI buyer and a missing buyer → BR-R03 or BR-R07 by setting; a profile without the flow → BR-R05; a boleta to a flow that accepts only `01` → BR-R08.

Test: `uv run pytest -q tests/unit/test_routing_router.py`

## Step 9: Store the reading and chain the jobs `[worker]`

Files: `src/invoicebot/store/invoices.py`, `src/invoicebot/pipeline/handlers.py`, `tests/integration/test_invoices_store.py`.

- `save_reading(conn, invoice_id, reading)`: writes the typed columns (`ruc_emisor`, `tipo`, `serie`, `numero`, `fecha`, `igv`, `total`, and the buyer's number into `ruc_adq`, the existing column) and the whole reading into `fields_json` (value, source, confidence per field). No migration is needed: every column exists in `0001`.
- `load_reading(conn, invoice_id) -> Reading | None` from `fields_json`.
- `get_file_path(conn, invoice_id, kind) -> str | None`.
- `handlers.py`: after `add_file`, the download handler enqueues the `extract` step in the same transaction. An invoice that already has a file enqueues nothing (retries stay idempotent).

Test cases (R-2.6, R-9.4): a reading round-trips through `save_reading` / `load_reading` with sources and confidences intact; amounts come back as `Decimal`; the download handler leaves exactly one `extract` job; a retried download adds no second job.

Test: `uv run pytest -q tests/integration/test_invoices_store.py tests/integration/test_pipeline_handlers.py`

## Step 10: Extract handler `[worker]`

Files: `src/invoicebot/extract/__main__.py`, `src/invoicebot/pipeline/extract.py`, `tests/integration/test_pipeline_extract.py`.

- `make_extract_handler(pool, extraction_cfg) -> Handler` for step `extract`:
  1. set state `EXTRACTING`;
  2. under `HEAVY`, run the helper `python -m invoicebot.extract <photo path> <max_px>` with a timeout; it prints one JSON object: `{"reading": {...} | null, "variant": ..., "decoder": ..., "error": null | "unreadable"}`. The handler validates that JSON with a pydantic model before using it;
  3. no reading, missing required fields, `"unreadable"`, or a timeout → state `EXTRACT_FAILED`, `rule_id="BR-E01"`, BR-E01 in the outbox. A helper that exits non-zero for any other reason raises, so the queue retries and, after the limit, fails the invoice with BR-S03;
  4. `check_issuer` → BR-E02 (state `EXTRACT_FAILED`); `check_tipo` → BR-E05 (state `CANCELLED`); each with its message;
  5. otherwise `save_reading`, state `ROUTING`, enqueue step `route`.
- `extract/__main__.py` is the helper: `load_gray` → `read_invoice_qr` → JSON on stdout. It reads only the path it is given, writes nothing, and prints nothing else (its stderr is not logged, because it could carry decoded text).
- State change, next job and outbox row go in one transaction (CLAUDE.md "Database").
- The handler acts only on an invoice in `RECEIVED` or `EXTRACTING`; in any other state it returns, so a re-run changes nothing.
- The handler logs `step`, `invoice_id`, `variant`, `decoder`, `rule_id` and `duration_ms` only.

Test cases (R-2.1, R-2.13, BR-E01, BR-E02, BR-E05, R-9.4): a photo with a valid QR ends in `ROUTING` with one `route` job and the reading saved; a blank photo ends in `EXTRACT_FAILED` with one BR-E01 outbox row; a QR whose RUC has a bad check digit → BR-E02; `tipo` 09 → `CANCELLED` with BR-E05; a file missing on disk raises, so the queue retries; a helper that outlives the timeout is killed and ends in BR-E01; running the handler twice on a finished invoice changes nothing. The tests run the real helper process on generated images.

Test: `uv run pytest -q tests/integration/test_pipeline_extract.py`

## Step 11: Route handler and summary (S4.2) `[inline: user-facing copy and a spec change]`

Files: `src/invoicebot/pipeline/route.py`, `src/invoicebot/rules/messages_es.py` (+ `specs/06-business-rules.md`, a "Summary message" paragraph in §1), `tests/integration/test_pipeline_route.py`.

- Summary text, kept in `messages_es.py` as `SUMMARY` and written into spec 06 §1 word for word: `🧾 {tipo_desc} {serie}-{numero} · {fecha:%d/%m/%Y}` / `Emisor: RUC {ruc_emisor}` / `Total: S/ {total}` / `➡️ {flow.label}` (the text of the routing rule that matched) / `ℹ️ Deducido del comprobante: {fields_desc}` when a field was inferred (D79) / `⚠️ Revisa: {fields_desc}` when `doubtful_fields` or the date check found something (D80). Add `TIPO_DESC` (`01` "Factura", `03` "Boleta", `07` "Nota de crédito", `08` "Nota de débito") to `messages_es.py` for `{tipo_desc}`; BR-E05 uses it too.
- `make_route_handler(pool, cfg, rules) -> Handler` for step `route`: `load_reading` → `route`.
  - A flow → store `flow_id` and `rule_id`; send the summary; state `FETCHING_DOCS` with **no job** (SUNAT validation is off until `read-more`, and the `fetch` phase adds the next step).
  - BR-R05, BR-R07, BR-R08 → state `CANCELLED` with the rule's message. BR-R03 is answered as BR-R07 in this phase (shortcut above).
- The date check runs here, with `today` from `SELECT now()`: a date out of bounds adds `fecha` to the warning line (D80). The invoice still goes on.

Test cases (R-3.1, R-3.4, R-9.3, BR-R05, BR-R07, BR-R08, D79, D80): a work-RUC factura → `FETCHING_DOCS`, `flow_id` set, one summary in the outbox whose text contains the flow's label from config; ack + summary = 2 messages on the happy path; a boleta to the work RUC → BR-R08; an unknown buyer → BR-R07; a profile without the flow → BR-R05; a date 400 days old → `FETCHING_DOCS` with a summary whose warning line names the date; an inferred `tipo` → the "Deducido" line names it; no test leaves an invoice in `CONFIRMING`.

Test: `uv run pytest -q tests/integration/test_pipeline_route.py tests/unit/test_rules_parity.py`

## Step 12: Wiring, conversation test and log check (S3.11) `[inline: startup wiring across config, worker and handlers]`

Files: `src/invoicebot/app.py`, `tests/integration/test_conversation_fake.py`, `tests/integration/test_read_logging.py`.

- `app.py`: compile the routing rules at startup and register `extract` and `route` next to `download_media` in the worker's handler mapping. Nothing else changes.
- Conversation test on the fake channel: a photo with a generated QR for the work RUC → "recibido" then the summary with the work flow's label; a blank photo → "recibido" then BR-E01. The download handler needs the WhatsApp client, so the test stands in for it: after intake it writes the generated image to a temp file, adds the `files` row and the `extract` job itself, then runs the extract and route handlers and the outbox sender.
- Log test: run both conversations with log capture and assert that no RUC, serie-número, total or decoded QR text from the fixtures appears in any log record (R-9.16).

Test: `uv run pytest -q tests/integration/test_conversation_fake.py tests/integration/test_read_logging.py`

## Step 13: QR spike on real photos (S0.2) `[inline: measurement on private data]`

Files: `specs/spikes/qr.md` only. The script is throwaway and stays outside the repo; the photos stay in a private folder.

- Interim run (D77): render each of the owner's 2026 PDFs to an image with a renderer used only by the throwaway script (not a project dependency), apply blur, a 5–15° tilt, uneven light and a downscale to WhatsApp size, and run `read_invoice_qr`. Record the result under an "Interim, from rendered PDFs" heading.
- Real run: the same over the owner's photos (at least 10), under its own heading; it replaces the interim numbers.
- Record for each run: images, hits, hit rate, hits per variant and per distortion, and median time per image. Aggregates only: no RUC, store name or amount.
- Under 95 %: add the next decoder (WeChat, then `pyzbar`) as its own step with its own `py-add-package` check, and re-run. At or above 95 %: record that zxing alone ships.

Done when: `specs/spikes/qr.md` is committed and S3.3's threshold in spec 04 is confirmed or adjusted.

## Step 14: RUC and EXIF check on every tracked file (S3.12) `[worker]`

Files: `scripts/check_fixtures.py`, `tests/unit/test_check_fixtures.py`.

- Scan the files from `git ls-files` instead of only `tests/` and `config/`. Skip `uv.lock` and binary files. Checked on 2026-11-12: a whole-repo scan flags 8 hash fragments in `uv.lock` (false hits) and one RUC-shaped number in `specs/01-research.md`; replace that one with a synthetic value in this step. The two existing exceptions stay: `tests/synthetic_rucs.txt`, and a store file's own RUC.
- Import `is_valid_ruc` from `invoicebot.extract.validate` (one home for the algorithm).
- Outside a git checkout (the test's temp tree), fall back to walking the root.

Test cases (R-9.17): a real-looking RUC in `docs/x.md` and in `specs/spikes/y.md` fails; the same in `uv.lock` passes; the existing cases still pass.

Test: `uv run pytest -q tests/unit/test_check_fixtures.py && uv run python scripts/check_fixtures.py`

## Finish

- `uv run pytest -q --cov` (branch coverage floor 90 %), then the lint line above.
- Review: `py-code-reviewer` on the whole diff, and `py-security-auditor` because this phase parses files (images, XML). Both only report; the main session decides what to fix.
- Update `docs/plan-mvp.md`: mark `read` done with its date, move anything deferred into the named later phase, and write `docs/plan-mvp-fetch.md`.
