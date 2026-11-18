# Plan: MVP phase 9b · flows-config

Roadmap: `docs/plan-mvp.md` row 9b (D141). It shapes the flows for the owner's real use before the live runs: flow A (RK EXPORT) needs only the PDF, and a third flow files the owner's boletas made out to their DNI as one Sheets row each.

**Goal:** flow A delivers when the PDF arrives, even with no XML. A boleta whose buyer is the owner's DNI is routed by BR-R10 to a Sheets-only flow, skips fetching, and ends `DONE` with one sheet row. Any Sheets sink can use a sheet the bot creates or a sheet the owner already has. `scripts/check_config.py` passes on the owner's real `config/config.yaml`.

**Needs from the owner first:** the DNI flow's sheet. Owner, 2026-11-17: "We should be able to create it or use an existing one, max flexibility", so both are supported (D145). The owner also chose the following:

- Flow A: the PDF is required and the XML is optional. Fetchers and Drive uploads stay as they are.
- DNI flow: one row only, with no fetch and no Drive.

## Design choices

Step 1 writes these into spec 05. They are proposals for the owner's review of this plan.

- **D142 · Flow A requires only the PDF.**
  - Flow A gets `required_docs: [pdf]` and `manual_uploads: [pdf, xml]`.
  - A fetch that brings the XML without the PDF stays `missing_docs`, and the chain moves to the next fetcher (R-5.1, unchanged). The XML is kept whenever it arrives with the PDF.
  - `sunat_sol` stays in the chain; the flow-a live check (S0.4) decides whether it earns its place.
  - Flow A completes with BR-D16 (PDF present). BR-D17 stays for a flow whose PDF is optional.
- **D143 · The owner's DNI and BR-R10.**
  - `owner.dni` is optional. When it is set it must match `^[0-9]{8}$`.
  - BR-R10 routes `when: {num_doc_adq: ["{owner.dni}"], tipo_doc_adq: ["1"]}` to the new flow `personal_dni`, which has `accepted_tipos: ["03"]`.
  - BR-R03 now reads "missing, a DNI that matches no rule, or a RUC that matches no rule".
  - OCR never reads a DNI buyer (`ocr_parse` takes only a second RUC). So a boleta photo with no readable QR falls to BR-R03, whose buttons include the new flow. The live run measures how often this happens.
- **D144 · A Sheets-only flow skips fetching.**
  - When a flow has no `fetchers` and no `required_docs`, the fetch handler moves the invoice from `FETCHING_DOCS` to `DELIVERING` and enqueues the deliver step in one transaction. It writes no attempt row.
  - A flow may have a `google_sheets` sink with no `google_drive` sink. A `google_drive` sink still needs a `google_sheets` sink.
  - A config check rejects `link_photo`, `link_pdf`, `link_xml` and `link_folder` in the columns of a flow that has no Drive sink.
  - The Google deliverer serves both kinds: `google_sheets` is mapped next to `google_drive`.
  - With no Drive sink, the deliverer writes only the row and completes with the new rule **BR-D18**: `✅ Registrado {serie}-{numero} de {razon_social}, S/ {total}.` `Fila: {link_row}`. Outside the window it uses the `completion` template, like BR-D16.
- **D145 · An existing sheet or a created one (answers Q6).**
  - `GoogleSheetsSink` gains `existing: bool = False`.
  - With `existing: true`, `spreadsheet_id` is the owner's sheet, and `auth-google` also requests `https://www.googleapis.com/auth/spreadsheets` for that sink's account (R-6.5 already allows it "only when the owner uses an existing sheet"). The account must sign in again after the flag is turned on.
  - `google-setup` takes `--flow <name>` in place of `--account`. The account comes from the flow's sheets sink.
  - For a created sheet, `google-setup` creates the sheet, and creates the Drive folder only when the flow has a Drive sink.
  - For an existing sheet, `google-setup` calls `ensure_tab`:
    - a missing tab is added with the header `[Clave, *columns]`;
    - an empty tab gets that header;
    - a tab whose row 1 differs makes it exit 1, naming the expected header;
    - it never edits other tabs.
  - `google-setup` prints the ids it created or checked and still writes no config file.

Conventions: lint is `uv run ruff check src tests scripts migrations && uv run ruff format --check src tests scripts migrations && uv run mypy src && uv run lint-imports`. Integration tests need Postgres (`podman compose -f deploy/docker-compose.yml up -d postgres`).

