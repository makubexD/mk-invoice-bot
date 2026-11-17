# Plan: MVP phase 8 · flow-a

Roadmap: `docs/plan-mvp.md` row 8. Flow A (`personal_google`): Google sign-in (S9.1), the Drive sink (S9.2), the Sheets sink with upsert (S9.3), the flow A completion message (S9.4), the Gmail variant of `email_inbox` (S9.5) and the `sunat_sol` fetcher (S10.6). M6 ("Photo → row in Sheets plus files in Drive; the SOL fallback for the owner's company") is reached when this phase and its deferred live check end.

**Goal:** an invoice routed to `personal_google` is fetched through its chain (now with `sunat_sol` and a Gmail inbox), then `DELIVERING` puts the photo, PDF and XML in Drive under `{root}/{YYYY}/{MM}/`, writes or updates its row in the bot's Sheet, moves to `DONE` and sends one ✅ or ⚠️ message with the row and folder links. A revoked Google token parks it in `PENDING_AUTH` (BR-D01) until `auth-google` runs again.

**Needs from the owner first:** neither prerequisite is done (owner, 2026-11-16), and the owner said to draft anyway. Nothing in this plan needs them for code or tests:

- S0.4 (does SOL "Consulta de comprobantes" give the XML of a received invoice?): `config/sunat_sol.yaml` ships with `status: todo` and placeholder selectors, so `sunat_sol` is not registered and the chain skips it (D82). The fetcher and its credential guard are tested with fakes; the selectors are filled and the file set `active` in the live check, only if S0.4 says yes.
- The Google OAuth app in production status: Google is tested with `httpx.MockTransport`; the consent flow is tested against a local loopback request. `auth-google` and `google-setup` run for real in the live check.

The live checks are in "Deferred: live check" at the end.

## Design choices

Step 1 writes these into the specs (spec 05, D128 to D135). They are proposals for the owner's review of this plan.

- **D128 · Google sign-in.** An OAuth client of type "Desktop app" in the owner's Google Cloud project, published to production (R-6.5; unverified is fine for personal use). `invoicebot auth-google --account <name>` runs the installed-app flow with a loopback redirect and PKCE (S256): it binds `127.0.0.1` on a free port, prints the consent URL (on the VPS the owner opens it through `ssh -L`), waits up to 5 min for one redirect, checks `state` with `hmac.compare_digest`, exchanges the code and stores the refresh token in `<data_dir>/tokens/<account>.google.age`, age-encrypted with the bot identity, as D85 does for Microsoft. The client id sits in config (`google.client_id`, public); the client secret is the age-store secret `google_client_secret`. The access token is cached in memory until 60 s before it expires (as R-4.3). A refresh answered `invalid_grant` raises `AuthRequired(account)` (BR-D01). A sign-in then resumes every `PENDING_AUTH` invoice, as `auth-microsoft` does. Device code is not used: Google's device flow does not allow `gmail.readonly`.
- **D129 · The bot creates its Drive folder and Sheet (Q6 default).** With `drive.file` the bot sees only files it created, so `invoicebot google-setup --account <name>` creates the root folder and the spreadsheet (one tab, header row) and prints their ids for `root_folder_id` and `spreadsheet_id`. That keeps flow A on `drive.file` alone (R-6.5). An existing sheet (the `spreadsheets` scope) waits for the owner's answer to Q6. Scopes are per account, derived from config: `drive.file` when a Google sink names the account, `gmail.readonly` when a `gmail` inbox does (a dedicated mailbox, D98).
- **D130 · Sheet layout and upsert (R-6.2, R-6.3, R-6.6).** Column A is always `Clave`, holding the invoice's `natural_key` (D93); the configured `columns` follow in order. To upsert, the sink reads `'<tab>'!A:A` (at most `google.max_rows`, default 20000 rows), updates the matching row in place, or appends one. Every write uses `valueInputOption=RAW`, and every text value is escaped with the one formula-escape helper that the Excel writer also uses (moved to `invoicebot/text.py`). `upsert_key` leaves the sink config: the key is always the natural key.
- **D131 · Links are flat template values.** The Drive sink returns `link_folder`, `link_photo`, `link_pdf` and `link_xml` (Drive `webViewLink`s; empty when that file is absent) and the Sheets sink `link_row`; they join the values the next sink and the completion message render. `sinks/expr.py` keeps its one attribute level. `config/example.yaml`'s `{refs.google_drive.photo.link}` becomes `{link_photo}`, and `{sunat.estado_cp_desc}` becomes `{estado_sunat}`.
- **D132 · The deliver step dispatches by flow.** `DELIVER_STEP` gets one dispatching handler that reads the invoice's flow and calls that flow's deliverer. `app.py` picks a flow's deliverer from its first sink's `kind` in one mapping (`google_drive` → flow A, `onedrive_folder` → flow B). A new kind of flow is a new entry, never an `if flow == …` (open/closed).
- **D133 · Flow A completion (R-8.1, R-8.2, S9.4).** `DONE` sends BR-D16 (✅, the key fields, the row and folder links) or, when the flow's desired PDF is missing, BR-D17 (⚠️ the same, saying the PDF is pending). Both map to the `completion` WhatsApp template outside the window. A step that fails for good is the existing dead-job path: BR-S03 to the sender with `/reprocesar {id}`, and BR-S03_admin. Local files of the invoice are deleted after the `DONE` commit (R-9.7).
- **D134 · `sunat_sol` (S10.6, R-9.10).** The site lives in `config/sunat_sol.yaml`: `status`, a `login` block (`url`, `ruc`, `user`, `password`, `submit` selectors) and a `query` block shaped like a portal file (`url`, `fields`, `submit`, `downloads`, `not_found`, `retry_schedule`), so the existing form filler runs it. The SUNAT origins are hard-coded in `invoicebot/sunat_origins.py`; `check_config` refuses a `login.url` outside them, and the credentials are typed only while `page.url` has one of those exact origins. SOL runs in its own helper entry (`python -m invoicebot.fetchers.browser.sol`) that reads the credentials from stdin, never from argv or the environment, and never logs them. It is registered only when `sunat.sol.enabled` and the file is `active`; startup refuses a `sol_ruc` secret that differs from `owner.ruc`. Flow B never lists it, so BR-F01 stays as built.
- **D135 · Gmail is a `Mailbox` (S9.5).** `fetchers/gmail.py` implements the existing `Mailbox` port. `search(term, top)` sends `q = has:attachment (filename:xml OR filename:pdf) "<term>"` (quotes removed from the term) with `maxResults=top` and reads each hit's `internalDate`; attachments come from the message's parts; a download checks the part's `size` against the cap before fetching and decodes base64url. Mailbox failures become provider-neutral errors in `fetchers/email_inbox.py`, so the fetcher no longer names Graph's.

