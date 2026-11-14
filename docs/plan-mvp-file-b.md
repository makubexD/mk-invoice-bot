# Plan: MVP phase 5 · file-b (M3)

Roadmap: `docs/plan-mvp.md` row 5 and its carried-over items. Backlog: `specs/04-tasks.md` S11.1, S11.2, S11.3, S11.4, S11.5, S11.8 (`dry_run` and `graph` with `account: personal` only), S11.13, S11.16, and S10.8's brand by serie prefix (moved here by the `fetch` plan). Requirements: R-7.1 to R-7.10, R-7.13, R-8.1, R-8.2, R-9.7. Rules: BR-D01, BR-D02, BR-D03, BR-D06, BR-D07, BR-D08, BR-D10, BR-S02. Design: `specs/03-design.md` §3, §4, §7, §16; `specs/spikes/excel.md`.

**Goal (M3):** the La Leña invoice from M2 reaches `DELIVERING`; the bot makes the folder `2026-11-Support_1_LaLena` in the owner's personal OneDrive with the filled Excel, the PDF, the XML and a manifest, then sends the email from the personal account to the owner's test inbox. The invoice ends `DONE` with BR-D10. With `dry_run` instead, it ends `BATCHED` with BR-D07 and [Ya lo envié], and the tap makes it `DONE` with BR-D08.

**Needs from the owner first (none ready on 2026-11-13; only step 25 waits for them):**

- An Entra app registration for personal Microsoft accounts (public client, device code allowed), its client id, and the delegated scopes `offline_access Files.ReadWrite Mail.Send`.
- The reimbursement Excel template copied to the host, outside git, and its SHA-256.
- A test inbox address for the MVP mail (D68), set only in the host's private config.

## Design choices

These settle places where the specs are ambiguous or ahead of this phase. Step 1 writes them into the specs as D84 to D90.

- **D84, Graph over `httpx`, MSAL only for tokens.** OneDrive and mail calls are plain REST on the app's one `httpx.AsyncClient`, faked in tests with `httpx.MockTransport`. `msal` (sync) runs in `asyncio.to_thread` and only gets and refreshes tokens. Authority `https://login.microsoftonline.com/consumers`. The client id is config, not a secret (a public client has none).
- **D85, token caches are age files.** Each account's MSAL cache is `<data_dir>/tokens/<account>.msal.age`, encrypted to the recipient of the bot's own age identity and rewritten when MSAL changes it. Nothing goes in `kv`. A missing, empty or refused cache raises `AuthRequired` → `PENDING_AUTH` with BR-D01.
- **D86, the template is a local file.** `excel_template.template` is a path on the host (bot storage), pinned by `template_sha256`, checked before every use (BR-D02). The bot does not read it from OneDrive.
- **D87, how cells are patched (D71).** Text cells are written as inline strings (`t="inlineStr"`), so `sharedStrings.xml` stays byte-identical. A written cell that held a formula loses it, and its entry is dropped from `calcChain.xml`. Formulas that depend on written cells get their cached value recomputed by a small evaluator (numbers, cell refs, ranges, `+ - * /`, `MIN`, `MAX`, `SUM`); a dependent formula outside that subset is BR-D02. Every package part except the sheet and `calcChain.xml` is copied byte for byte.
- **D88, `seq` is claimed in the database.** The folder sink lists the month's folders (root and `{YYYY}/`), then the pipeline claims `seq = max(listed, already claimed in flow_data_json) + 1` in one short transaction under an advisory lock per flow and month, and stores it before the folder exists. A retry reuses the stored `seq` and folder name. The folder is created with `conflictBehavior: fail`; a 409 on the claimed name means a retry and is accepted.
- **D89, no automatic resend.** An automatic mail mode commits the batch as `sending` before the Graph call and `sent` after it. A job that finds its batch `sending` never resends: the batch goes `failed` with BR-D03. When `mail.send_enabled` is false, every mode is replaced by `dry_run` at startup (R-7.7). `send_enabled: true` requires `env: production`.
- **D90, overrides resolve like documents.** A `motivo=` / `descripcion=` text (or a photo caption) goes to the quoted message's invoice, else to the sender's most recent invoice whose batch is not `sent`. Outbound message ids are not stored yet, so quoting the bot's own summary falls back to the most recent invoice.
- **Moved out of this phase (step 1 records it in the roadmap):**
  - BR-R09's one-tap brand question goes to `widen-b` with reply handling (D80): no store in `config/stores/` has two brands today. This phase picks the brand by serie prefix, or the only brand.
  - The PKCE loopback login and the work-account attempt (S11.1, S0.8) go to `widen-b` with `inbox_rule`: the personal account allows device code.
  - The `_Sent` rename goes to `widen-b` with S11.12; the `payments` table and `payment_id` go there with S11.11 (`/pagado`), their first caller. Spec 03 §3 is amended.
  - `/pendientes` listing a batch `prepared` for 2 days (S11.4) goes with the commands of `widen-b`.
  - The `PORTAL_MISSING` and `TEMPLATE_INVALID` gaps are logged with a `shortcut:` until the gap service (E15, `widen-b`).
- **R-8.2 for flow B** is met by BR-D01, BR-D02 and BR-D03 here, and by BR-S03's `FAILED` message, which exists.

Conventions: everything in `docs/plan-mvp-read.md` "Conventions" still applies. In addition:

- New packages `invoicebot.sinks` and `invoicebot.auth` join `.importlinter`'s `zero-consumer` contract and get a `sinks-are-low` contract: they never import `pipeline`, `queue` or `store`. The pipeline passes them what they need.
- Graph's base URL is one constant, `https://graph.microsoft.com/v1.0`. Every Graph call has a timeout from config and logs the operation, the status and the duration; never a path, a file name, an address or a token (R-9.16).
- No test sends mail or touches a real account. The synthetic template is built in code; no `.xlsx` is committed.
- Lint for every step: `uv run ruff check src tests scripts migrations && uv run ruff format --check src tests scripts migrations && uv run mypy src && uv run lint-imports`.