- Every user text is a constant in `rules/messages_es.py`, quoted in spec 06 by step 1 (the parity test needs both).
- No real RUCs, DNIs, phone numbers or photos in tests. Use `tests/synthetic_rucs.txt` and the synthetic DNI `40000000`.
- Integration fixtures date invoices in Lima time.
- HTTP is faked only with `httpx.MockTransport`.

Out of scope here: reading a DNI buyer from OCR, the live runs (9d) and the host work (9c).

## Step 1: Record D142–D145, BR-R10 and BR-D18 `[inline: spec and roadmap text the owner reviews with this plan]`

Files: `specs/05-open-questions.md`, `specs/06-business-rules.md`, `specs/02-requirements.md`, `src/invoicebot/rules/messages_es.py`, `src/invoicebot/rules/catalog.py`, `docs/plan-mvp.md`.

- Spec 05:
  - rows D142 to D145 with the text from "Design choices";
  - Q6 is answered by D145.
- Spec 06, `catalog.py` and `messages_es.py` together:
  - BR-R10 row, message `➡️ {flow.label}`;
  - BR-R03 text per D143;
  - BR-D18 row and text per D144.
- Spec 02 R-6.5: cite D145 next to the `spreadsheets` scope.
- Roadmap row 9b: link this plan.

Test: `uv run pytest -q tests/unit/test_rules_parity.py`.

## Step 2: `owner.dni` in the config `[worker:low]`

Files: `src/invoicebot/config/models.py`, `config/example.yaml`, `tests/unit/test_config_models.py`.

- `Owner` gains `dni: str | None = None`. A field validator accepts `None` or `^[0-9]{8}$` and raises `ValueError("owner.dni must be 8 digits")` otherwise. The pattern is a module constant `DNI_PATTERN`.
- `example.yaml` gets `dni: "40000000"` under `owner`, with the comment `# owner's DNI (boletas, BR-R10)`.
- Tests, each with a docstring starting `D143:`:
  - `example.yaml` loads and `owner.dni == "40000000"`;
  - `dni` absent loads as `None`;
  - `"4000000"` (7 digits) raises;
  - `"4000000A"` raises.

Test: `uv run pytest -q tests/unit/test_config_models.py`.

## Step 3: A flow may have Sheets without Drive `[worker]`

Files: `src/invoicebot/config/models.py`, `tests/unit/test_config_models.py`.

- `Flow._google_sinks_come_in_pairs` keeps "a `google_drive` sink needs a `google_sheets` sink" and drops the rule that a `google_sheets` sink needs a Drive sink before it. Rename it `_google_drive_needs_sheets` and update its docstring (D144).
- Tests, each with a docstring starting `D144:`:
  - a flow with only a `google_sheets` sink loads;
  - a flow with only a `google_drive` sink still raises with the existing message;
  - Drive then Sheets still loads.
- Replace any existing test that asserted the old Sheets-without-Drive error; do not weaken others.

Test: `uv run pytest -q tests/unit/test_config_models.py`.

## Step 4: Config check for Drive links in a Sheets-only flow `[worker]`

Files: `src/invoicebot/config/checks.py`, `tests/unit/test_config_checks.py`.

- A new blocking problem `SHEETS_LINK_WITHOUT_DRIVE` covers this case: in a flow with no `google_drive` sink, a `google_sheets` column whose expression names `link_photo`, `link_pdf`, `link_xml` or `link_folder`.
  - Import the four names from `sinks/drive_files.py` (add `LINK_PHOTO`, `LINK_PDF`, `LINK_XML` constants there if only `LINK_FOLDER` exists, and use them where the literals are set).
  - The location string is `flows.<name>.sinks[<i>].columns.<header>`.
  - Wire it where `_sink_problems` returns `[]` for Google sinks.
- Tests, each with a docstring starting `D144:`:
  - a Sheets-only flow with `Foto: "{link_photo}"` gives exactly one `SHEETS_LINK_WITHOUT_DRIVE` problem naming that column;
  - the same column in a flow with a Drive sink gives none;
  - a Sheets-only flow with `Total: "{total}"` gives none.

Test: `uv run pytest -q tests/unit/test_config_checks.py tests/unit/test_sinks_drive_files.py`.