Conventions: lint is `uv run ruff check src tests scripts migrations && uv run ruff format --check src tests scripts migrations && uv run mypy src && uv run lint-imports`. Integration tests need Postgres (`podman compose -f deploy/docker-compose.yml up -d postgres`). Every new user text is a constant in `rules/messages_es.py`, quoted in spec 06 by step 1. No real RUCs, phone numbers or photos in tests; use `tests/synthetic_rucs.txt`. Integration fixtures date invoices in the owner's timezone (Lima). Times come from the database clock (`now()`). Google is faked only with `httpx.MockTransport`; every test builds its own client.

Out of scope here: an existing Sheet and the `spreadsheets` scope (Q6), the Gmail mailer (S11.10, post-MVP), captchas on SOL (D99), `paid_api`, events (`invoice.delivered`, v1.1).

## Step 1: Record D128–D135 and the new texts `[inline: spec and roadmap text the owner reviews with this plan]`

Files: `specs/05-open-questions.md`, `specs/06-business-rules.md`, `specs/04-tasks.md`, `specs/03-design.md`, `docs/plan-mvp.md`, `src/invoicebot/rules/messages_es.py`.

- Spec 05: rows D128 to D135 with the text from "Design choices"; Q6 notes the default of D129.
- Spec 06 and `messages_es.py` together (the parity test needs both): BR-D16 `✅ Guardado {serie}-{numero} de {razon_social}, S/ {total}.` + `Fila: {link_row}` + `Carpeta: {link_folder}`; BR-D17 `⚠️ Guardado {serie}-{numero} de {razon_social}, S/ {total}, pero aún sin PDF; lo agrego cuando llegue.` + the same two link lines. BR-D01's admin text already names `scripts/auth_{provider}.py`.
- Spec 04: S9.1 cites D128, S9.2 D129 and D131, S9.3 D130, S9.4 D133, S9.5 D135, S10.6 D134.
- Spec 03: §6 `sunat_sol` (D134), §7 the Drive and Sheets sinks (D129–D132), the `auth/google_oauth.py` line and the `tokens/<account>.google.age` file.
- Roadmap: row 8 notes D128–D135.

Test: `uv run pytest -q tests/unit/test_rules_parity.py`.

## Step 2: BR-D16 and BR-D17 in the catalog and the template map `[worker:low]`

Files: `src/invoicebot/rules/catalog.py`, `src/invoicebot/channels/whatsapp/transport.py`, `tests/unit/test_whatsapp_transport.py`.

- Catalog entries for BR-D16 and BR-D17, state `DONE`, following BR-D10's entry.
- `TEMPLATE_SLOT_BY_RULE`: `"BR-D16": "completion"`, `"BR-D17": "completion"`.
- Tests: R-1.10 an out-of-window BR-D16 message is sent with the `completion` template; R-1.10 the same for BR-D17. (If the transport tests live in another file, add them there.)

Test: `uv run pytest -q tests/unit/test_rules_parity.py tests/unit/test_whatsapp_transport.py`.

## Step 3: One formula-escape helper `[worker]`

Files: `src/invoicebot/text.py`, `src/invoicebot/sinks/xlsx_patch.py`, `tests/unit/test_text.py`.

- Move `_R10_ESCAPE_PREFIXES` and the escape it drives out of `xlsx_patch.py` into `text.py` as `FORMULA_PREFIXES` and `escape_formula(value: str) -> str` (a leading `'` when the value starts with `=`, `+`, `-`, `@`, tab or carriage return). `xlsx_patch.py` imports it; its behaviour does not change.
- Tests: R-6.6 each of the six prefixes gets a leading `'`; R-6.6 `=HYPERLINK("x")` comes back as `'=HYPERLINK("x")`; R-6.6 a plain name and an empty string are unchanged. The existing xlsx tests still pass unchanged.

Test: `uv run pytest -q tests/unit/test_text.py tests/unit/test_sinks_xlsx_patch.py`.

## Step 4: Google config `[worker]`

Files: `src/invoicebot/config/models.py`, `config/example.yaml`, `tests/unit/test_config_models.py`.