Out of scope here: `inbox_rule`, `laptop_outlook`, `forward_to_self`, `eml_draft`, the laptop agent, `onedrive_work`, `/pagado`, renames and the year move, the go-live import, `email_inbox`, gaps.

## Step 1: Record the design choices `[inline: decision text across specs]`

Files: `specs/05-open-questions.md`, `specs/03-design.md`, `docs/plan-mvp.md`.

- Add D84 to D90 to spec 05 with the text of "Design choices" above.
- Spec 03 §3: the `file-b` migration adds `flow_data_json`, the `prepared` batch state and the `eml` and `manifest` file kinds; `payments` and `payment_id` arrive with S11.11. §7: the template path is local (D86); the target YAML gains `env` and `mail.send_enabled`. §10: `auth/msal_cache.py` as listed; Graph clients in `sinks/onedrive.py` and `sinks/mailer_graph.py`.
- Roadmap: the items "Moved out of this phase" go under a new "Moved by the `file-b` plan" heading; the drift items this phase closes (`example.yaml` flow B shape, `files.kind`) are struck when step 4 and step 3 land, in the finish commit.

Test: `grep -c "^| D8[4-9]\|^| D90" specs/05-open-questions.md` prints 7.

## Step 2: Add `msal` `[inline: dependency vetting through py-add-package]`

Files: `pyproject.toml`, `uv.lock`, `.importlinter`.

- Add `msal` (pinned) with the `py-add-package` skill.
- `.importlinter`: add `invoicebot.sinks` and `invoicebot.auth` to `zero-consumer`'s sources; add the `sinks-are-low` contract (sources `invoicebot.sinks`, `invoicebot.auth`; forbidden `invoicebot.pipeline`, `invoicebot.queue`, `invoicebot.store`).

Test: `uv run python -c "import msal" && uv run lint-imports`.

## Step 3: The flow B migration `[worker]`

Files: `migrations/versions/0005_flow_b.py`, `tests/integration/test_schema.py`.

- Revision `0005` after `0004`, plain SQL through `op.execute`, expand only:
  - `ALTER TABLE invoices ADD COLUMN flow_data_json JSONB NOT NULL DEFAULT '{}'::jsonb`.
  - Replace the `batches.state` check with one that also allows `prepared` (drop the old constraint by its name, add the new one; find the name in `0001_initial.py` or `pg_constraint`).
  - Replace the `files.kind` check with one that also allows `eml` and `manifest`.
  - `downgrade` reverses each.
- Tests in `test_schema.py` (R-7.2, R-7.4, R-7.8): an invoice gets `flow_data_json = '{}'` by default; a batch accepts `prepared` and still rejects `bogus`; a file row accepts `eml` and `manifest` and still rejects `bogus`.

Test: `uv run pytest -q tests/integration/test_schema.py`.

## Step 4: Flow B config shape `[worker]`

Files: `src/invoicebot/config/models.py`, `config/example.yaml`, `tests/unit/test_config_models.py`.

Target: spec 03 §7, with only what this phase uses. All models `extra="forbid"`, frozen like their neighbours.

- Top level: `env: Literal["development", "production"]`; `mail: Mail` with `send_enabled: bool = False`; `microsoft: Microsoft` with `client_id: str` (pattern of a GUID) and `timeout_s: float = Field(gt=0)`. A model validator rejects `mail.send_enabled: true` unless `env == "production"` (R-7.7).
- `Profile`: add `legal_name: str` (used by file names, R-7.2); remove `can_send_batch` (post-MVP `on_command`).
- `Flow`: add `reimbursement: Reimbursement | None = None`, where `Reimbursement` has `cap: Decimal = Field(gt=0)`.
- Replace `OneDriveSink`, `MailerTrigger` and `MailerSink` with:
  - `OneDriveFolderSink`: `kind: Literal["onedrive_folder"]`, `targets: list[Literal["onedrive_personal"]]` (min 1), `root: str` (must start with `/`), `folder: str`, `files: SinkFiles` (`pdf: str`, `xml: str`), `manifest: str = ".invoicebot.json"`.
  - `ExcelTemplateSink` (reworked): `kind: Literal["excel_template"]`, `template: Path`, `template_sha256: str` (64 lowercase hex), `allow_macros: bool = False`, `output: str`, `sheet: str`, `cells: dict[str, str]` (keys match `^[A-Z]{1,3}[1-9][0-9]{0,6}$`), `defaults: ExcelDefaults` (`motivo: str`, `descripcion: str`). A `.xlsm` template without `allow_macros` is rejected (R-7.10).
  - `MailerSink`: `kind: Literal["mailer"]`, `trigger: Literal["per_invoice"]`, `modes: list[Literal["dry_run", "graph"]]` (min 1, unique), `account: Literal["personal"]`, `to: list[EmailStr-like str]` (min 1; validate with a simple `^[^@\s]+@[^@\s]+$` pattern, no new dependency), `cc: list[str] = []`, `subject: str`, `body: str`, `attach: list[Literal["excel_template", "pdf", "xml"]]`.
- `example.yaml`: move `work_microsoft` to the shape above with synthetic values (`me@example.com`, a zero SHA such as 64 `0`s, template `/data/templates/reembolsos.xlsx`, client id `00000000-0000-0000-0000-000000000000`); add `env: development`, `mail: {send_enabled: false}`, `microsoft`, `legal_name` on each profile; drop `can_send_batch`. Keep flow A's sinks as they are.
- Tests (R-7.7, R-7.10, R-7.13, D53): `example.yaml` loads; `send_enabled: true` with `env: development` is rejected; with `production` accepted; an `.xlsm` template without `allow_macros` is rejected; an unknown mail mode, a duplicate mode and an empty `to` are rejected; a cell key `16B` is rejected; `reimbursement.cap: 0` is rejected; a flow without `reimbursement` loads with `None`.

Test: `uv run pytest -q tests/unit/test_config_models.py tests/unit/test_config_checks.py`.

## Step 5: Template expressions `[worker]`

Files: `src/invoicebot/sinks/__init__.py`, `src/invoicebot/sinks/expr.py`, `tests/unit/test_sinks_expr.py`.