## Step 5: `existing` sheets and the `spreadsheets` scope `[worker]`

Files: `src/invoicebot/config/models.py`, `src/invoicebot/auth/providers.py`, `tests/unit/test_auth_google_consent.py`.

- `GoogleSheetsSink` gains `existing: bool = False`, with a docstring line citing D145.
- `providers.py` gets the constant `SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"`. `google_scopes_for(account)` adds it when any flow's `google_sheets` sink with `existing: true` names that account. Keep the order stable: identity scopes, `drive.file`, `spreadsheets`, then `gmail.readonly`.
- Tests, each with a docstring starting `D145:`:
  - an account with a created sheet gets no `spreadsheets` scope;
  - the same account with `existing: true` gets it, exactly once, even with two such sinks;
  - a second account without an existing sheet does not get it.

Test: `uv run pytest -q tests/unit/test_auth_google_consent.py tests/unit/test_config_models.py`.

## Step 6: A flow with nothing to fetch goes straight to delivery `[worker]`

Files: `src/invoicebot/pipeline/fetch.py`, `tests/integration/test_pipeline_fetch.py`.

- In the fetch handler, after `flow` is resolved (around line 153) and before `decide`, check for a flow with `not flow.fetchers and not flow.required_docs`. For such a flow, in one transaction:
  - `set_state(conn, invoice_id, DELIVERING, expected=FETCHING_DOCS)`;
  - `enqueue(conn, invoice_id, DELIVER_STEP)`;
  - log `step done` with `outcome="nothing_to_fetch"`;
  - return.
- Put the check in a small helper `_needs_no_fetch(flow) -> bool` with a docstring citing D144.
- Tests, each with a docstring starting `D144:`:
  - a flow with `fetchers: []` and `required_docs: []` moves the invoice to `DELIVERING` with one deliver job and no attempt rows;
  - a flow with `fetchers: []` and `required_docs: [pdf]` still ends in `MANUAL_REQUIRED` with BR-F07 (unchanged path);
  - a superseded job for the first case does nothing.

Test: `uv run pytest -q tests/integration/test_pipeline_fetch.py`.

## Step 7: The Google deliverer writes a row without Drive `[worker]`

Files: `src/invoicebot/pipeline/deliver_google.py`, `tests/integration/test_pipeline_deliver_google.py`.

- `_Sinks.drive` becomes `GoogleDriveSink | None`, and `_sinks()` uses `next(..., None)` for Drive. Sheets stays required: raise `RuntimeError` naming the flow if it is absent.
- `_run_sinks` calls `file_to_drive` only when there is a Drive sink. Without Drive, the row values have no `link_*` keys.
- `_complete` picks the rule:
  - no Drive sink → `RULE_ROW_ONLY = "BR-D18"`, formatted with `serie`, `numero`, `razon_social`, `total` and `link_row` (no `link_folder`);
  - otherwise the existing BR-D17/BR-D16 choice.
  - The local photo is still deleted after the commit (R-9.7).
- Tests, each with a docstring starting `D144:` or `BR-D18:`:
  - a Sheets-only flow appends one row through the fake Sheets transport, makes no Drive request, ends `DONE` with `rule_id` BR-D18, and queues the BR-D18 text;
  - a re-run upserts the same row (no second append);
  - the existing Drive+Sheets tests still pass unchanged.

Test: `uv run pytest -q tests/integration/test_pipeline_deliver_google.py tests/unit/test_pipeline_deliver_google.py`.

## Step 8: Wire the Google deliverer for Sheets-only flows `[worker]`

Files: `src/invoicebot/app.py`, `tests/unit/test_app_wiring.py`.

- `by_kind` maps `GOOGLE_SHEETS_KIND = "google_sheets"` (a constant next to `GOOGLE_DRIVE_KIND`) to the same Google handler.
- `_google_deliver_handler` builds the handler when any flow has a Google sink of either kind (today: Drive only).
- Tests, each with a docstring starting `D144:`:
  - a config whose only Google flow is Sheets-only gets a deliverer for that flow;
  - a config with no Google sinks builds no Google handler;
  - `example.yaml` (after step 11) is not needed here, so build the configs in the test.

Test: `uv run pytest -q tests/unit/test_app_wiring.py`.

## Step 9: `GoogleSheets.ensure_tab` for an existing sheet `[worker]`