- New `Google(client_id: str, timeout_s: PositiveInt = 30, max_rows: PositiveInt = 20000)`, `extra="forbid"`; `Config.google: Google | None = None`.
- `GoogleDriveSink` and `GoogleSheetsSink` gain `account: str` (pattern `^[a-z0-9_]+$`, as `FlowInbox.account`). `GoogleSheetsSink` drops `upsert_key` (D130). Its `columns` must be non-empty and must not contain the header `Clave`.
- A `Flow` validator: a `google_sheets` sink needs a `google_drive` sink before it (it renders the links); a `Config` validator: any Google sink or a `gmail` inbox needs `google` set.
- `config/example.yaml`: `google: { client_id: "000000000000-example.apps.googleusercontent.com", timeout_s: 30 }` with a comment naming the secret `google_client_secret`; both Google sinks get `account: owner`; drop `upsert_key`; columns use `{link_photo}`, `{link_pdf}`, `{link_xml}` and `"Estado SUNAT": "{estado_sunat}"`.
- Tests: R-6.2 the example config loads; R-6.3 `Clave` as a column is refused; R-6.1 a sheets sink without a drive sink is refused; R-6.5 a gmail inbox without `google` is refused; R-6.3 a leftover `upsert_key` is refused (`extra="forbid"`).

Test: `uv run pytest -q tests/unit/test_config_models.py && uv run python scripts/check_config.py config/example.yaml`.

## Step 5: Template values in one module `[worker]`

Files: `src/invoicebot/pipeline/values.py` (new), `src/invoicebot/pipeline/deliver.py`, `tests/unit/test_pipeline_values.py` (new).

- Move the reading-to-values part of `deliver.py` `_prepare_values` (`_fecha`, `_text` and the mapping of `fecha`, `ruc_emisor`, `razon_social`, `serie`, `numero`, `tipo`, totals) into `values.py` as `invoice_values(reading, xml_reading) -> dict[str, object]`. Flow B's own keys (`comercio`, `seq`, reimbursement) stay in `deliver.py`, which now calls `invoice_values` and adds them. Flow B's behaviour does not change.
- Add `tipo_desc` (the SUNAT tipo description; reuse an existing map if one exists, else `TIPO_DESC = {"01": "Factura", "03": "Boleta"}` here) and `estado_sunat` (from the stored `sunat_status_json` `estado_cp`: `"0"` No existe, `"1"` Aceptado, `"2"` Anulado, `"3"` Autorizado, `"4"` No autorizado; `""` when not checked). `invoice_values` takes the status dict as an argument.
- Tests: R-6.2 the values of a synthetic reading; R-6.2 the XML fills a field the photo lacks; R-4.2 `estado_sunat` for `"1"` and for no status. `tests/integration/test_pipeline_deliver.py` still passes unchanged.

Test: `uv run pytest -q tests/unit/test_pipeline_values.py tests/integration/test_pipeline_deliver.py`.

## Step 6: Google REST errors `[worker:low]`

Files: `src/invoicebot/sinks/google_api.py` (new), `tests/unit/test_sinks_google_api.py` (new).

- Module docstring: shared pieces of the Google REST clients. `GOOGLE_TIMEOUT` is not here (config). `class GoogleError(Exception)` (a refusal no retry fixes), `class GoogleTransient(GoogleError)` (429, 5xx, network). `raise_google_error(response: httpx.Response, account: str) -> None`: 2xx returns; 401 raises `AuthRequired(account)` (from `auth/msal_cache.py`; if importing it from there crosses a contract, move `AuthRequired` to `auth/__init__.py` and re-export it); 429 and 5xx raise `GoogleTransient`; any other status raises `GoogleError` with the status only, never the body. `auth_headers(token: str) -> dict[str, str]`.
- Tests: R-6.4 a 401 raises `AuthRequired` with the account; 429 and 503 raise `GoogleTransient`; R-9.16 a 403 raises `GoogleError` whose text has the status and not the body; a 200 returns.

Test: `uv run pytest -q tests/unit/test_sinks_google_api.py`.

## Step 7: Google token provider `[worker]`

Files: `src/invoicebot/auth/google_oauth.py` (new), `tests/unit/test_auth_google_oauth.py` (new).

- `TOKEN_URL = "https://oauth2.googleapis.com/token"`. `class GoogleTokens(http, client_id, client_secret, account, token_path, identity, timeout_s)` implements `TokenSource`. `token()`: returns the cached access token while more than 60 s remain; otherwise reads the refresh token from `token_path` (age-decrypted with `identity`; missing or unreadable → `AuthRequired(account)`), posts `grant_type=refresh_token` with a timeout, and caches the new token with `expires_in` (clamped to 1 h). `invalid_grant` → `AuthRequired(account)`; 5xx or network → `GoogleTransient`; another 4xx → `GoogleError`. One `asyncio.Lock` so concurrent callers refresh once.
- `save_refresh_token(path, identity, refresh_token)`: age-encrypt and write atomically (temp file + `os.replace`, mode 0600), as `msal_cache.py` writes its cache.
- Tests (MockTransport, a test identity): R-6.4 a refresh returns the token and a second call within the TTL makes no request; R-4.3 a token within 60 s of expiry is refreshed; BR-D01 `invalid_grant` raises `AuthRequired`; BR-D01 no token file raises `AuthRequired`; a 503 raises `GoogleTransient`; R-9.9 the saved file is not plain text and round-trips; R-9.16 the refresh token is never in a raised error's text.

Test: `uv run pytest -q tests/unit/test_auth_google_oauth.py`.

## Step 8: Consent URL, PKCE and scopes `[worker]`

Files: `src/invoicebot/auth/google_consent.py` (new), `src/invoicebot/auth/providers.py`, `tests/unit/test_auth_google_consent.py` (new).