The tiny safe formatter of spec 03 §7, for folder names, file names, cells, subject and body.

- `render(template: str, values: Mapping[str, object]) -> str`: `string.Formatter` subclass. A field is a bare name or `name.attr` (one level, only for a value that is a `Mapping`; `profile.legal_name` reads `values["profile"]["legal_name"]`). Indexing (`[`), conversions (`!r`), nested fields in a format spec and any other attribute access raise `ExprError`. A `date` value accepts `strftime` specs (`{fecha:%Y}`); a `Decimal` uses the normal format spec. An unknown name raises `ExprError` naming it.
- `field_names(template: str) -> frozenset[str]`: the top-level names a template uses (`profile.legal_name` → `profile`), raising `ExprError` on the same forbidden forms.
- `ExprError(ValueError)`.
- Tests (R-7.1, R-7.2, R-7.6): `"{fecha:%Y}-{fecha:%m}-Support_{seq}_{comercio}"` with `date(2026, 11, 5)`, 1, `LaLena` → `2026-11-Support_1_LaLena`; `"{profile.legal_name}.pdf"` renders; `"Support release {fecha:%d/%m/%Y}"` → `Support release 05/11/2026`; `{x[0]}`, `{x!r}`, `{x.__class__}`, `{a.b.c}`, `{x:{y}}` and `{missing}` each raise `ExprError`; `field_names` returns `{"fecha", "seq", "comercio"}` for the folder template.

Test: `uv run pytest -q tests/unit/test_sinks_expr.py && uv run lint-imports`.

## Step 6: Startup checks for flow B `[worker]`

Files: `src/invoicebot/config/checks.py`, `tests/unit/test_config_checks.py`.

`config` is a leaf package and may not import `sinks`; `field_names` comes in as an argument of the new check (`check_config.py` passes `sinks.expr.field_names`), or move the parse into `config/` if that is simpler. Read how `checks.py` reports `Problem`s and follow it.

- Every template of a flow B sink (`folder`, `files.*`, `cells` values, `defaults.*`, `subject`, `body`) uses only known names: `fecha`, `seq`, `comercio`, `serie`, `numero`, `ruc_emisor`, `razon_social`, `base`, `igv`, `service_charge`, `total`, `motivo`, `descripcion`, `profile`. `defaults.descripcion` may use `comercio` but not `motivo`/`descripcion`. A failure is a `Problem` naming the flow, the sink and the field (BR-D02 at startup).
- `folder` must use `seq` and `fecha` with `%Y` and `%m` (R-7.1).
- A flow with a `mailer` sink lists `excel_template` in `attach` only if it has an `excel_template` sink.
- Tests (BR-D02, R-7.1): `example.yaml` has no problems; an unknown name in `cells` is reported with its path; a folder template without `{seq}` is reported; `attach: [excel_template]` without the sink is reported.

Test: `uv run pytest -q tests/unit/test_config_checks.py`.

## Step 7: Comercio and folder numbers `[worker]`

Files: `src/invoicebot/sinks/folders.py`, `tests/unit/test_sinks_folders.py`.

Pure functions, no I/O.