Files: `src/invoicebot/sinks/google_sheets.py`, `tests/unit/test_sinks_google_sheets.py`.

- `async def ensure_tab(self, spreadsheet_id: str, tab: str, header: list[str]) -> None` (D145):
  1. Read the spreadsheet's sheet titles (`GET spreadsheets/{id}?fields=sheets.properties.title`).
  2. If `tab` is missing, `batchUpdate` with `addSheet` and write `header` to row 1.
  3. If it exists, read `{tab}!1:1`. When it is empty, write `header`. When it equals `header`, do nothing. Otherwise raise `SheetHeaderMismatch(GoogleError)` whose message gives the tab, the expected header and the found one.
  - Reuse the existing request, error and `_a1` helpers; no new HTTP client.
- Tests with `httpx.MockTransport`, each with a docstring starting `D145:`:
  - missing tab → one `addSheet` and one header write;
  - empty tab → header write only;
  - matching header → no write;
  - different header → `SheetHeaderMismatch` and no write;
  - a 403 → the existing `GoogleError` path.

Test: `uv run pytest -q tests/unit/test_sinks_google_sheets.py`.

## Step 10: `google-setup --flow` creates or checks the flow's sheet `[worker]`

Files: `src/invoicebot/cli.py`, `tests/unit/test_cli_google_setup.py`, `specs/03-design.md`.

- The `google-setup` subparser replaces `--account` with `--flow` (required), and `--folder-name` and `--sheet-title` stay. An unknown flow, or a flow with no `google_sheets` sink, exits 2 through `parser.error` before any request. The account is the sheets sink's `account`; replace `_sheets_sink_for` with a flow lookup.
- `_create_google_setup` returns the lines to print, not a fixed tuple:
  - `root_folder_id: …` only when the flow has a `google_drive` sink (`ensure_folder` as today);
  - for `existing: false`, `sheets.create(...)` and `spreadsheet_id: …`;
  - for `existing: true`, `sheets.ensure_tab(sink.spreadsheet_id, sink.tab, [SHEETS_KEY_COLUMN, *sink.columns])` and `spreadsheet_id: … (checked)`;
  - `SheetHeaderMismatch` exits 1 with its message on stderr.
- The `AuthRequired` hint becomes `run invoicebot auth-google --account <account>`, with the account from the sink.
- Update the docstrings and the `google-setup` paragraph in spec 03.
- Tests, each with a docstring starting `D145:`; replace the `--account` cases:
  - a Drive+Sheets flow prints both ids;
  - a Sheets-only created flow prints only `spreadsheet_id` and makes no Drive request;
  - an existing sheet with a matching header prints `(checked)` and creates nothing;
  - a header mismatch exits 1;
  - an unknown flow exits 2 with no request.

Test: `uv run pytest -q tests/unit/test_cli_google_setup.py`.

## Step 11: Flow A takes the PDF; the DNI flow in `example.yaml` `[worker:low]`

Files: `config/example.yaml`, `tests/unit/test_routing_router.py`, `tests/unit/test_config_checks.py`.

- `personal_google`: `required_docs: [pdf]  # D142: xml is kept when it comes` and `manual_uploads: [pdf, xml]`.
- `owner.dni` is already set (step 2). Add the routing rule after BR-R02:
  `- id: BR-R10` / `when: { num_doc_adq: ["{owner.dni}"], tipo_doc_adq: ["1"] }` / `flow: personal_dni`.
- New flow `personal_dni`:
  - `label: "Boletas a mi DNI"`, `accepted_tipos: ["03"]`, `fetchers: []`, `required_docs: []`, `manual_uploads: []`, `deliver_without_docs: false`;
  - one `google_sheets` sink:
    - `account` and `existing: false` as flow A's sheets sink;
    - `spreadsheet_id: "DNI_SHEET_ID_PLACEHOLDER"`, `tab: "Boletas"`;
    - columns `Fecha`, `RUC`, `Razón social`, `Serie-Número`, `Total`, `Estado SUNAT`, with the same expressions as flow A and no `link_*`;
  - a commented example `# existing: true  # use your own sheet (D145); run auth-google again`.