- `google_consent.py` (pure): `pkce_pair() -> tuple[str, str]` (verifier from `secrets.token_urlsafe(64)`, S256 challenge); `consent_url(client_id, redirect_uri, scopes, state, challenge) -> str` (`https://accounts.google.com/o/oauth2/v2/auth`, `access_type=offline`, `prompt=consent`, `response_type=code`); `parse_redirect(path: str, expected_state: str) -> str` returns the code, raises `ConsentError` on a missing code, an `error=` parameter or a state mismatch (`hmac.compare_digest`).
- `providers.py`: `google_accounts(cfg) -> set[str]` and `google_scopes_for(cfg, account) -> list[str]` (D129: `https://www.googleapis.com/auth/drive.file` when a Google sink names the account, `https://www.googleapis.com/auth/gmail.readonly` when a `gmail` inbox does; an unknown account raises `ValueError`).
- Tests: R-6.5 the example config gives `owner` only `drive.file` and `invoices_a` only `gmail.readonly`; R-6.5 the URL carries `access_type=offline` and the S256 challenge; the challenge is the base64url SHA-256 of the verifier; a wrong state, an `error=access_denied` and a missing code each raise `ConsentError`.

Test: `uv run pytest -q tests/unit/test_auth_google_consent.py`.

## Step 9: `auth-google` command `[worker]`

Files: `src/invoicebot/cli.py`, `scripts/auth_google.py` (new), `tests/integration/test_cli_auth_google.py` (new).

- `invoicebot auth-google --account <name>` (a choice of `google_accounts(cfg)`): reads `google_client_secret` from a `Secrets` view scoped to `auth_google`; starts a one-request listener on `127.0.0.1:0` (`asyncio.start_server`; reads the request line only, capped at 8 KiB; answers a short plain page); prints the consent URL with `redirect_uri=http://127.0.0.1:<port>`; waits at most 300 s; `parse_redirect`; exchanges the code at `TOKEN_URL` with the verifier; `save_refresh_token` to `<data_dir>/tokens/<account>.google.age`; then resumes `PENDING_AUTH` invoices with the existing `_resume_pending_auth`. A response without `refresh_token` is an error that tells the owner to remove the app's access and sign in again. The URL printing goes through a `show` callable, as `auth-microsoft`'s device code does, so the test can read it. `scripts/auth_google.py` forwards to `cli.main`, like `scripts/auth_microsoft.py`.
- Test (real Postgres, MockTransport for the token endpoint): R-6.4 the test reads the printed URL, sends `GET /?code=x&state=<state>` to the listener, and the token file exists afterwards; BR-D01 a `PENDING_AUTH` invoice is `DELIVERING` with a deliver job after the sign-in; a wrong state exits non-zero and writes no file.

Test: `uv run pytest -q tests/integration/test_cli_auth_google.py`.

## Step 10: Drive client `[worker]`

Files: `src/invoicebot/sinks/google_drive.py` (new), `tests/unit/test_sinks_google_drive.py` (new).

- `DRIVE_URL = "https://www.googleapis.com/drive/v3"`, `UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"`. `@dataclass(frozen=True) DriveFile(id, link)`. `class GoogleDrive(http, tokens: TokenSource, timeout_s, account)`:
  - `ensure_folder(parent_id, name) -> str`: `files.list` with `q = '<parent>' in parents and name = '<name>' and mimeType = 'application/vnd.google-apps.folder' and trashed = false` (backslash and `'` escaped in the name), the first hit's id, else `files.create` of a folder.
  - `put_file(folder_id, name, data, mime) -> DriveFile`: the same lookup without the mime filter; an existing file is reused (no second upload); else a `multipart/related` upload with `fields=id,webViewLink`.
  - `folder_link(folder_id) -> str`: `https://drive.google.com/drive/folders/<id>`.
  - Every response goes through `raise_google_error`; ids from Google are checked against `^[A-Za-z0-9_-]{10,200}$` before use.
- Tests: R-6.1 a missing folder is created and an existing one reused; R-6.1 an upload sends the metadata part with the name and parent and returns the link; R-6.3 a second `put_file` of the same name uploads nothing; a name with `'` is escaped in `q`; a malformed id from Google raises `GoogleError`; a 401 raises `AuthRequired`.

Test: `uv run pytest -q tests/unit/test_sinks_google_drive.py`.

## Step 11: Drive sink `[worker]`

Files: `src/invoicebot/sinks/drive_files.py` (new), `tests/unit/test_sinks_drive_files.py` (new).

- `async file_to_drive(sink: GoogleDriveSink, drive: GoogleDrive, files: DriveInput, values) -> dict[str, str]` where `DriveInput(photos: list[Path], pdf: bytes | None, xml: bytes | None)`. Renders `sink.path` with `expr.render` and walks its `/`-separated parts from `root_folder_id` with `ensure_folder` (an empty part or `..` raises `ExprError`). Renders `sink.filename` once; uploads only the kinds in `sink.upload`: photos as `<name>.jpg`, `<name>_2.jpg`, …, the PDF as `<name>.pdf`, the XML as `<name>.xml`. Returns `link_folder`, `link_photo` (the first photo), `link_pdf`, `link_xml`, each `""` when that file is absent (D131).
- Tests: R-6.1 with the example sink and a fake drive, the folder path is `2026/11` under the root and the names follow `{fecha}_{ruc}_{serie}-{numero}`; R-6.1 two photos get `_2`; R-5.9 no PDF leaves `link_pdf` empty; a path rendering to `..` is refused; a kind not in `upload` is not uploaded.