- `comercio_for(store: Store | None, serie: str, razon_social: str) -> str | None` (R-7.1, R-5.8): with a store, the brand whose `serie_prefix` has a prefix of `serie`; else the only brand when there is one; else `None` (the BR-R09 question is `widen-b`'s). Without a store, the CamelCase slug of the first two words of `razon_social`, ASCII only (`"Restaurante La Leña S.A.C."` → `RestauranteLa`; accents folded, punctuation dropped).
- `seq_pattern(folder_template: str, year: int, month: int) -> re.Pattern[str]`: built from the template's parts: literals escaped, `{fecha:%Y}` → the year, `{fecha:%m}` → the month with an optional leading zero (`0?11`, `0?7`), `{seq}` → `(\d+)`, any other field → `.+?`; then `(?:_.*)?$` for a status suffix (R-7.11). Anchored at the start.
- `max_seq(names: Iterable[str], pattern: re.Pattern[str]) -> int`: the largest captured number, 0 when none match.
- Tests (R-7.1): prefix `F0` picks its brand for serie `F001`; one brand and no prefix → that brand; two brands, no match → `None`; no store → slug; `max_seq` over `2026-7-Support_3_KFC`, `2026-07-Support_5_X_Sent`, `2026-07-Support_2_Y_PAID_15_12___144_6`, `2026-08-Support_9_Z`, `notes.txt` for July 2026 → 5; for a month with no folders → 0; a name like `2026-071-Support_9_X` does not match July.

Test: `uv run pytest -q tests/unit/test_sinks_folders.py`.

## Step 8: A synthetic Excel template for tests `[worker]`

Files: `tests/xlsx_factory.py`, `tests/unit/test_xlsx_factory.py`.

A test helper (not a test module) that builds a small, valid `.xlsx` package in memory with the stdlib `zipfile`, shaped like the owner's template (spec 05 D71) but with no real data:

- `build_template() -> bytes` with parts: `[Content_Types].xml`, `_rels/.rels`, `docProps/core.xml`, `xl/workbook.xml` (one sheet named `Template`), `xl/_rels/workbook.xml.rels`, `xl/styles.xml`, `xl/sharedStrings.xml` (a few strings), `xl/worksheets/sheet1.xml`, `xl/worksheets/_rels/sheet1.xml.rels` → `xl/drawings/drawing1.xml` holding two shapes (`xdr:sp`) and one picture (`xdr:pic`) → `xl/media/image1.png` (a valid 1×1 PNG), `xl/calcChain.xml`, `customXml/item1.xml`, `xl/printerSettings/printerSettings1.bin` (a few bytes).
- Sheet content: `C13` a shared string; `B16`, `C16`, `E16` empty styled cells; `F16` and `G16` numbers; `H16` the formula `F16*0.1` with a cached value; `I16` the formula `F16+G16+H16` with a cached value; `J16` the formula `MIN(I16, 50)` with a cached value; `K20` an unrelated formula `1+1`. `calcChain.xml` lists `H16`, `I16`, `J16`, `K20`.
- Constants for the cell addresses and the part names, so the patch tests use them.
- Tests: the package opens with `zipfile`, every listed part is present, each XML part parses with `lxml`, and the drawing has 2 `sp` and 1 `pic`.

Test: `uv run pytest -q tests/unit/test_xlsx_factory.py`.

## Step 9: The formula subset `[worker]`

Files: `src/invoicebot/sinks/xlsx_formula.py`, `tests/unit/test_sinks_xlsx_formula.py`.

- `parse(formula: str) -> Formula`: a small recursive-descent parser for numbers, cell refs (`A1`, `$A$1`), ranges (`A1:B3`, only inside a function), `+ - * /` with the usual precedence, unary minus, parentheses, and the functions `MIN`, `MAX`, `SUM` (case-insensitive, comma-separated arguments). Anything else (strings, other functions, sheet-qualified refs, `%`, `^`, comparisons) raises `UnsupportedFormula(ValueError)`.
- `Formula.refs() -> frozenset[str]`: every cell it reads (ranges expanded; a range over 1 000 cells raises `UnsupportedFormula`).
- `Formula.evaluate(values: Mapping[str, Decimal]) -> Decimal`: a missing cell is 0, as in Excel; division by zero raises `UnsupportedFormula`.
- Tests (R-7.4, D87): `MIN(I16, 50)` with `I16=63.5` → 50 and with 42 → 42; `F16+G16+H16`; `SUM(F16:H16)`; `-A1*(B1+2)/4`; `$F$16` equals `F16`; `refs()` of `SUM(A1:B2)` → four cells; `IF(A1>1,1,0)`, `Sheet2!A1`, `"x"`, `A1^2` each raise `UnsupportedFormula`.

Test: `uv run pytest -q tests/unit/test_sinks_xlsx_formula.py`.

## Step 10: Patch cells inside the package (D71, D87) `[worker:high: reasoning: OOXML part rewriting with byte-identical copies, calcChain and cached-value ordering]`

Files: `src/invoicebot/sinks/xlsx_patch.py`, `tests/unit/test_sinks_xlsx_patch.py`.

- `patch_cells(template: bytes, sheet: str, writes: Mapping[str, str | Decimal]) -> bytes`:
  - Find the sheet's part through `xl/workbook.xml` and its rels (never assume `sheet1.xml`). Unknown sheet → `TemplateError`.
  - For each write: create the row and cell in order if missing (keep the row and cell order Excel requires); keep the cell's `s` style attribute; remove any `<f>` and `<v>`; text as `t="inlineStr"` `<is><t xml:space="preserve">…</t></is>`, escaped first: a text starting with `= + - @`, tab or carriage return gets a leading `'` (R-7.10); a `Decimal` as `<v>` in plain notation with no `t`.
  - Recompute cached values: every formula cell left in the sheet whose formula (step 9) reads a written cell, directly or through another formula, gets its `<v>` rewritten from the evaluated value, in dependency order; a cycle or an `UnsupportedFormula` among those dependents → `TemplateError`. Formulas that read no written cell are not touched.
  - `calcChain.xml`: drop the `<c>` entries of written cells that held a formula; if none remain, keep the part as is when nothing was dropped.
  - Write a new zip: every other entry copied from the template with its original `ZipInfo` (name, date, compression) and its original bytes, in the original order.
  - Refuse input that is not a zip, has more than 200 parts, or would expand past 20 MB (`TemplateError`): it comes from disk but stays capped.
- `TemplateError(ValueError)`.
- Tests on `tests/xlsx_factory.py` (R-7.4, R-7.10, D71): writing `C13="Support release"`, `F16=Decimal("40.00")`, `G16=Decimal("7.20")`, `E16="F001-123"`, `C16="=cmd"`:
  - every part except the sheet and `calcChain.xml` is byte-identical to the template's;
  - `C13` is an inline string, `C16` reads `'=cmd`, `F16` is `40.00` with no formula;
  - `H16`'s cached value is 4.0, `I16`'s is 51.2, `J16`'s is 50, `K20`'s is unchanged;
  - writing `H16` (a formula cell) removes its formula and its `calcChain` entry, and `I16` and `J16` are recomputed from the new value;
  - the drawing still holds 2 shapes and the picture; the cell `s` styles are unchanged;
  - an unknown sheet, a non-zip input and a dependent formula `IF(...)` (patch the factory's sheet in the test) raise `TemplateError`.

Test: `uv run pytest -q tests/unit/test_sinks_xlsx_patch.py`.

## Step 11: The Excel sink values `[worker]`

Files: `src/invoicebot/sinks/excel_template.py`, `tests/unit/test_sinks_excel_template.py`.

- `build_excel(sink: ExcelTemplateSink, values: Mapping[str, object], overrides: Mapping[str, str]) -> bytes` (R-7.4):
  - Read `sink.template`; missing file, SHA-256 not equal to `template_sha256` → `TemplateInvalid` (BR-D02), with the reason (`missing`, `hash`, `cells`) and no path or hash in the message.
  - `motivo` and `descripcion`: the override when present, else the rendered default (`descripcion` default may use `comercio`).
  - Each `cells` entry: if the expression is exactly one field naming a `Decimal` amount (`{base}`, `{igv}`, `{service_charge}`, `{total}`), write the `Decimal`; else write the rendered text (step 5's `render`). A missing amount writes `Decimal("0")`.
  - Call `patch_cells`; a `TemplateError` or `ExprError` → `TemplateInvalid` with reason `cells`.
- `TemplateInvalid(Exception)` with a `reason` attribute.
- Tests (R-7.4, R-7.10, BR-D02), with a template from `tests/xlsx_factory.py` written to `tmp_path`: the right cells get the right values; `motivo` override wins over the default; the default description renders `Cena en LaLena`; amounts are numbers; a wrong SHA raises reason `hash`; a missing file raises `missing`; a cell expression `{nope}` raises `cells`.

Test: `uv run pytest -q tests/unit/test_sinks_excel_template.py`.

## Step 12: Token caches (S11.1, D85) `[worker]`

Files: `src/invoicebot/auth/__init__.py`, `src/invoicebot/auth/msal_cache.py`, `tests/unit/test_auth_msal_cache.py`.

- `class TokenProvider` (long-lived state, one per account): `__init__(self, client_id: str, account: str, scopes: Sequence[str], cache_path: Path, identity: pyrage.x25519.Identity, http_client: object | None = None)`.
  - `async token(self) -> str`: in `asyncio.to_thread`: load the cache (decrypt with `identity`; missing file → empty cache), build `msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=cache, http_client=http_client)`, `acquire_token_silent(scopes, account=first account)`; no account or no token or an `error` → `AuthRequired(account)`. If `cache.has_state_changed`, write it back encrypted to `identity.to_public()`, atomically (temp file + `os.replace`, mode `0o600`).
  - `login_device_code(self, show: Callable[[str], None]) -> None` (sync, for the CLI): `initiate_device_flow`, `show(flow["message"])`, `acquire_token_by_device_flow`; an `error` → `AuthRequired`; save the cache as above.
- `AUTHORITY = "https://login.microsoftonline.com/consumers"`; `AuthRequired(Exception)` carries the account name only.
- The age identity: if `SecretStore` does not expose the loaded identity, pass the identity file path instead and load it with the same helper `secrets.py` uses (`_load_identity`); do not duplicate the parsing.
- Tests (R-7.9, BR-D01, R-9.9), with a fresh `pyrage.x25519.Identity.generate()` and a fake `http_client` (an object with `get`/`post` returning objects with `status_code`, `text`, `headers` and `json()`, as msal expects): no cache file → `AuthRequired`; a cache seeded by a fake device-code login returns the access token from `token()` without a network call; a refresh served by the fake `post` rewrites the cache; the cache file on disk is not valid JSON (it is encrypted) and has mode `0o600`; an `invalid_grant` response → `AuthRequired`.

Test: `uv run pytest -q tests/unit/test_auth_msal_cache.py && uv run lint-imports`.

## Step 13: The OneDrive client `[worker]`

Files: `src/invoicebot/sinks/base.py`, `src/invoicebot/sinks/onedrive.py`, `tests/unit/test_sinks_onedrive.py`.

- `sinks/base.py`: `class Drive(Protocol)` with `list_names(path: str) -> list[str]`, `create_folder(parent: str, name: str) -> None`, `upload(path: str, data: bytes) -> None`; `class TokenSource(Protocol)` with `async token() -> str`. Docstrings state the invariants: paths are absolute from the drive root, `create_folder` is idempotent for an existing folder of that name, `upload` replaces.
- `sinks/onedrive.py`: `class GraphDrive` implementing `Drive` over an `httpx.AsyncClient`, a `TokenSource` and `timeout_s`:
  - `list_names`: `GET /me/drive/root:{path}:/children?$select=name&$top=200`, following `@odata.nextLink` (only when it starts with the Graph base URL); a 404 → `[]`.
  - `create_folder`: `POST /me/drive/root:{parent}:/children` with `{"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"}`; a 409 is accepted (D88); a 404 on the parent creates it first, once.
  - `upload`: up to 4 MB, `PUT /me/drive/root:{path}:/content`; above, `POST …:/createUploadSession` with `conflictBehavior: replace`, then `PUT` chunks of 10 × 320 KiB to the session URL (no auth header on the session URL, as Graph asks); the session URL must be `https://` (R-5.10).
  - Path segments are percent-encoded; a segment that is empty, `.` or `..`, or holds `/`, `\`, `:` or a control character raises `ValueError` before any request.
  - A 401 → `AuthRequired` (import from `invoicebot.auth.msal_cache`); 429 and 5xx → `GraphTransient(Exception)` (the queue retries); other 4xx → `GraphError(Exception)` with the status only.
- Tests with `httpx.MockTransport` (R-7.1, R-7.2, R-7.3): listing follows `nextLink` and returns names; a missing folder lists `[]`; create sends `conflictBehavior: fail` and accepts 409; a missing parent is created once; a 3 MB upload is one `PUT`; a 5 MB upload opens a session and sends the right number of chunks with correct `Content-Range` headers and no `Authorization` on chunk requests; `..` in a path raises before a request; 401, 503 and 400 map to their exceptions; a `nextLink` to another host is not followed.

Test: `uv run pytest -q tests/unit/test_sinks_onedrive.py && uv run lint-imports`.

## Step 14: The email and its `.eml` `[worker]`

Files: `src/invoicebot/sinks/eml.py`, `tests/unit/test_sinks_eml.py`.

- `@dataclass(frozen=True) class Attachment(name: str, data: bytes, mime: str)` and `OutgoingMail(to: tuple[str, ...], cc: tuple[str, ...], subject: str, body: str, attachments: tuple[Attachment, ...])` (put both in `sinks/base.py` if that is where step 13 left the ports; one home).
- `build_eml(mail: OutgoingMail) -> bytes` with `email.message.EmailMessage` and `email.policy.SMTP`: `To`, `Cc` when present, `Subject` exactly as given (D69), `Date`, the plain-text body, each attachment with its MIME type and file name (non-ASCII names encoded by the policy). No `From` (the sender is the account).
- Tests (R-7.6, R-7.7, D69): the subject is byte-for-byte `Support release 05/11/2026`; three attachments come back with their names and bytes through `email.message_from_bytes`; a file name with `ñ` round-trips; no `Cc` header when `cc` is empty; a CR or LF in the subject raises `ValueError` (header injection).

Test: `uv run pytest -q tests/unit/test_sinks_eml.py`.

## Step 15: Graph mail `[worker]`

Files: `src/invoicebot/sinks/mailer_graph.py`, `tests/unit/test_sinks_mailer_graph.py`.

- `class GraphMailer` over an `httpx.AsyncClient`, a `TokenSource` and `timeout_s`; `async send(mail: OutgoingMail) -> None`:
  - Total attachment size up to 3 MB: one `POST /me/sendMail` with `{"message": {subject, body {contentType: "Text"}, toRecipients, ccRecipients, attachments: [#microsoft.graph.fileAttachment, base64]}, "saveToSentItems": true}`; 202 is success.
  - Above 3 MB (R-7.6): `POST /me/messages` (draft, no attachments), then per attachment `POST /me/messages/{id}/attachments/createUploadSession` and chunked `PUT`s as in step 13 (no auth header on the session URL), then `POST /me/messages/{id}/send`.
  - Errors map as in step 13 (`AuthRequired`, `GraphTransient`, `GraphError`); reuse step 13's helpers rather than copying them.
- Tests with `httpx.MockTransport` (R-7.6, R-7.7): a small mail is one `sendMail` call whose JSON has the exact subject, the recipients and three base64 attachments; a 4 MB attachment goes through draft, upload session and send, in that order; 401 → `AuthRequired`; 503 → `GraphTransient`.

Test: `uv run pytest -q tests/unit/test_sinks_mailer_graph.py`.

## Step 16: Flow data and batches in the store `[worker]`

Files: `src/invoicebot/store/flow_b.py`, `tests/integration/test_flow_b_store.py`.

Store functions take the caller's connection and never commit.

- `load_flow_data(conn, invoice_id) -> dict[str, Any]` and `merge_flow_data(conn, invoice_id, patch: Mapping[str, Any]) -> None` (`flow_data_json || %s::jsonb`).
- `claim_seq(conn, flow_id: str, year: int, month: int, listed_max: int, invoice_id: int) -> int` (D88): `pg_advisory_xact_lock(hashtext('seq:' || flow_id || ':' || year || '-' || month))`; if the invoice already has `seq` in its flow data, return it; else `max(listed_max, max((flow_data_json->>'seq')::int) over this flow's invoices whose flow_data year and month match) + 1`, stored with `year` and `month` in the invoice's flow data, returned.
- `open_batch(conn, invoice_id, flow_id, state: Literal["prepared", "sending"]) -> int`: the invoice's batch if it has one, else a new batch of one in `state`, linked through `invoices.batch_id` (R-7.5).
- `get_batch_state(conn, invoice_id) -> str | None`, `set_batch_state(conn, batch_id, state)` (sets `sent_at = now()` when `sent`).
- `take_photo_paths(conn, invoice_id) -> list[Path]`: returns the photo files' `local_path`s and sets them to `NULL` (R-9.7); the caller deletes the files after commit.
- Tests against Postgres (R-7.1, R-7.5, R-7.8, R-9.7, D88): two invoices in one flow and month claim 1 and 2; a second claim for the same invoice returns its first number; `listed_max = 4` makes the next claim 5; another month starts again from its listed max; two claims in parallel connections get different numbers; `open_batch` twice returns the same batch; `sent` sets `sent_at`; `take_photo_paths` returns the path once and `[]` the second time.

Test: `uv run pytest -q tests/integration/test_flow_b_store.py`.

## Step 17: The folder sink `[worker]`

Files: `src/invoicebot/sinks/onedrive_folder.py`, `tests/unit/test_sinks_onedrive_folder.py`.

- `sinks/base.py` gains (if step 13 did not add it) `@dataclass(frozen=True) class DeliveryContext`: `invoice_id`, `values: Mapping[str, object]` (the step 6 names), `pdf: bytes`, `xml: bytes`, `claim_seq: Callable[[int], Awaitable[int]]` (the pipeline's port over step 16, given the listed max), `saved: Mapping[str, Any]` (the invoice's flow data).
- `async file_folder(sink: OneDriveFolderSink, drive: Drive, ctx: DeliveryContext) -> FolderResult` (R-7.1, R-7.2):
  - If `ctx.saved` has `folder`, reuse it (no listing, no claim).
  - Else list `root` and `root/{YYYY}` (paid folders live there, R-7.11), `max_seq` with `seq_pattern`, `seq = await ctx.claim_seq(listed_max)`, render the folder name with `seq`.
  - `create_folder(root, name)`; upload the PDF and XML under their rendered names and the manifest `{"invoice_id": …, "version": 1}` (JSON) as `sink.manifest`.
  - Return `FolderResult(folder: str, path: str, seq: int)`; the pipeline saves `folder` and `seq`.
- Tests with an in-memory fake `Drive` (R-7.1, R-7.2, D88): an empty month gives `2026-11-Support_1_LaLena` with the PDF, XML and manifest; existing `2026-11-Support_2_X` and `2026/2026-11-Support_4_Y_Sent_PAID_…` give seq 5 (the claim gets 4); a saved folder is reused without listing; file names use `profile.legal_name`; the manifest holds the invoice id.

Test: `uv run pytest -q tests/unit/test_sinks_onedrive_folder.py`.

## Step 18: Mail modes `[worker]`

Files: `src/invoicebot/sinks/mailer.py`, `tests/unit/test_sinks_mailer.py`.

- `build_mail(sink: MailerSink, values: Mapping[str, object], files: Mapping[str, Attachment]) -> OutgoingMail`: subject and body rendered (step 5), `to`/`cc` from config, attachments in `sink.attach` order from `files` (keys `excel_template`, `pdf`, `xml`); a missing one raises `ValueError`.
- `class MailMode(Protocol)`: `automatic: bool`; `async deliver(invoice_id: int, mail: OutgoingMail) -> str | None` (returns a local `.eml` path for an assisted mode, `None` for a sent one). Docstring: an automatic mode sends exactly once per call; the caller guards against repeats (D89).
- `class DryRun` (`automatic = False`): writes `build_eml(mail)` to `<data_dir>/mail/<invoice_id>.eml` (replacing it, so an override rewrites it) and returns the path; logs the invoice id and the size only.
- `class GraphSend` (`automatic = True`): `await mailer.send(mail)`, returns `None`.
- `mail_modes(cfg: Config, dry_run: DryRun, graph: GraphSend) -> Mapping[str, MailMode]`: `{"dry_run": dry_run, "graph": graph}`, but `graph` maps to `dry_run` when `cfg.mail.send_enabled` is false (R-7.7, D89).
- Tests (R-7.6, R-7.7, D69, D89): `build_mail` renders the exact subject and orders attachments; `DryRun` writes a parseable `.eml` and a second call replaces it; with `send_enabled: false` the `graph` entry is the dry run; with `true` it is `GraphSend`, which calls a fake mailer once.

Test: `uv run pytest -q tests/unit/test_sinks_mailer.py`.

## Step 19: The deliver job `[worker]`

Files: `src/invoicebot/pipeline/deliver.py`, `tests/integration/test_pipeline_deliver.py`.

- `DELIVER_STEP = "deliver"`. `make_deliver_handler(pool, cfg, directory, drive: Drive, modes: Mapping[str, MailMode], data_dir) -> Handler`, acting only on `DELIVERING` (R-9.4) for a flow whose sinks include `onedrive_folder` (other flows: log and return; flow A is `flow-a`'s).
- Inputs: the invoice head, its reading and flow data; the PDF and XML from `files` (`get_file_path`); the XML parsed with `parse_invoice_xml` for `base`, `igv`, `service_charge`, `total` (R-7.4; with no XML the photo's reading, and the summary already flagged it); `comercio` from `comercio_for` (step 7) and saved in flow data; the step-6 value names, with `profile` from `cfg.profiles`.
- Order: `file_folder` (save `folder`, `seq`), `build_excel` and upload it into the folder, `build_mail`, then the first mode in `sink.modes` (only the first, R-7.7):
  - Assisted (`dry_run`): one transaction: `open_batch(..., "prepared")`, record the `.eml` as a file of kind `eml`, state `BATCHED`, BR-D07 with one button `Button(id=button_id(invoice_id, "sent"), title=…)` (R-8.1); `{modes_desc}` names the mode in Spanish (add the text to `messages_es.py`).
  - Automatic (`graph`): a transaction that finds the batch already `sending` → batch `failed`, BR-D03, stop (D89). Else commit `open_batch(..., "sending")`; `send`; then one transaction: batch `sent`, state `DONE` with BR-D10, `take_photo_paths`; after commit delete those files (a missing file is fine).
- Failures: `AuthRequired` → `PENDING_AUTH` with BR-D01 to the sender and `BR-D01_admin` (`provider="microsoft"`) to each admin profile; `TemplateInvalid` → `MANUAL_REQUIRED` with BR-D02 and a `shortcut:` log for the `TEMPLATE_INVALID` gap; `GraphTransient` re-raises (the queue retries; after its limit BR-S03). A `GraphError` on send → batch `failed`, BR-D03, invoice stays `BATCHED`.
- No store missing for the issuer → the slug, and a `shortcut:` log for the `PORTAL_MISSING` gap.
- Tests against Postgres with a fake `Drive` and fake modes (R-7.1–R-7.8, R-8.1, BR-D01, BR-D02, BR-D03, BR-D07, BR-D10, D88, D89): `dry_run` ends `BATCHED`, batch `prepared`, BR-D07 with the [Ya lo envié] button, folder and seq in flow data; `graph` ends `DONE`, batch `sent`, BR-D10, the photo file gone and `local_path` NULL; rerunning the job after `DONE` does nothing; a batch already `sending` is not resent and goes `failed` with BR-D03; `AuthRequired` → `PENDING_AUTH` with the user and admin messages; a bad template SHA → `MANUAL_REQUIRED` with BR-D02; a second run after a crash before the mail step reuses the folder.

Test: `uv run pytest -q tests/integration/test_pipeline_deliver.py`.

## Step 20: The reimbursement cap at routing (S11.13) `[worker]`

Files: `src/invoicebot/pipeline/route.py`, `src/invoicebot/rules/messages_es.py`, `tests/integration/test_pipeline_route.py`.

- After a successful route, if the flow has `reimbursement` and the reading's `total` is above `cap` (R-7.13): state `CONFIRMING` with rule `BR-D06`, no fetch job, the summary followed by BR-D06's text, and two buttons: `continue` (`Continuar`) and `cancel` (`Cancelar`), ids from `button_id`. At or below the cap, or with no `reimbursement`, nothing changes. A total that is missing keeps today's path.
- `messages_es.py`: the button titles as constants (`BUTTON_CONTINUE`, `BUTTON_CANCEL`, `BUTTON_SENT = "Ya lo envié"`), next to the other texts.
- `{total}` and `{cap}` print as `63.50` and `50.00` (two decimals).
- Tests (R-7.13, BR-D06): total 63.50 with cap 50 → `CONFIRMING`, BR-D06 text, two buttons, no `fetch` job; total 50.00 → `FETCHING_DOCS` and no question; a flow without `reimbursement` → unchanged.

Test: `uv run pytest -q tests/integration/test_pipeline_route.py`.

## Step 21: Button presses `[worker]`

Files: `src/invoicebot/pipeline/actions.py`, `src/invoicebot/pipeline/intake.py`, `tests/integration/test_pipeline_actions.py`.

- `pipeline/actions.py`: `VALID_STATES: Mapping[str, frozenset[str]]` = `continue` and `cancel` → `{"CONFIRMING"}`, `sent` → `{"BATCHED"}`; `ACTIONS: Mapping[str, Callable[[AsyncConnection, int, InboundMessage], Awaitable[None]]]`; `async handle_press(conn, msg) -> bool` calls `resolve_reply` (it already sends BR-S02 for a stale press) and runs the action. All in the caller's transaction.
  - `continue`: only when `rule_id` is `BR-D06` (else BR-S02); state `FETCHING_DOCS` and a `fetch` job (R-7.13).
  - `cancel`: state `CANCELLED` with `BR-D06`, a one-line reply (`MESSAGES` already has a cancel text? if not, add `CANCELLED_BY_USER` to `messages_es.py`).
  - `sent` (R-7.8, BR-D08): only when the batch is `prepared`; batch `sent`, state `DONE` with BR-D08 and its reply, photo paths taken (step 16); files deleted after commit by the caller of `handle_inbound` (return them, or do it in `intake` after its transaction).
- `intake.handle_inbound`: a message with `button_id` goes to `handle_press` and returns `IntakeResult(status="stored", invoice_id=…)` after `store_inbound`; a duplicate is still a duplicate (R-1.8).
- Tests (R-3.5, R-7.8, R-7.13, BR-D06, BR-D08, BR-S02): [Continuar] on a BR-D06 invoice → `FETCHING_DOCS` with a fetch job; [Cancelar] → `CANCELLED`; [Ya lo envié] on a `BATCHED` invoice with a prepared batch → `DONE`, batch `sent`, BR-D08, photo `local_path` NULL; the same press again → BR-S02 and nothing else; a press from another profile → nothing; a redelivered press is a duplicate and acts once.

Test: `uv run pytest -q tests/integration/test_pipeline_actions.py tests/integration/test_intake.py`.

## Step 22: Reason and description overrides (S11.16, D70, D90) `[worker]`

Files: `src/invoicebot/pipeline/overrides.py`, `src/invoicebot/pipeline/intake.py`, `tests/integration/test_pipeline_overrides.py`.

- `parse_overrides(text: str) -> dict[str, str]`: `motivo=…` and `descripcion=…` (case-insensitive key, value to the end of its line, trimmed, at most 120 characters, control characters refused); other text → `{}`.
- At intake: a photo whose caption has overrides stores them under `overrides` in its new invoice's flow data.
- A text-only message with overrides: find the invoice (the quoted message's invoice of the same profile, else the profile's most recent invoice not `CANCELLED`/`FAILED` whose batch is not `sent`). If its batch is `sent` (or it is `DONE`), reply BR-S02. Else merge the overrides; if the invoice is `BATCHED` with a `prepared` batch, move it back to `DELIVERING` and queue a `deliver` job, so step 19 rebuilds the Excel and the `.eml` in the same folder (it reuses the saved folder); reply with a one-line confirmation (add the text to `messages_es.py` only if no existing text fits; name it in the summary).
- Tests (R-7.4, D70, BR-S02): a caption `motivo=Viaje` lands in the new invoice's flow data; a later text `descripcion=Almuerzo` on a `BATCHED` prepared invoice moves it to `DELIVERING` with a `deliver` job; on a `DONE` invoice it gets BR-S02 and changes nothing; text without `=` is only stored.

Test: `uv run pytest -q tests/integration/test_pipeline_overrides.py tests/integration/test_intake.py`.

## Step 23: Sign in and resume (S11.1, BR-D01) `[worker]`

Files: `src/invoicebot/cli.py`, `scripts/auth_microsoft.py`, `tests/integration/test_cli_auth.py`.

- `invoicebot auth-microsoft --account personal`: loads settings and config, builds the account's `TokenProvider` (scopes `offline_access Files.ReadWrite`, plus `Mail.Send` when a flow's mailer has `graph` with `account: personal`, R-7.9), runs `login_device_code(print)`, then in one transaction moves every `PENDING_AUTH` invoice to `DELIVERING` with a `deliver` job and prints how many (the BR-D01 "auto-resumes after re-auth"). Exit 0, or 1 with the reason on `AuthRequired`.
- `scripts/auth_microsoft.py`: the name BR-D01's admin text gives; it only calls the CLI entry with `auth-microsoft` and its arguments.
- Tests (R-7.9, BR-D01) with a stubbed `TokenProvider.login_device_code` (pass it in; the CLI builds it through a factory argument that tests replace): two `PENDING_AUTH` invoices become `DELIVERING` with `deliver` jobs; a `DONE` invoice is untouched; a failed login exits 1 and resumes nothing; the scopes include `Mail.Send` only when `graph` is configured.

Test: `uv run pytest -q tests/integration/test_cli_auth.py`.

## Step 24: Wire flow B `[worker]`

Files: `src/invoicebot/app.py`, `src/invoicebot/pipeline/fetch.py`, `tests/integration/test_pipeline_fetch.py`.

- `fetch.py`: the `ok` path also enqueues `DELIVER_STEP` in the same transaction as `DELIVERING` (import the constant from `pipeline/deliver.py`; if that creates a cycle, move the step names to a small `pipeline/steps.py`).
- `app.py`: build one `TokenProvider` for `personal` (cache under `settings.data_dir / "tokens"`, the age identity, `cfg.microsoft`), `GraphDrive` and `GraphMailer` on the app's existing `httpx.AsyncClient`, `DryRun(data_dir)`, `mail_modes(cfg, …)`, and register `DELIVER_STEP: make_deliver_handler(...)`. Nothing signs in at startup; a missing cache surfaces as BR-D01 on the first delivery.
- Test (R-5.1, R-7.1): the existing `ok` test also asserts a `deliver` job; the app factory test (wherever `app.py` is tested today) still starts.

Test: `uv run pytest -q tests/integration/test_pipeline_fetch.py && uv run lint-imports`.

## Step 25: Live run (M3) `[inline: live accounts, the owner's template and a private invoice]`

Waits for the three owner items above.

- Host private config: `microsoft.client_id`, the template path and its SHA-256, `mailer.to` = the test inbox, `env: production` and `mail.send_enabled: true` only for this run on this machine (R-7.7, D68); `mailer.modes: [graph]`.
- `scripts/auth_microsoft.py --account personal`, then the M2 La Leña photo through the fake channel or WhatsApp.
- Check: the folder `2026-11-Support_1_LaLena` in the personal OneDrive (or the next free number) holds the Excel, the PDF, the XML and the manifest; the Excel opens in Excel with its shapes, the right values, and the capped total shown in the OneDrive preview; the test inbox has `Support release dd/mm/yyyy` with three attachments; the chat got BR-D10; the photo is gone from the bot's storage.
- Then once with `modes: [dry_run]`: BR-D07, the `.eml` opens in Outlook, [Ya lo envié] gives BR-D08.
- Record the result in `specs/04-tasks.md` (M3) and set `send_enabled` back to `false`.

Test: the checks above, by hand; `uv run pytest -q tests/integration/test_pipeline_deliver.py` still passes.