- Profile `yo` gets `personal_dni` in `flows`; `llm_fallback.flows.personal_dni: false`.
- Tests, each with a docstring starting `BR-R10:` or `D142:`:
  - with `example.yaml`, a reading with `tipo_doc_adq "1"` and `num_doc_adq "40000000"` routes to `personal_dni` by BR-R10;
  - `tipo_doc_adq "1"` with another DNI goes unmatched (BR-R03);
  - the owner RUC still routes BR-R01;
  - `run_checks` on `example.yaml` has no blocking problem.

Test: `uv run pytest -q tests/unit/test_routing_router.py tests/unit/test_config_checks.py tests/unit/test_config_models.py && uv run python scripts/check_config.py config/example.yaml`.

## Step 12: Integration test, a DNI boleta end to end `[worker]`

Files: `tests/integration/test_flow_dni.py` (new).

Model it on `tests/integration/test_flow_a.py` (fixtures, fake channel, fake Google transport, Lima dates). Each test's docstring starts `BR-R10:`, `D144:` or `BR-D18:`. Use synthetic RUCs and the DNI `40000000`.

- A tipo 03 boleta reading with buyer DNI `40000000` (QR path) runs ROUTING → VALIDATING → FETCHING_DOCS → DELIVERING → `DONE` with BR-D18. Along the way it makes exactly one Sheets append, no Drive request and no fetch attempt row, and the outbox holds the BR-R10 summary and the BR-D18 text with the row link.
- The same boleta sent twice ends with BR-E04 on the second (the duplicate check is unchanged).
- A tipo 01 factura with buyer DNI `40000000` is cancelled by BR-R08.

Test: `uv run pytest -q tests/integration/test_flow_dni.py`.

## Step 13: Integration test, flow A with only the PDF `[worker]`

Files: `tests/integration/test_flow_a.py`.

Add these cases; each test's docstring starts `D142:`:

- A fetch that returns only the PDF delivers. Drive gets the photo and the PDF, the row has an empty XML link, and the invoice ends `DONE` with BR-D16.
- A fetch that returns only the XML is `missing_docs`, and the chain tries the next fetcher (the attempt row says so).
- A manual PDF upload in `MANUAL_REQUIRED` delivers.

If an existing flow A test fixture assumed `required_docs: [xml]`, update the fixture, not the assertion's intent.

Test: `uv run pytest -q tests/integration/test_flow_a.py`.

## Step 14: Check the owner's real config `[inline: private config.yaml, owner supplies the DNI and the sheet choice]`

Files: `docs/plan-mvp.md` (the note only). `config/config.yaml` is gitignored and never committed.

- Ask the owner for:
  - their DNI;
  - for each Sheets sink, created or `existing` (with the id for an existing one).
- Apply steps 11's shape to `config/config.yaml`:
  - `owner.dni`, BR-R10, `personal_dni`;
  - flow A's `required_docs: [pdf]`;
  - profile flows.
- Run `uv run python scripts/check_config.py config/config.yaml`. Fix only config values, never code. A code change found here is a new finding for the owner.
- Record in the roadmap's 9b row which checks passed and what the live run must still fill in (sheet ids after `google-setup --flow`, a new `auth-google` sign-in if any sink is `existing`).

Test: `uv run python scripts/check_config.py config/config.yaml` exits 0.

## Step 15: No `None` in a Google completion without a business name `[worker]`

Files: `src/invoicebot/pipeline/deliver_google.py`, `tests/integration/test_flow_dni.py`.

Found by step 12: a QR-only boleta has no `razon_social` (no XML, no OCR name), so BR-D18 reads `✅ Registrado B001-81001 de None, S/ 49.90.`

- In `_complete`, a missing or empty `razon_social` is replaced by `RUC {ruc_emisor}`; BR-D16 and BR-D17 get the same fallback. The fallback lives in one small helper with a docstring citing BR-D18. Message texts are unchanged.
- Test (docstring starts `BR-D18:`): the QR-only case in `test_flow_dni.py` asserts the BR-D18 text contains `de RUC <ruc_emisor>` and never `None`.

Test: `uv run pytest -q tests/integration/test_flow_dni.py tests/integration/test_pipeline_deliver_google.py`.

## Deferred: live check (in 9d `ops-live`)

- `auth-google` with the `spreadsheets` scope when a sink is `existing`, and `google-setup --flow personal_dni`.
- A real DNI boleta through WhatsApp. Record how many boleta photos carry a QR with the DNI, since OCR cannot read it (D143).
- A flow A invoice whose store gives only the PDF.