Test: `uv run pytest -q tests/unit/test_sinks_drive_files.py`.

## Step 12: Sheets client `[worker]`

Files: `src/invoicebot/sinks/google_sheets.py` (new), `tests/unit/test_sinks_google_sheets.py` (new).

- `SHEETS_URL = "https://sheets.googleapis.com/v4/spreadsheets"`. `class GoogleSheets(http, tokens, timeout_s, account, max_rows)`:
  - `keys(spreadsheet_id, tab) -> dict[str, int]`: `values.get` of `'<tab>'!A1:A<max_rows>` → key to 1-based row (the header row skipped). Tab names are quoted with `'` doubled.
  - `append(spreadsheet_id, tab, row: list[str]) -> int`: `values:append` with `valueInputOption=RAW` and `insertDataOption=INSERT_ROWS`; the row number parsed from `updates.updatedRange` (a range that does not parse raises `GoogleError`).
  - `update(spreadsheet_id, tab, row_number, row)`: `values.update` of `'<tab>'!A<n>` with `valueInputOption=RAW`.
  - `row_link(spreadsheet_id, tab, row_number) -> str`: reads the tab's `sheetId` once (`fields=sheets.properties`) and returns `https://docs.google.com/spreadsheets/d/<id>/edit#gid=<gid>&range=A<n>`.
  - Every text cell goes through `escape_formula` before it is sent (R-6.6).
