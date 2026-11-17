# Plan: MVP phase 7 · read-more

Roadmap: `docs/plan-mvp.md` row 7. PDF input (S3.10), the padrón (S3.4), the synthetic receipt generator and the golden-set accuracy report (S3.9, S3.8), SUNAT validation (E6: S6.1, S6.2), the `VALIDATING` and `CONFIRMING` states that spec 03 §4 names and the code skips (`confirm_mode: always`, BR-E03, `source=llm`), and the `read-more` leftovers of earlier reviews. M5 ("PDFs are read; the padrón; accuracy report on the private set; SUNAT validation") is reached when this phase and its deferred live check end.

**Goal:** a PDF sent on its own starts an invoice and is read from its text layer and QR, or from a page-1 render. The issuer's `razon_social` comes from a local padrón imported by hand from SUNAT's ZIP. After routing, every invoice passes `VALIDATING`: with SUNAT credentials the validez API is called and BR-V01 to BR-V06 act on its answer; without them nothing changes. CI fails when the committed synthetic set falls below R-2.9's thresholds, and one script measures the private set.

**Needs from the owner first:** neither prerequisite is done (owner, 2026-11-16), and the owner said to draft anyway. Nothing in this plan needs them for code or tests:

- S0.1 (private set ≥ 30 pairs): CI runs the committed synthetic set; `scripts/accuracy_report.py` runs on whatever private set exists, in the live check.
- S0.5 (one work invoice validated with the owner's credentials): the SUNAT API is tested with `httpx.MockTransport`; `sunat.validez_api.enabled` stays `false` in `config/example.yaml`. The token URL and the request fields come from the reference client (`thegreenter/consulta-cpe`, spec 01) and are confirmed in the live check.
- The padrón ZIP: tests build a synthetic mini-ZIP at test time.

The live checks are in "Deferred: live check" at the end.

## Design choices

Step 1 writes these into the specs (spec 05, D121 to D127). They are proposals for the owner's review of this plan.

- **D121 · When a PDF starts an invoice.** A PDF that quotes an open invoice, or arrives while the sender has open invoices, stays an upload for a stuck invoice (R-1.5, D102, today's `_link_upload`). A PDF with no quote and no open invoice starts a new invoice like a photo (BR-S09, back-pressure BR-S01). An XML with no quote and no open invoice gets BR-E07 `unsupported_type` (D58: XML is not an input). Today both are silently dropped.
- **D122 · PDF reading happens in the helper.** The untrusted PDF is opened only inside the extract helper subprocess (its rlimits already apply), with `pypdfium2` (one wheel, no system library; renders and reads text). More than `limits.media.max_pdf_pages` (new, default 5) pages → the helper answers `"error": "too_many_pages"` and the invoice gets BR-E07 with a new reason `too_many_pages`. Page 1 is rendered at 200 dpi to a gray image for the QR cascade. If page 1's text layer has at least `extraction.pdf_min_text_chars` (new, default 40) non-space characters, the text is parsed by the OCR rule parser and its fields get the new source `pdf` at full confidence; otherwise the render goes through the OCR path as a photo. QR and text merge as QR and OCR do today.
- **D123 · Padrón import, by hand, from a local ZIP.** `scripts/import_padron.py <zip>` reads a ZIP the owner downloaded (SUNAT's `padron_reducido_ruc.zip`: one `|`-separated text file, ISO-8859-1, a header row; columns RUC, razón social, estado, condición, then address columns that are ignored). It streams the member line by line into `COPY padron_staging` (created as `LIKE padron INCLUDING ALL`), skips and counts rows whose RUC is not 11 digits, then swaps in one transaction: drop `padron`, rename `padron_staging` and its primary key. It never downloads; the download and the weekly refresh come with S12.4 in `ops`. The lookup fills `razon_social` with source `padron` and full confidence when the padrón has the RUC; the reading's other fields are untouched.
- **D124 · `VALIDATING` always runs.** Routing that today goes straight to `FETCHING_DOCS` goes to `VALIDATING` and enqueues a `validate` job. The routed summary moves from the route step to the validate step, so the SUNAT line (BR-V01, V03, V04, V05) joins it. With `sunat.validez_api.enabled: false` the validate step only sends the summary and moves on, which is today's behavior. Enabled with a missing secret is refused at startup by the existing fail-closed `Secrets.enabled` check, not sent as BR-V04.
- **D125 · What a SUNAT answer does.** `estadoCp` 1 → `FETCHING_DOCS`, no line. 0 → `FETCHING_DOCS` with the BR-V01 line and a `sunat_recheck` job in 24 h, at most 3 rechecks. 3 → `FETCHING_DOCS` with BR-V05. 2 or 4 → `CONFIRM_INVALID` with BR-V02 or BR-V06 and the buttons [Sí] `<id>:invalid_ok` and [Cancelar]; [Sí] moves the invoice to `FETCHING_DOCS`. A timeout, a 5xx, a token failure or an answer that doesn't parse → `FETCHING_DOCS` with the BR-V04 line and a `sunat_recheck` job on the `queue/retry.py` backoff, at most 3. BR-V03 is added when the issuer is not `ACTIVO`/`HABIDO`: from the API's `estadoRuc`/`condDomiRuc` codes when it answered, else from the padrón row, else not checked. A recheck stores the new answer in `sunat_status_json`; one that finds 2 or 4 sends the owner BR-V02_admin or BR-V06_admin and changes no state (the invoice may already be delivered).
- **D126 · `CONFIRMING` before validation.** Routing asks in `CONFIRMING` with the summary and [Confirmar] `<id>:confirm`, [Editar], [Cancelar] when `confirm_mode: always`, when the date fails `check_date` (BR-E03's text replaces the summary's first line), or when any field has source `llm`. [Confirmar] records the answer `{"confirmed": true}` and re-runs routing, which then goes to `VALIDATING`; an edit already re-enters `ROUTING` and drops the answer (D92). The token for the API lives in `kv` under `sunat_token`, read and written with the database clock.
- **D127 · Golden set format.** `expected.json` holds `{"has_qr": bool, "fields": {name: value}}` with the reading's field names (`ruc_emisor`, `tipo`, `serie`, `numero`, `fecha_emision` as ISO date, `total`, `base`, `igv`, `service_charge`, `icbper`, `discounts`, `num_doc_adq`, `razon_social`), absent keys meaning "not printed". A set is a folder of `<name>.<jpg|png|pdf>` with `<name>.json`. The committed set lives in `tests/golden/` and is drawn by `scripts/make_golden.py` with `cv2` text and the QR from `zxing-cpp`'s writer (no new dependency). `scripts/expected_from_xml.py <xml> [--no-qr]` writes a private pair's JSON from its official XML.

Conventions: lint is `uv run ruff check src tests scripts migrations && uv run ruff format --check src tests scripts migrations && uv run mypy src && uv run lint-imports`. Integration tests need Postgres (`podman compose -f deploy/docker-compose.yml up -d postgres`). Every new user text is a constant in `rules/messages_es.py`, quoted in spec 06 by step 1. No real RUCs, phone numbers or photos in tests; use `tests/synthetic_rucs.txt` and add new synthetic RUCs there (shape `20000000xxx` with a valid check digit). Times come from the database clock (`now()`).

Out of scope here: the padrón download and weekly refresh (S12.4, `ops`), PDF417 barcodes, a revisit of `_fix_serie` (it waits for a private pair with such a serie; see the live check), the LLM fallback (E3b; only the `source=llm` check lands).

## Step 1: Record D121–D127 and the new texts `[inline: spec and roadmap text the owner reviews with this plan]`

Files: `specs/05-open-questions.md`, `specs/06-business-rules.md`, `specs/04-tasks.md`, `specs/03-design.md`, `docs/plan-mvp.md`.

- Spec 05: rows D121 to D127 with the text from "Design choices".
- Spec 06: BR-E07's `{reason_desc}` list gains `too_many_pages` (`tiene más de {max_pages} páginas`); BR-V02 and BR-V06 note the `invalid_ok` button. New owner texts: `BR-V02_admin` `⚠️ SUNAT ahora dice que el comprobante #{id} ({serie}-{numero}) está ANULADO.` and `BR-V06_admin` `⚠️ SUNAT ahora dice que el comprobante #{id} ({serie}-{numero}) NO está AUTORIZADO.` The confirm question: the summary followed by `¿Está todo bien?` with [Confirmar] [Editar] [Cancelar].
- Spec 04: S3.4 cites D123, S3.8 and S3.9 cite D127, S3.10 cites D121 and D122, S6.1 and S6.2 cite D124 and D125.
- Spec 03: §4 (route → `VALIDATING`, the summary sent by `validate`, D126), §5 (`sunat_status_json` shape: `estado_cp`, `estado_ruc`, `cond_domi_ruc`, `checked_at`, `rechecks`), §11 (D127), the `extract/pdf.py` text of D122, the padrón swap of D123.
- Roadmap: drop the "Known drift" items S3.10 and `confirm_mode` close; row 7 notes D121–D127.

Test: none (spec text only); `uv run pytest -q tests/unit/test_rules_parity.py` still passes.

## Step 2: Add `pypdfium2` `[inline: dependency vetting through py-add-package]`

Files: `pyproject.toml`, `uv.lock`.

- Run the `py-add-package` skill for `pypdfium2` (runtime). Check its license (Apache-2.0 / BSD-3) and that its wheel bundles PDFium (no system package).

Test: `uv run python -c "import pypdfium2; print(pypdfium2.PdfDocument.new())"`.

## Step 3: New texts and rules in the catalog `[worker:low]`

Files: `src/invoicebot/rules/messages_es.py`, `src/invoicebot/rules/catalog.py`, `tests/unit/test_rules_parity.py`.

- Add the texts of step 1 verbatim: `BR-V02_admin`, `BR-V06_admin`, the confirm question line (`CONFIRM_QUESTION`), the button title `BUTTON_CONFIRM = "Confirmar"`, `REJECTION_DESC["too_many_pages"]`.
- Catalog entries for `BR-V02_admin` and `BR-V06_admin` follow the existing `*_admin` entries.
- The parity test already compares spec 06 to the module; extend it only if it skips `_admin` texts.

Test: `uv run pytest -q tests/unit/test_rules_parity.py`.

## Step 4: `check_fixtures.py` refuses unlisted binary files `[worker]`

Files: `scripts/check_fixtures.py`, `tests/binary_fixtures.txt` (new), `tests/unit/test_check_fixtures.py`.

- Today a tracked file with a NUL byte or invalid UTF-8 is skipped (`:176-181`). Instead, a binary file that is not an image suffix (`IMAGE_SUFFIXES`) is a problem unless its path matches a glob in `tests/binary_fixtures.txt` (one `fnmatch` pattern per line, `#` comments). The file starts with a comment saying why each entry is safe; it starts empty apart from that comment.
- Tests: R-9.17 a tracked `.pdf` not in the list is reported; R-9.17 the same file under a listed glob passes; R-9.17 a PNG still goes through the EXIF check, not the list; R-9.17 the list file itself is not scanned for RUCs.

Test: `uv run pytest -q tests/unit/test_check_fixtures.py && uv run python scripts/check_fixtures.py`.

## Step 5: The Tesseract command is an argument `[worker]`

Files: `src/invoicebot/extract/ocr.py`, `tests/unit/test_extract_ocr.py`, `tests/fake_tesseract.py` (new).

- `read_text(image, ocr_cfg, command: Sequence[str] = (TESSERACT_COMMAND,))`: the argv starts with `command`, then today's arguments. Production callers keep the default.
- `tests/fake_tesseract.py`: a script that reads an env var `FAKE_TESSERACT_MODE` (`fail` exits 1, `sleep` sleeps past the timeout, `tsv` prints a fixed TSV) and ignores its arguments.
- Rewrite the three tests at `:84`, `:96`, `:107` to pass `command=(sys.executable, "tests/fake_tesseract.py")` instead of monkeypatching `subprocess.run`. Keep their assertions; add R-2.3 the fake's TSV comes back as the `OcrText`.

Test: `uv run pytest -q tests/unit/test_extract_ocr.py`.

## Step 6: `xlsx_formula` caps nesting and reads a unary `+` `[worker]`

Files: `src/invoicebot/sinks/xlsx_formula.py`, `tests/unit/test_sinks_xlsx_formula.py`.

- `parse` raises `UnsupportedFormula("nested deeper than 64 levels")` past 64 nested parentheses or calls (Excel's limit), counted in the parser, before `_refs` or `_evaluate` recurse. Name the constant `MAX_NESTING = 64`.
- `_unary` accepts `+` as well as `-`; a unary `+` returns its operand unchanged.
- Tests: R-7.4 `=+A1+B1` evaluates to the sum; R-7.4 `=--A1` still works; R-7.4 64 nested parentheses parse; R-7.4 65 raise `UnsupportedFormula`, not `RecursionError`.

Test: `uv run pytest -q tests/unit/test_sinks_xlsx_formula.py`.

## Step 7: Config keys for PDFs and SUNAT `[worker]`

Files: `src/invoicebot/config/models.py`, `config/example.yaml`, `tests/unit/test_config_models.py`.

- `Limits.media` (the model holding `max_bytes`): `max_pdf_pages: int = Field(5, gt=0)`. `Extraction`: `pdf_min_text_chars: int = Field(40, ge=0)`.
- `ValidezApi`: add `token_url: HttpsUrl` with the default `https://api-seguridad.sunat.gob.pe/v1/clientesextranet/{client_id}/oauth2/token/`, `api_url` with the default `https://api.sunat.gob.pe/v1/contribuyente/contribuyentes/{ruc}/validarcomprobante`, `timeout_s: float = Field(10, gt=0)`. Use the repo's existing https URL type if there is one; otherwise a `str` with a pattern `^https://`. A model validator checks that `token_url` contains `{client_id}` and `api_url` contains `{ruc}`.
- `example.yaml`: `validez_api.enabled: false` and the three keys; `max_pdf_pages: 5`; `pdf_min_text_chars: 40`.
- Tests: R-9.6 `example.yaml` loads; R-2.15 `max_pdf_pages: 0` is refused; R-4.1 an `http://` `token_url` is refused; R-4.1 an `api_url` without `{ruc}` is refused.

Test: `uv run pytest -q tests/unit/test_config_models.py && uv run python scripts/check_config.py`.

## Step 8: Padrón lookup `[worker]`

Files: `src/invoicebot/store/padron.py` (new), `tests/integration/test_store_padron.py` (new).

- `@dataclass(frozen=True) class PadronRow: ruc: str; razon_social: str; estado: str; condicion: str`.
- `async def lookup(conn, ruc: str) -> PadronRow | None`: `SELECT … FROM padron WHERE ruc = %s` with `int(ruc)`; a `ruc` that is not 11 digits returns `None` without a query.
- Tests: R-2.5 a seeded row comes back; R-2.5 an unknown RUC is `None`; R-2.5 `EXPLAIN` of the lookup query uses `padron_pkey` (an `Index Scan` or `Index Only Scan` line), with enough seeded rows and `ANALYZE` for the planner to prefer it.

Test: `uv run pytest -q tests/integration/test_store_padron.py`.

## Step 9: Padrón line parser `[worker]`

Files: `src/invoicebot/padron.py` (new), `tests/unit/test_padron.py` (new).

- Module docstring: reading SUNAT's padrón text, one line at a time (D123). `def parse_line(line: str) -> tuple[str, str, str, str] | None`: split on `|`, take the first four columns stripped, return `None` when the RUC is not 11 digits or fewer than 4 columns exist. `def rows(lines: Iterable[str]) -> Iterator[tuple[str, str, str, str]]` skips the header row (the first line) and `None` lines, and counts skipped lines in a small result object returned at the end (or a counter the caller passes in; choose the simpler).
- `def open_member(zip_path: Path) -> ContextManager[TextIO]`: the ZIP's single `.txt` member through `zipfile` and `io.TextIOWrapper(encoding="iso-8859-1", newline="")`; a ZIP with zero or several `.txt` members raises `ValueError`. Reuse `channels/media_limits.check_zip` for the entry-count and ratio caps if its limits fit (SUNAT's ratio is about 4:1); if not, say so in the summary and don't call it.
- Tests: R-2.5 a line with Ñ in latin-1 decodes; R-2.5 a short line and a 10-digit RUC are skipped and counted; R-2.5 the header is skipped; R-2.5 a ZIP with two `.txt` members raises.

Test: `uv run pytest -q tests/unit/test_padron.py`.

## Step 10: Padrón import and swap `[worker]`

Files: `src/invoicebot/padron.py`, `scripts/import_padron.py` (new), `tests/integration/test_import_padron.py` (new).

- `async def import_padron(conn, zip_path: Path) -> ImportResult(rows, skipped)`: `DROP TABLE IF EXISTS padron_staging`, `CREATE TABLE padron_staging (LIKE padron INCLUDING ALL)`, stream `rows()` into `cursor.copy("COPY padron_staging (ruc, razon_social, estado, condicion) FROM STDIN")` with `write_row`, then in one transaction `DROP TABLE padron`, `ALTER TABLE padron_staging RENAME TO padron`, `ALTER INDEX padron_staging_pkey RENAME TO padron_pkey`. The caller's connection is in autocommit for the COPY; the swap uses `conn.transaction()`.
- `scripts/import_padron.py <zip>`: loads config and the database URL the way `scripts/import_claims.py` does, runs the import, prints rows and skipped. It logs no RUCs or names.
- Tests (the mini-ZIP is written to `tmp_path` at test time): R-2.5 rows are imported and `lookup` finds one; R-2.5 a second import replaces the first (an old RUC is gone); R-2.5 a failure mid-COPY (a ZIP whose second half is a broken line that makes COPY raise) leaves the old `padron` intact; R-2.5 `tracemalloc` peak during the import of a ZIP with ≥ 20 MB of uncompressed text stays under 10 MB (streaming).

Test: `uv run pytest -q tests/integration/test_import_padron.py`.

## Step 11: Fill `razon_social` from the padrón `[worker]`

Files: `src/invoicebot/extract/padron.py` (new), `src/invoicebot/pipeline/extract.py`, `tests/integration/test_pipeline_extract.py`.

- `extract/padron.py`: `def with_padron(reading: Reading, razon_social: str | None) -> Reading` returns the reading with `razon_social` set to `Field(razon_social, "padron", FULL_CONFIDENCE)` when given, else unchanged. It imports no store code.
- `pipeline/extract.py`: after `_classify` succeeds and before `save_reading`, look up `ruc_emisor` with `store.padron.lookup` in the same transaction and pass its `razon_social`.
- Tests: R-2.5 a seeded padrón row replaces the OCR `razon_social` with source `padron`; R-2.5 an empty padrón keeps the OCR value; R-9.16 the extraction log lines carry no razón social.

Test: `uv run pytest -q tests/integration/test_pipeline_extract.py`.

## Step 12: The `kv` store `[worker:low]`

Files: `src/invoicebot/store/kv.py` (new), `tests/integration/test_store_kv.py` (new).

- `async def get(conn, key) -> str | None` returns the value when `expires_at IS NULL OR expires_at > now()`. `async def put(conn, key, value, ttl: timedelta | None)` upserts with `expires_at = now() + ttl`. No commit.
- Tests: R-4.3 a put value is read back; R-4.3 an expired value reads `None`; R-4.3 a second put replaces the first.

Test: `uv run pytest -q tests/integration/test_store_kv.py`.

## Step 13: SUNAT token `[worker]`

Files: `src/invoicebot/sunat/__init__.py` (new), `src/invoicebot/sunat/token.py` (new), `tests/integration/test_sunat_token.py` (new).

- Package docstring: the SUNAT validez API client; it imports neither `pipeline`, `queue`, `api` nor `events`.
- `SECRETS = frozenset({"sunat_client_id", "sunat_client_secret"})`; `ADAPTER = "sunat_validez"`.
- `async def token(conn, http: httpx.AsyncClient, secrets: Secrets, cfg: ValidezApi) -> str`: reads `kv` key `sunat_token`; on a miss POSTs the form `grant_type=client_credentials`, `scope=https://api.sunat.gob.pe/v1/contribuyente/contribuyentes`, `client_id`, `client_secret` to `cfg.token_url.format(client_id=…)` with `cfg.timeout_s`; stores `access_token` with `ttl = expires_in - 60 s` (R-4.3). A non-200, a timeout or a body without a string `access_token` and a positive int `expires_in` raises `SunatUnavailable` (defined here). The token and secret never reach a log or an exception text.
- Tests (`httpx.MockTransport`): R-4.3 the first call POSTs and caches; R-4.3 a second call within the TTL makes no request; R-4.3 a cached token 59 s from expiry is refreshed (seed `kv` directly with a short `expires_at`); R-4.3 a 401 and a malformed body raise `SunatUnavailable` whose text holds no secret.

Test: `uv run pytest -q tests/integration/test_sunat_token.py`.

## Step 14: `validarcomprobante` `[worker]`

Files: `src/invoicebot/sunat/validez.py` (new), `tests/unit/test_sunat_validez.py` (new).

- `@dataclass(frozen=True) class CpStatus: estado_cp: int; estado_ruc: str | None; cond_domi_ruc: str | None`.
- `async def validate(http, token: str, cfg: ValidezApi, owner_ruc: str, reading: Reading) -> CpStatus`: POST JSON `{"numRuc": ruc_emisor, "codComp": tipo, "numeroSerie": serie, "numero": numero, "fechaEmision": "dd/mm/yyyy", "monto": "0.00"-formatted total}` with `Authorization: Bearer`. A 200 whose `success` is true and whose `data.estadoCp` is a digit 0–4 returns the status; anything else (timeout, non-200, `success: false`, a missing or out-of-range `estadoCp`) raises `SunatUnavailable`. A response over 64 KB is refused (stream with a cap, as `net/http.py` callers do).
- Tests (`httpx.MockTransport`): R-4.1 the request body and URL match a fixed reading; R-4.1 each `estadoCp` 0–4 parses; R-4.1 `estadoCp: "7"`, `success: false`, a 500, a timeout and an oversize body raise `SunatUnavailable`.

Test: `uv run pytest -q tests/unit/test_sunat_validez.py`.

## Step 15: The SUNAT verdict `[worker]`

Files: `src/invoicebot/sunat/verdict.py` (new), `tests/unit/test_sunat_verdict.py` (new).

- `@dataclass(frozen=True) class Verdict: rule_id: str | None; ask: bool; recheck: bool; issuer_line: tuple[str, str] | None`.
- `def decide(status: CpStatus | None, padron: PadronRow | None) -> Verdict` implements D125: `None` (unavailable) → BR-V04, recheck; 1 → no rule; 0 → BR-V01, recheck; 3 → BR-V05; 2 → BR-V02, ask; 4 → BR-V06, ask. `issuer_line` (for BR-V03) is `(estado, condicion)` names when the issuer is not `ACTIVO`/`HABIDO`: from `estado_ruc`/`cond_domi_ruc` codes through a table (`00` ACTIVO; `01` BAJA PROVISIONAL; `02` BAJA PROV. POR OFICIO; `03` SUSPENSION TEMPORAL; `10` BAJA DEFINITIVA; `11` BAJA DE OFICIO; `22` INHABILITADO-VENT.UNICA; condición `00` HABIDO, `09` PENDIENTE, `11` POR VERIFICAR, `12` NO HABIDO, `20` NO HALLADO; an unknown code shows the code), else from the padrón row's text, else `None`. `PadronRow` is imported from `store.padron`; if import-linter forbids `sunat` → `store`, take two strings instead.
- Tests: R-4.2 one case per `estadoCp`, plus unavailable; BR-V03 a `10`/`00` status gives `("BAJA DEFINITIVA", "HABIDO")`; BR-V03 `00`/`00` gives no line; BR-V03 no status with a `BAJA DE OFICIO` padrón row gives the line; BR-V03 an unknown code `99` shows `99`.

Test: `uv run pytest -q tests/unit/test_sunat_verdict.py`.

## Step 16: Store the SUNAT status `[worker:low]`

Files: `src/invoicebot/store/invoices.py`, `tests/integration/test_invoices_store.py`.

- `async def set_sunat_status(conn, invoice_id, status: CpStatus | None, rechecks: int)` writes `sunat_status_json = {"estado_cp", "estado_ruc", "cond_domi_ruc", "checked_at": now(), "rechecks"}` (`null` fields when unavailable), using the database clock in SQL (`jsonb_build_object(…, 'checked_at', now())`). `async def sunat_rechecks(conn, invoice_id) -> int` reads `rechecks` (0 when null).
- Tests: R-4.1 a status is stored and its `checked_at` is the database time; R-4.1 `rechecks` reads back.

Test: `uv run pytest -q tests/integration/test_invoices_store.py -k sunat`.

## Step 17: Move the routed summary to its own module `[worker:low]`

Files: `src/invoicebot/pipeline/summary.py` (new), `src/invoicebot/pipeline/route.py`, `tests/integration/test_pipeline_route.py`.

- Move `_summary` (`route.py:546`) and the helpers only it uses to `pipeline/summary.py` as `summary_text(reading, flow_line, today, extraction, extra_lines: Sequence[str] = ())`, the extra lines appended at the end. `route.py` imports it. No behavior change.

Test: `uv run pytest -q tests/integration/test_pipeline_route.py`.

## Step 18: The validate step `[worker:high: reasoning: the step joins the SUNAT call, the verdict, the state change, the summary and the next job in one transaction, and the order of the network call against the transaction needs care]`

Files: `src/invoicebot/pipeline/validate.py` (new), `tests/integration/test_pipeline_validate.py` (new).

- `VALIDATE_STEP = "validate"`. `make_validate_handler(pool, http, secrets: Secrets | None, cfg: Config) -> Handler`, following `make_route_handler`'s shape. Acts only in `VALIDATING`. `secrets is None` means validation is off (D124): one transaction sets `FETCHING_DOCS` (expected `VALIDATING`), enqueues `FETCH_STEP` and queues the summary with `summary_buttons`, exactly as `_routed` does today.
- With validation on: read the reading and the padrón row; get the token and call `validate` **outside** any transaction (no connection held during the HTTP call, except the short `kv` read and write in `token`); `SunatUnavailable` → `status = None`. Then one transaction: `set_sunat_status`, `decide`, and
  - not `ask`: `FETCHING_DOCS`, `FETCH_STEP`, the summary with the rule's line and the BR-V03 line as `extra_lines`; when `recheck`, enqueue `SUNAT_RECHECK_STEP` (defined in step 19's module; define the constant here if step 19 isn't in yet, and move it there) with `run_after = now() + 24 h` for BR-V01, `now() + backoff_minutes(1)` for BR-V04.
  - `ask`: `CONFIRM_INVALID` with the rule id, the BR-V02/V06 text and buttons [Sí] `button_id(id, INVALID_OK_ACTION)` and [Cancelar] (`INVALID_OK_ACTION = "invalid_ok"` lives in `pipeline/actions.py`; add only the constant here if step 20 isn't in).
  - A state moved under the call (`StateMoved`) ends the job quietly, as in `route.py`.
- Tests (`httpx.MockTransport`, real Postgres): R-4.1 off → `FETCHING_DOCS`, one fetch job, the summary as before; R-4.2 `estadoCp` 1 → `FETCHING_DOCS`, status stored, no SUNAT line; R-4.2 0 → BR-V01 line and a recheck job about 24 h ahead; R-4.2 3 → BR-V05 line; R-4.2 2 and 4 → `CONFIRM_INVALID` with the rule and the [Sí] button, no fetch job; BR-V04 a 500 → `FETCHING_DOCS` with the V04 line and a recheck job; BR-V03 an inactive issuer adds its line; R-4.1 an invoice cancelled before the job runs is left alone.

Test: `uv run pytest -q tests/integration/test_pipeline_validate.py`.

## Step 19: The SUNAT recheck `[worker]`

Files: `src/invoicebot/pipeline/sunat_recheck.py` (new), `tests/integration/test_pipeline_sunat_recheck.py` (new).

- `SUNAT_RECHECK_STEP = "sunat_recheck"`; `MAX_RECHECKS = 3` (BR-V01). `make_sunat_recheck_handler(pool, http, secrets, cfg)`: any state except `CANCELLED`. Call as in step 18; one transaction stores the status with `rechecks + 1` and:
  - 1 or 3: nothing more. 0 or unavailable: a new recheck when `rechecks + 1 < MAX_RECHECKS` (24 h for 0, the backoff for unavailable). 2 or 4: queue `BR-V02_admin` or `BR-V06_admin` through `queue_admin_alert`; no state change (D125).
- Tests: BR-V01 0 then 1 stores 1 and schedules nothing; BR-V01 three answers of 0 stop after the third; BR-V02 a recheck answering 2 alerts the admin and leaves the state; BR-V04 an outage reschedules on the backoff; R-4.1 a cancelled invoice is skipped.

Test: `uv run pytest -q tests/integration/test_pipeline_sunat_recheck.py`.

## Step 20: [Sí] on `CONFIRM_INVALID` `[worker]`

Files: `src/invoicebot/pipeline/actions.py`, `src/invoicebot/pipeline/questions.py`, `tests/integration/test_pipeline_actions.py`.

- `INVALID_OK_ACTION = "invalid_ok"`, valid in `{CONFIRM_INVALID}`. It answers BR-V02 or BR-V06: if `ASKED_RULES` holds one rule per action, change its values to `frozenset[str]` and update the lookup in `_still_asks` and `replies.resolve_reply` (keep the other actions' sets to one rule). The action sets `FETCHING_DOCS` (expected `CONFIRM_INVALID`) and enqueues `FETCH_STEP`.
- `questions.py`: `invalid_question(invoice_id, rule_id) -> tuple[str, list[Button]]` with the rule's text, [Sí] and [Cancelar]; step 18's handler uses it (update it if step 18 built the buttons inline).
- Tests: BR-V02 [Sí] moves to `FETCHING_DOCS` with a fetch job; BR-V06 the same; BR-V02 [Cancelar] cancels; BR-S02 [Sí] on an invoice already `FETCHING_DOCS` is stale.

Test: `uv run pytest -q tests/integration/test_pipeline_actions.py -k invalid`.

## Step 21: Wire validation into the app and route to `VALIDATING` `[worker]`

Files: `src/invoicebot/app.py`, `src/invoicebot/pipeline/route.py`, `.importlinter`.

- `app.py`: when `cfg.sunat.validez_api.enabled`, `store.scoped(sunat.ADAPTER, sunat.SECRETS)`; refuse to start when its `.enabled` is false (D124), the way the other adapters fail closed. Register `VALIDATE_STEP` and `SUNAT_RECHECK_STEP` in `handlers` with the plain `http` client (the host is fixed config). Pass `None` secrets when disabled.
- `route.py` `_routed`: set `VALIDATING` and enqueue `VALIDATE_STEP`; no summary (the validate step sends it). `ROUTED_STATE` becomes `VALIDATING`.
- `.importlinter`: a contract that `invoicebot.sunat` imports none of `invoicebot.pipeline`, `invoicebot.queue`, `invoicebot.api`, `invoicebot.events`.
- Update the route tests that expected `FETCHING_DOCS` and a summary from the route step: they now expect `VALIDATING` and a `validate` job; add one end-to-end test R-4.1 route then validate (off) gives the same summary and `FETCHING_DOCS` as before.

Test: `uv run pytest -q tests/integration/test_pipeline_route.py tests/unit/test_app_wiring.py && uv run lint-imports`.

## Step 22: Confirm before validating `[worker]`

Files: `src/invoicebot/pipeline/route.py`, `src/invoicebot/pipeline/actions.py`, `tests/integration/test_pipeline_route.py`.

- In `_decide`, after the flow and buyer questions and before `route`: when `confirm_mode == "always"`, `check_date(fecha, …)` returns BR-E03, or any field has source `llm`, and the recorded answers lack `{"confirmed": true}`, return an `ask` in `CONFIRMING` with the summary (`summary_text`), `CONFIRM_QUESTION`, and [Confirmar] `<id>:confirm`, [Editar], [Cancelar]; for BR-E03 the rule is BR-E03 and its text comes first.
- `actions.py`: `CONFIRM_ACTION = "confirm"`, valid in `CONFIRMING`, asked rules BR-E03 and the confirm question's rule id (use `BR-E09`'s neighbour pattern: pick the existing rule id for a plain confirmation if spec 06 has one, else a new `CONFIRM` id added in step 3's catalog; say which in the summary). It records `{"confirmed": true}` with `_record_answer`.
- Tests: R-3.4 `confirm_mode: always` asks once, then [Confirmar] moves to `VALIDATING`; R-3.4 `auto` with a good date goes straight to `VALIDATING`; BR-E03 a date one year and a day old asks with BR-E03's text; BR-E03 an edit `fecha=…` re-enters `ROUTING` and, with a good date, routes on; R-2.11 a reading with one `llm` field asks even with `auto`.

Test: `uv run pytest -q tests/integration/test_pipeline_route.py -k "confirm or E03 or llm"`.

## Step 23: A PDF or an XML on its own `[worker]`

Files: `src/invoicebot/pipeline/intake.py`, `tests/integration/test_intake.py`.

- `handle_inbound`: a message whose attachment is `pdf`, with no resolved quote and no open invoice for the profile (`list_open_invoices` empty), goes through `_queue_or_back_pressure` like a photo (D121). With a quote or open invoices it stays an upload (`_link_upload`, unchanged). An `xml` with no quote and no open invoice queues BR-E07 with `reason_desc` `unsupported_type`; no invoice is created.
- Tests: R-1.1 a lone PDF creates an invoice in `RECEIVED`, a `download_media` job and BR-S09; R-1.5 a PDF while an invoice is open is linked as today; R-1.5 a PDF quoting an invoice is linked; BR-E07 a lone XML gets BR-E07 and creates nothing; BR-S01 a lone PDF over the queue cap gets BR-S01.

Test: `uv run pytest -q tests/integration/test_intake.py`.

## Step 24: The helper reads a PDF `[worker]`

Files: `src/invoicebot/extract/pdf.py` (new), `src/invoicebot/extract/__main__.py`, `tests/unit/test_extract_pdf.py` (new).

- `extract/pdf.py` (D122): `class TooManyPages(ValueError)`; `def open_pdf(path, max_pages) -> PdfDocument` raises `TooManyPages`; `def page_text(doc) -> str` page 1's text layer; `def render_gray(doc, dpi=200) -> np.ndarray` page 1 as a gray `uint8` array; `def text_to_ocr(text) -> OcrText` builds the `OcrText` the OCR parser takes from plain text lines, each word at full confidence (read `extract/ocr.py`'s `OcrText` for its shape). A `pypdfium2` error raises `ValueError` (unreadable).
- `__main__.py`: a new argv flag for the input kind (`photo`|`pdf`) and `max_pdf_pages` and `pdf_min_text_chars`; for a PDF: open (too many pages → `"error": "too_many_pages"`), render page 1 and run the QR cascade on it; when the text layer has ≥ `pdf_min_text_chars` non-space characters, parse it with `parse_ocr(text_to_ocr(text))` and mark its fields source `pdf`; else preprocess and OCR the render as a photo. Merge as today. Add `"pdf"` to `Source` in `extract/fields.py` (D122) if it is not there.
- Tests build PDFs at test time with `pypdfium2` (a page with text drawn via its text API, or an image page from a cv2 canvas): R-2.15 a text PDF yields `ruc_emisor`, `serie`, `numero`, `total` with source `pdf`; R-2.15 an image-only PDF goes through OCR (source `ocr`); R-2.15 a 6-page PDF raises `TooManyPages`; R-2.15 random bytes raise `ValueError`.

Test: `uv run pytest -q tests/unit/test_extract_pdf.py`.

## Step 25: The extract step takes a PDF `[worker]`

Files: `src/invoicebot/pipeline/extract.py`, `src/invoicebot/pipeline/handlers.py`, `tests/integration/test_pipeline_extract.py`.

- `pipeline/extract.py`: remove the `shortcut:` at `:122-124`. The file is the invoice's `photo` or `pdf` file; pass its kind and the two new config values in `_run_helper`'s argv. `_HelperResult.error` accepts `"too_many_pages"`, which ends the invoice in `CANCELLED` with BR-E07 (`reason_desc` `too_many_pages`, `max_pages`) through the same path as `handlers._reject`; reuse it rather than copying it (move it to a shared function if it's private to `handlers.py`).
- `handlers.py`: the download handler accepts a `pdf` attachment for a new invoice (its byte cap is `caps.pdf` already); check whether `_first_attachment` or the photo-only path refuses it, and fix only that.
- Tests: R-2.15 a text PDF goes `RECEIVED` → `ROUTING` with a reading; R-2.15 a 6-page PDF ends `CANCELLED` with BR-E07; R-1.11 a PDF over `max_bytes.pdf` ends with BR-E07 `too_large`.

Test: `uv run pytest -q tests/integration/test_pipeline_extract.py tests/integration/test_pipeline_handlers.py`.

## Step 26: Golden-set scoring `[worker]`

Files: `src/invoicebot/extract/accuracy.py` (new), `tests/unit/test_extract_accuracy.py` (new).

- `def load_expected(path) -> Expected` (D127 format; unknown keys refused). `def score(reading: Reading | None, expected: Expected) -> Score` with `qr_all_right: bool | None` (`None` when `has_qr` is false) and `required_right`, `required_total` counts over `REQUIRED`. A field is right when it equals the expected value: amounts as `Decimal`, `fecha_emision` as an ISO date, others as stripped upper-case text (reuse `merge._same_value`; make it public as `same_value` if needed). `def summarize(scores) -> Summary` with the QR rate, the OCR-only required-field rate and `passes(qr_min=0.95, ocr_min=0.80)`, the thresholds as named constants citing R-2.9.
- Tests: R-2.9 an exact reading scores all right; R-2.9 `10.0` equals `10.00`; R-2.9 a missing `total` counts wrong; R-2.9 a set at 94 % QR fails and at 95 % passes; R-2.9 a reading `None` (unreadable) counts every field wrong.

Test: `uv run pytest -q tests/unit/test_extract_accuracy.py`.

## Step 27: Synthetic receipt generator `[worker]`

Files: `scripts/make_golden.py` (new), `tests/unit/test_make_golden.py` (new), `tests/synthetic_rucs.txt`.

- `scripts/make_golden.py --seed N --count K --out DIR`: for each receipt, a `random.Random(seed)` picks a synthetic issuer RUC from `tests/synthetic_rucs.txt` (add a few new valid ones of the `20000000xxx` shape), a serie, numero, date, 1–4 items, IGV at 18 %, 10 % or 10.5 %, an optional 10 % service charge, and whether a QR is printed. It draws a receipt-shaped canvas with `cv2.putText` in the layout the OCR parser reads (look at the cv2 canvas in `tests/unit/test_extract_ocr.py` and the labels in `config/parsing_rules.yaml`), places the SUNAT QR (`ruc|tipo|serie|numero|igv|total|fecha|…`) drawn with `zxing-cpp`'s writer, applies a mild rotation, blur, noise and JPEG compression, and writes `<name>.jpg` (no EXIF: `cv2.imwrite`) and `<name>.json` (D127).
- Same seed → byte-identical output.
- Tests: R-2.9 two runs with seed 1 produce identical files; R-9.17 `check_fixtures.find_problems` on the output dir reports nothing; R-2.9 each JSON's `total` equals base + IGV + service charge; R-2.1 the QR of a generated image decodes with `read_invoice_qr` to the JSON's values.

Test: `uv run pytest -q tests/unit/test_make_golden.py`.

## Step 28: Commit the synthetic set and gate CI on it `[inline: the set is tuned until it covers S0.1's cases and passes R-2.9; the tuning is judgment]`

Files: `tests/golden/` (new, ≥ 10 pairs), `tests/unit/test_golden_set.py` (new), `scripts/make_golden.py` (only if tuning needs it).

- Generate with a fixed seed until the set covers QR present and absent, IGV at 18 %, 10 % and 10.5 %, and with and without a service charge (S0.1), at most about 1.5 MB in all.
- `test_golden_set.py` runs the real helper (as `pipeline/extract.py` builds its argv) over every image, scores with `extract/accuracy.py` and asserts `summarize(...).passes()`. Mark nothing as skipped; Tesseract is already required by the unit tests.
- If the set fails R-2.9, fix the generator's layout or the parser rules it exposes, not the thresholds; a parser change is its own commit with its own test.

Test: `uv run pytest -q tests/unit/test_golden_set.py && uv run python scripts/check_fixtures.py`.

## Step 29: Accuracy report and expected-from-XML scripts `[worker]`

Files: `scripts/accuracy_report.py` (new), `scripts/expected_from_xml.py` (new), `tests/unit/test_accuracy_scripts.py` (new).

- `scripts/accuracy_report.py --set <dir> [--write <md>]`: runs the helper over each image or PDF in the set (the same argv builder as step 28; share it, don't copy it), prints the QR rate, the OCR-only rate, the per-field rates and pass/fail, and with `--write` writes those numbers to the markdown file (default `specs/spikes/accuracy.md`) with the date and the set size. It prints no field values, RUCs or file names (R-9.16, D54).
- `scripts/expected_from_xml.py <xml> [--no-qr]` prints the D127 JSON from `parse_invoice_xml`.
- Tests: R-2.9 the report on `tests/golden` prints rates and passes; R-9.16 its output contains no RUC from the set; R-2.9 a UBL fixture from `tests/unit/test_extract_ubl.py` becomes the expected JSON with `has_qr: true`, and `--no-qr` makes it `false`.

Test: `uv run pytest -q tests/unit/test_accuracy_scripts.py`.

## Step 30: Close the drift notes `[inline: roadmap text]`

Files: `docs/plan-mvp.md`, `specs/spikes/accuracy.md` (new).

- `specs/spikes/accuracy.md`: the committed set's numbers from `scripts/accuracy_report.py --set tests/golden --write`, labeled "synthetic set; the private set's numbers come with the live check".
- Roadmap: remove the "Known drift" lines that steps 21–25 closed; mark the `read-more` leftovers done (`check_fixtures` binaries, the Tesseract command, `xlsx_formula`); move `_fix_serie` to "Deferred: live check" below.

Test: `uv run python scripts/check_fixtures.py`.

## Deferred: live check

Run when the owner has S0.1 and S0.5. Record the results in `specs/spikes/accuracy.md` and spec 04 (M5).

1. **S0.5:** with the SUNAT API credentials in the age store and `validez_api.enabled: true`, send a work invoice photo. Confirm the token URL and the request fields of steps 13–14 against the real API (fix the config defaults if they differ) and that `sunat_status_json` holds `estado_cp` 1.
2. **S0.1:** with ≥ 30 private pairs (photo + XML → `scripts/expected_from_xml.py`), run `scripts/accuracy_report.py --set <private dir> --write` and record the numbers. If any pair has a serie with D, O, Q, I or L in its last three characters, check `_fix_serie` (`extract/ocr_parse.py:189`) against it.
3. **Padrón:** download `padron_reducido_ruc.zip`, run `scripts/import_padron.py` under `/usr/bin/time -v`, and record peak RSS (target < 50 MB, S3.4) and peak disk while staging and live tables coexist.
4. **PDF:** send one store PDF through WhatsApp and confirm it is read from its text layer.