- Tests: R-6.6 every write request has `valueInputOption=RAW`; R-6.6 `=HYPERLINK(…)` is sent as `'=HYPERLINK(…)`; R-6.3 `keys` maps the column; the append's row comes from `updatedRange`; a tab named `O'Brien` is quoted `'O''Brien'`; a 429 raises `GoogleTransient`.

Test: `uv run pytest -q tests/unit/test_sinks_google_sheets.py`.

## Step 13: Sheets sink `[worker]`

Files: `src/invoicebot/sinks/sheet_rows.py` (new), `tests/unit/test_sinks_sheet_rows.py` (new).

- `async upsert_row(sink: GoogleSheetsSink, sheets: GoogleSheets, key: str, values) -> dict[str, str]`: the row is `[key] + [expr.render(expr, values) for each column]`; an existing key's row is updated, else a row is appended; returns `{"link_row": …}`.
- Tests (a fake sheets object): R-6.2 a new key appends one row with `Clave` first and the columns in order; R-6.3 a known key updates its row and appends nothing; R-6.2 the Drive links render into their columns.

Test: `uv run pytest -q tests/unit/test_sinks_sheet_rows.py`.

## Step 14: The `PENDING_AUTH` move in one module `[worker:low]`

Files: `src/invoicebot/pipeline/pending_auth.py` (new), `src/invoicebot/pipeline/deliver.py`, `tests/integration/test_pipeline_deliver.py`.

- Move `deliver.py` `_pending_auth` (state `PENDING_AUTH`, BR-D01 to the sender, BR-D01_admin to the admins) into `pending_auth.py` as `park_for_auth(conn, head, invoice_id, provider: str, account: str, admins)`; the provider slug comes from the caller (`microsoft` for flow B). `deliver.py` calls it; behaviour unchanged.
- Test: the existing BR-D01 case in `test_pipeline_deliver.py` passes unchanged.

Test: `uv run pytest -q tests/integration/test_pipeline_deliver.py`.

## Step 15: Flow A deliver handler `[worker:high: reasoning: the handler joins two network sinks, the state change, the completion message and the file cleanup; what runs outside the transaction, what a retry repeats and when cleanup is safe need care]`

Files: `src/invoicebot/pipeline/deliver_google.py` (new), `tests/integration/test_pipeline_deliver_google.py` (new).

- `make_google_deliver_handler(context: GoogleDeliverContext) -> Handler`; `GoogleDeliverContext(pool, cfg, drive: GoogleDrive, sheets: GoogleSheets, admins)` (frozen dataclass). Acts only in `DELIVERING` (R-9.4).
- Load the invoice, its flow, reading, `sunat_status_json`, the photo paths, PDF and XML (`get_file_path`, as `deliver.py` `_load` does); build values with `invoice_values`. No connection is held during Google calls.
- Run the flow's sinks in order: `file_to_drive`, then `upsert_row` with `natural_key`; each one's returned links join the values. A retry repeats both, which is safe: Drive reuses names and Sheets upserts (R-6.3).
- `AuthRequired(account)` → `park_for_auth(..., provider="google", account=account)` and return. `GoogleTransient` and `GoogleError` propagate (queue retries; a dead job is BR-S03, D133).
- Then one transaction: `DELIVERING` → `DONE` (expected-state update; `StateMoved` ends the job quietly) and the completion queued on the origin channel: BR-D16, or BR-D17 when `pdf` is absent. After the commit has returned, delete the invoice's local photo, PDF and XML files (R-9.7); a cancellation before the commit returns deletes nothing.
- Tests (real Postgres, MockTransport for Drive and Sheets, the fake channel): R-6.1/R-6.2 a `DELIVERING` invoice ends `DONE` with files uploaded and one row appended, BR-D16 queued with both links; R-6.3 running the job twice leaves one row; R-8.1 no PDF → BR-D17; BR-D01 a 401 → `PENDING_AUTH`, BR-D01 to the sender and the admin text with `auth_google`; a 503 raises and the invoice stays `DELIVERING`; R-9.7 the local files are gone after `DONE`; R-9.4 an invoice not in `DELIVERING` is untouched.

Test: `uv run pytest -q tests/integration/test_pipeline_deliver_google.py`.

## Step 16: Deliver dispatch by flow `[worker]`

Files: `src/invoicebot/pipeline/deliver_dispatch.py` (new), `src/invoicebot/app.py`, `tests/integration/test_pipeline_deliver_dispatch.py` (new).

- `make_deliver_dispatch(pool, by_flow: Mapping[str, Handler]) -> Handler`: reads the invoice's `flow_id` and calls that flow's handler; a flow with no deliverer logs `step skipped` with `reason="no_deliverer"` and returns.
- `app.py`: `DELIVERERS: Mapping[str, Callable[..., Handler]]` keyed by sink kind (`"google_drive"` → the flow A builder, `"onedrive_folder"` → the existing flow B handler); each flow maps to the builder of its first sink's kind; `DELIVER_STEP` gets the dispatcher (D132). The `RESEND_*` steps stay flow B only.
- Tests: R-6.1 a flow A invoice reaches the flow A handler and a flow B invoice the flow B one (recording fakes); a flow without a deliverer is skipped.

Test: `uv run pytest -q tests/integration/test_pipeline_deliver_dispatch.py`.

## Step 17: Wire Google into the app `[worker]`

Files: `src/invoicebot/app.py`, `src/invoicebot/auth/providers.py`, `tests/unit/test_app_wiring.py`.

- When `cfg.google` is set: one `GoogleTokens` per `google_accounts(cfg)` (client secret from a `Secrets` view scoped to `google_oauth`, declaring only `google_client_secret`; token path `<data_dir>/tokens/<account>.google.age`); the flow A deliverer gets a `GoogleDrive` and `GoogleSheets` on the plain (non-guarded) client with the sink's account. A missing `google_client_secret` with `cfg.google` set fails at startup (`Secrets.enabled`'s fail-closed check).
- Tests (or extend the existing wiring test file if it is named differently): R-9.10 the Google secret view declares only `google_client_secret`; R-6.4 a config without `google` builds no Google clients; a config with `google` and no secret fails at startup.

Test: `uv run pytest -q tests/unit/test_app_wiring.py`.

## Step 18: `google-setup` command `[worker]`

Files: `src/invoicebot/cli.py`, `src/invoicebot/sinks/google_sheets.py`, `tests/unit/test_cli_google_setup.py` (new).

- `GoogleSheets.create(title, tab, header: list[str]) -> str` (`spreadsheets.create` with the one tab, then a RAW header write).
- `invoicebot google-setup --account <name> [--folder-name Comprobantes] [--sheet-title Comprobantes]`: creates the root folder (`GoogleDrive.ensure_folder("root", …)`) and the sheet with the header `["Clave", *columns]` from the first flow whose sheets sink names the account; prints `root_folder_id: …` and `spreadsheet_id: …` for the private config (D129). It writes no config file.
- Tests (MockTransport): R-6.2 the header starts with `Clave` and follows the configured columns; the printed ids are the created ones; an account no Google sink names exits non-zero.

Test: `uv run pytest -q tests/unit/test_cli_google_setup.py`.

## Step 19: Provider-neutral mailbox errors `[worker]`

Files: `src/invoicebot/fetchers/base.py`, `src/invoicebot/fetchers/email_inbox.py`, `src/invoicebot/fetchers/graph_mail.py`, `tests/unit/test_fetchers_graph_mail.py`, `tests/unit/test_fetchers_email_inbox.py`.

- `fetchers/base.py` defines `class MailboxError(Exception)` (the mailbox answered with a refusal or broke); it lives there, not in `email_inbox.py`, because `email_inbox` imports `graph_mail` and the reverse import would be circular. The fetcher maps `MailboxError` and `httpx.HTTPError` to `TECHNICAL` and `AuthRequired` to `AUTH_REQUIRED`, and no longer imports Graph's errors. `GraphMailbox`'s public methods (`search`, `attachments`, `download`) raise `MailboxError` from its `GraphError`/`GraphTransient` (`raise … from err`); `AuthRequired` passes through.
- Tests: R-5.2 a Graph 500 surfaces as `MailboxError` from `GraphMailbox.search`. In `test_fetchers_email_inbox.py` the two tests whose fake raises `GraphTransient(503)` / `GraphError(403)` change to raise `MailboxError("…")` and `httpx.ConnectError("…")` (renamed to a mailbox error / an HTTP error, both still `TECHNICAL`), and the `sinks.onedrive` import goes; every other test there is unchanged.

Test: `uv run pytest -q tests/unit/test_fetchers_graph_mail.py tests/unit/test_fetchers_email_inbox.py`.

## Step 20: Gmail mailbox `[worker]`

Files: `src/invoicebot/fetchers/gmail.py` (new), `tests/unit/test_fetchers_gmail.py` (new).

- `GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me"`. `class GmailMailbox(http, tokens, account, timeout_s)` implements `Mailbox` (D135):
  - `search(term, top)`: `messages.list` with the D135 query and `maxResults=top`, then `messages.get?format=metadata` per hit for `internalDate` → `MailMessage` (newest first).
  - `attachments(message_id)`: `messages.get?format=full`, walking `payload.parts` recursively (depth capped at 10) for parts with a `filename` and `body.attachmentId` → `MailAttachment(id, name, size)`.
  - `download(message_id, attachment_id, max_bytes)`: streams the response and raises `ValueError` once the raw body passes `max_bytes * 4 // 3 + _JSON_OVERHEAD_BYTES` (named constant, 4096), then decodes base64url and raises `ValueError` for a decoded body above `max_bytes`. It does not look up the listed size itself: the fetcher already checks `MailAttachment.size` before calling `download` (D135's check before fetching), and Gmail may return a different `attachmentId` on each `messages.get`, so matching by id is unreliable.
  - Ids are checked against `^[A-Za-z0-9_-]{1,2048}$` (Gmail attachment ids are often several hundred characters); failures go through `raise_google_error` and are re-raised as `MailboxError` (except `AuthRequired`).
- Tests: R-5.2 the query holds the quoted term and the attachment filter; a term with `"` has it removed; nested parts are found; a 600-character attachment id is accepted; a streamed body over the raw cap raises `ValueError`; a decoded body over `max_bytes` raises `ValueError`; base64url decodes; a 401 raises `AuthRequired`; a 500 raises `MailboxError`; a bad id is refused before any request.

Test: `uv run pytest -q tests/unit/test_fetchers_gmail.py`.

## Step 21: Register the Gmail inbox `[worker]`

Files: `src/invoicebot/app.py`, `src/invoicebot/pipeline/fetch.py`, `tests/integration/test_pipeline_fetch.py`.

- `app.py`: `_email_inbox_fetcher` picks the mailbox from a mapping `{"graph": …, "gmail": …}` instead of returning `None` for `gmail`; the Gmail mailbox uses the inbox account's `GoogleTokens`.
- `pipeline/fetch.py` `_alert_sign_in`: alert for any provider, with the slug from `{"graph": "microsoft", "gmail": "google"}` (one mapping, beside the existing `AUTH_PROVIDER_SLUG`), instead of returning for non-Graph inboxes.
- Tests: BR-D01 a Gmail `AUTH_REQUIRED` attempt sends the admin text naming `auth_google` and the account; the Graph case is unchanged.

Test: `uv run pytest -q tests/integration/test_pipeline_fetch.py`.

## Step 22: SUNAT origins and the SOL site file `[worker]`

Files: `src/invoicebot/sunat_origins.py` (new), `src/invoicebot/config/models.py`, `config/sunat_sol.yaml` (new).

- `sunat_origins.py`: `SUNAT_ORIGINS = frozenset({"https://api-seguridad.sunat.gob.pe", "https://e-menu.sunat.gob.pe"})` and `is_sunat_origin(url: str) -> bool` (parse with `urllib.parse`; `https` only; no userinfo; no port other than none or 443; `scheme://host` exactly in the set).
- `config/models.py`: `SolLogin(url, ruc, user, password, submit)` (selectors are strings) and `SunatSolSite(status: Literal["active","todo","broken"], login: SolLogin, query: Portal)`; a validator refuses a `login.url` for which `is_sunat_origin` is false.
- `config/sunat_sol.yaml`: `status: todo`, the login URL `https://api-seguridad.sunat.gob.pe/...` placeholder path, placeholder selectors, a `query` block with `fields` for `{ruc_emisor}`, `{serie}`, `{numero}`, `downloads: {xml: …}`, `not_found`, `retry_schedule: ["1h", "6h", "24h"]`, and a comment that S0.4 fills it.
- Test file: `tests/unit/test_sunat_origins.py` (new; counts as this step's third file): R-9.10 the two origins pass; `http://` fails; `https://api-seguridad.sunat.gob.pe.evil.test` fails; `https://evil.test/?u=https://e-menu.sunat.gob.pe` fails; `https://user@e-menu.sunat.gob.pe` fails; `:8443` fails; the shipped file loads as `todo`.

Test: `uv run pytest -q tests/unit/test_sunat_origins.py`.

## Step 23: Load and check the SOL site `[worker]`

Files: `src/invoicebot/config/loader.py`, `src/invoicebot/config/checks.py`, `tests/unit/test_config_checks.py`.

- `load_directory` (or the loader beside it) reads `config/sunat_sol.yaml` when it exists into `SunatSolSite | None`. `run_checks`: with `sunat.sol.enabled` and the file `active`, the query's selectors pass the same checks a portal file gets; a flow listing `sunat_sol` with `sunat.sol.enabled: false` is a warning, not an error (the chain skips it).
- Tests: R-5.2 the shipped `todo` file passes; a `login.url` outside SUNAT is refused at load; R-5.8 an active file with a bad query selector is reported.

Test: `uv run pytest -q tests/unit/test_config_checks.py && uv run python scripts/check_config.py config/example.yaml`.

## Step 24: Type the SOL credentials only on SUNAT `[worker]`

Files: `src/invoicebot/fetchers/browser/sol_login.py` (new), `tests/unit/test_browser_sol_login.py` (new).

- `class LoginPage(Protocol)`: `url: str`, `async fill(selector, value)`, `async click(selector)`, `async wait_for_load_state()`. `@dataclass(frozen=True) SolCredentials(ruc, user, password)` with a `__repr__` that hides all three. `async log_in(page, login: SolLogin, creds) -> bool`: returns `False` without typing anything when `is_sunat_origin(page.url)` is false; re-checks the origin before each of the three fills; clicks submit; waits for load.
- Tests (a fake page that records calls): R-9.10 on a SUNAT URL the three fields are filled and submit clicked; R-9.10 a look-alike page (`https://api-seguridad.sunat.gob.pe.evil.test/login`) gets no fill; R-9.10 a page that redirects away after the first fill gets no second fill; R-9.16 `repr(creds)` holds none of the values.

Test: `uv run pytest -q tests/unit/test_browser_sol_login.py`.

## Step 25: The SOL helper entry `[worker]`

Files: `src/invoicebot/fetchers/browser/sol.py` (new), `src/invoicebot/fetchers/browser/__main__.py`, `tests/unit/test_browser_sol.py` (new).

- Beside the portal wire format in `fetchers/browser/protocol.py`, `sol.py` defines `SolRequest(site: SunatSolSite, invoice, out_dir, max_bytes, page_timeout_ms, credentials: SolCredentials)` parsed from stdin (pydantic, `extra="forbid"`; the credentials as `SecretStr`). Reuse `__main__.py`'s launch, proxy and resource limits: make the launch part a function both entries call (no copy). `async run_sol(page, request) -> FetchResult`: `page.goto(login.url)`, `log_in` (a `False` → reason `technical`), then `form.fill_and_download(page, site.query, invoice, out_dir, max_bytes)`. `main()` emits `HelperOutput`. Nothing logs the request.
- Tests (fake page and a fake form filler): R-5.2 a run logs in then fills the query; R-9.10 a login refused on origin returns `technical` without filling the query; R-9.16 a request whose parse fails writes no credential to stderr; the portal helper's tests still pass.

Test: `uv run pytest -q tests/unit/test_browser_sol.py tests/unit/test_browser_main.py`.

## Step 26: `sunat_sol` fetcher `[worker]`

Files: `src/invoicebot/fetchers/sunat_sol.py` (new), `tests/unit/test_fetchers_sunat_sol.py` (new).

- `SUNAT_SOL_FETCHER = "sunat_sol"`, `SECRETS = ("sol_ruc", "sol_user", "sol_password")`. `class SunatSolFetcher(site, secrets: Secrets, max_bytes, timeout_s, helper_command = SOL_HELPER_COMMAND)` implements `Fetcher`: builds the `SolRequest`, runs `run_helper` with it on stdin and the same minimal env as the portal fetcher (no secret in env or argv), and maps the output as `PortalFetcher` does, with `portal_key="sunat_sol"` (R-5.7 stats).
- Tests (a fake helper script, as the portal fetcher's tests use): R-9.10 the credentials reach the helper on stdin and not in its argv or environment; R-5.2 an `ok` output returns the XML file; R-5.1 `not_found_yet` passes through; a helper timeout is `technical`; R-9.10 a `Secrets` view without `sol_password` raises at construction.

Test: `uv run pytest -q tests/unit/test_fetchers_sunat_sol.py`.

## Step 27: Register `sunat_sol` `[worker]`

Files: `src/invoicebot/app.py`, `tests/unit/test_app_wiring.py`.

- A `sunat_sol` entry in `_fetcher_builders`: built only when `cfg.sunat.sol.enabled` and the site file is `active`; the secrets come from `store.scoped(SUNAT_SOL_FETCHER, set(SECRETS))`. At startup, a `sol_ruc` that differs from `cfg.owner.ruc` refuses to start (`hmac.compare_digest`; the message names the keys, never the values). `_fetch_schedules` takes `sunat_sol`'s retry schedule from the site file.
- Tests: R-5.2 a `todo` site registers nothing; R-5.2 an `active` site with matching secrets registers the fetcher; R-9.10 a mismatched `sol_ruc` fails startup and the error text holds neither RUC.

Test: `uv run pytest -q tests/unit/test_app_wiring.py`.

## Step 28: Flow A end to end `[worker]`

Files: `tests/integration/test_flow_a.py` (new).

- One test from photo to `DONE` (reuse `read_conversation`/`draw_text` fixtures and the fake channel): a synthetic receipt whose buyer is `owner.ruc` routes to `personal_google`; a fake fetcher returns a matching XML; the dispatcher runs the flow A deliverer against MockTransport Drive and Sheets; the invoice ends `DONE` with BR-D16 holding the row link. A second test: BR-D01 a revoked token parks the invoice and `resume_pending_auth` plus the deliver job finish it. Dates in Lima time.

Test: `uv run pytest -q tests/integration/test_flow_a.py`.

## Step 29: Close the drift notes `[inline: roadmap text]`

Files: `docs/plan-mvp.md`, `specs/03-design.md`.

- Roadmap: note in row 8 what landed and that `sunat_sol` is `todo` until S0.4; add the deferred live check of this plan to the roadmap's carried-over list.
- Spec 03 §7: remove any `refs.` example left over (D131).

Test: `uv run python scripts/check_fixtures.py`.

## Deferred: live check

Run when the owner has the Google OAuth app in production and S0.4 is answered. Record the results in spec 04 (M6).

1. **Google:** create the Desktop OAuth client, publish it to production, put `google_client_secret` in the age store and `google.client_id` in the private config. Run `auth-google --account owner` and `--account invoices_a` (through `ssh -L`), then `google-setup --account owner`, and put the ids in the private config.
2. **M6, Drive and Sheets:** send a personal invoice photo; confirm the files in `{root}/{YYYY}/{MM}/`, one row, and BR-D16 with working links. Send it again after `/reprocesar`; the row is updated, not duplicated.
3. **Gmail:** forward a store's invoice mail to the dedicated Gmail; confirm `email_inbox` finds its XML.
4. **S0.4:** if SOL gives the XML of a received invoice, fill `config/sunat_sol.yaml`'s selectors (watch for frames in SOL's menu), set it `active`, and run one invoice through `sunat_sol`. If it doesn't, record that, keep the file `todo`, and drop `sunat_sol` from the flow's default chain.
5. **BR-D01:** remove the app's access in the Google account settings, send a photo, confirm `PENDING_AUTH` and the admin alert, sign in again, and confirm it finishes.
