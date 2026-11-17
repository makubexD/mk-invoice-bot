# Plan: MVP phase 8 · flow-a review fixes

Roadmap: `docs/plan-mvp.md` row 8. The finish review of `docs/plan-mvp-flow-a.md` (py-code-reviewer, py-security-auditor and the five-axis review, 2026-11-17) found two high, eight medium and several low issues. This plan is that review's one round of fixes, on the same branch (`docs/plan-mvp-flow-a`). Its finish also closes `docs/plan-mvp-flow-a.md`: the finish commit carries a `Plan-Done:` line for both plans. The red `tests/unit/test_health.py` (H1) is already fixed in its own commit.

**Goal:** a new Google sign-in resumes only the invoices parked for that account and they finish on the first try; a park never overwrites a cancel; every Google flow calls Google with its own account; a Google rate limit retries; SOL credentials can reach SUNAT hosts only; and the duplicated knowledge the review named has one home.

## Design choices

- **A parked invoice names its account.** `park_for_auth` stores `<provider>:<account>` in `invoices.error`; `resume_pending_auth` resumes only the rows with that exact value. No migration: the column exists and nothing is in production.
- **A new token file is a new sign-in.** `GoogleTokens` remembers the token file's `st_mtime_ns` at each refresh and refreshes again when it changes, so the server drops an access token that a revoke killed as soon as `auth-google` writes the new file. No port change.
- **Flow A config rules move to load time.** A flow with a `google_drive` sink must have a `google_sheets` sink (the reverse already holds), and `root_folder_id`/`spreadsheet_id` must match the Google id pattern. The runtime `RuntimeError`/`ValueError` copies go.
- **Carried to `ops`:** `auth-google` does not check which Google user signed in (needs `openid email` and an expected address in config).

Conventions: lint is `uv run ruff check src tests scripts migrations && uv run ruff format --check src tests scripts migrations && uv run mypy src && uv run lint-imports`. Integration tests need Postgres. Synthetic values only. Every test docstring starts with its requirement or rule id.

## Step 1: Carry the ops item to the roadmap `[inline: roadmap text]`

Files: `docs/plan-mvp.md`.

- Under "Carried over from `flow-a`", add: **`ops`:** `auth-google` saves the token of whichever Google user signed in; request `openid email`, compare the address with one set per account in config and refuse a mismatch (py-security-auditor, 2026-11-17).

Test: `uv run python scripts/check_fixtures.py`.

## Step 2: A new token file makes the server refresh `[worker]`

Files: `src/invoicebot/auth/google_oauth.py`, `tests/unit/test_auth_google_oauth.py`.

- `GoogleTokens` keeps `self._token_mtime_ns: int | None = None`. `_refresh` sets it from `self._token_path.stat().st_mtime_ns` right after `_load_refresh_token` succeeds.
- `token()` returns the cached token only when it is unexpired **and** `self._token_path.stat().st_mtime_ns == self._token_mtime_ns`. A `stat` that raises `OSError` counts as changed, so `_refresh` runs and raises `AuthRequired` as today.
- Tests (BR-D01): (a) a cached token is reused while the file is unchanged (one token POST for two `token()` calls); (b) after the file is rewritten with a different mtime (`os.utime`), `token()` posts again and returns the new access token; (c) after the file is deleted, `token()` raises `AuthRequired`.

Test: `uv run pytest -q tests/unit/test_auth_google_oauth.py`.

## Step 3: A park cannot overwrite a cancel, and it names its account `[worker]`

Files: `src/invoicebot/pipeline/pending_auth.py`, `src/invoicebot/pipeline/deliver.py`, `src/invoicebot/pipeline/deliver_google.py`.

- `park_for_auth` calls `set_state(conn, invoice_id, PENDING_AUTH, rule_id=RULE_AUTH, error=f"{provider}:{account}", expected=DELIVERING)`, with `PENDING_AUTH` and `DELIVERING` imported from `invoicebot.states`. Delete the local `PENDING_AUTH_STATE` constant and update its importers (`grep -rn PENDING_AUTH_STATE src tests`). Docstring: a `StateMoved` rolls the caller's transaction back and the worker handles it.
- `deliver.py` (around line 821) and `deliver_google.py` `_park_auth`: drop the `_still_delivering` check before `park_for_auth`. Delete `deliver_google._still_delivering` (now unused); keep `deliver._still_delivering`, which other paths still use.
- Test: in `tests/integration/test_pipeline_deliver_google.py`, add (BR-D01, R-9.4): the invoice is moved to `CANCELLED` after `_load` and before the park (make the fake Drive's first call flip the state in its own connection, then raise `AuthRequired`); the invoice stays `CANCELLED` and no BR-D01 message is queued. Extend the existing park test to assert `error == "google:owner"`.

Test: `uv run pytest -q tests/integration/test_pipeline_deliver_google.py tests/integration/test_pipeline_deliver.py`.

## Step 4: A sign-in resumes only its own account's invoices `[worker]`

Files: `src/invoicebot/store/invoices.py`, `src/invoicebot/cli.py`, `tests/integration/test_invoices_store.py`.

- `resume_pending_auth(conn, provider: str, account: str)` adds `AND error = %s` with `f"{provider}:{account}"` as the bound parameter.
- `cli.py` `_resume_pending_auth` takes the provider and account and passes them on: `_auth_microsoft` passes `"microsoft"` and its account, `_auth_google` passes `"google"` and its account. Use the provider slugs already defined in `pipeline/pending_auth.py` (`PROVIDER_NAMES` keys) through named constants there (`MICROSOFT`, `GOOGLE`), not new literals.
- Tests (BR-D01): three `PENDING_AUTH` rows with `error` `google:owner`, `google:invoices_a` and `microsoft:personal`; `resume_pending_auth(conn, "google", "owner")` returns only the first id and leaves the others `PENDING_AUTH`.

Test: `uv run pytest -q tests/integration/test_invoices_store.py tests/integration/test_cli_auth.py tests/integration/test_cli_auth_google.py`.

## Step 5: Flow A sink rules are checked when the config loads `[worker]`

Files: `src/invoicebot/config/models.py`, `src/invoicebot/sinks/google_drive.py`, `tests/unit/test_config_models.py`.

- Rename `Flow._sheets_sink_follows_a_drive_sink` to `_google_sinks_come_in_pairs` and also raise `ValueError("a flow with a 'google_drive' sink needs a 'google_sheets' sink")` when a Drive sink has no Sheets sink.
- Move `ID_PATTERN` from `sinks/google_drive.py` to `config/models.py` as `GOOGLE_ID_PATTERN` (same regex); `google_drive.py` and `google_sheets.py` import it from there. `GoogleDriveSink.root_folder_id` and `GoogleSheetsSink.spreadsheet_id` get a field validator that refuses a value not matching it (`root_folder_id` may also be the literal `"root"`).
- Tests (R-6.1, R-6.2): a Drive sink without Sheets is refused; a `root_folder_id` of `"a/b"` and a `spreadsheet_id` of `"x y"` are refused; `"root"` is accepted; the example config still loads.

Test: `uv run pytest -q tests/unit/test_config_models.py tests/unit/test_sinks_google_drive.py tests/unit/test_sinks_google_sheets.py`.

## Step 6: Each Google sink calls Google with its own account `[worker:high: reasoning: rewires app startup and the handler context together; the per-account map must stay consistent with google_accounts and the deliver dispatch]`

Files: `src/invoicebot/app.py`, `src/invoicebot/pipeline/deliver_google.py`, `tests/integration/test_pipeline_deliver_google.py`.

- `GoogleDeliverContext` replaces `drive`/`sheets` with `drives: Mapping[str, GoogleDrive]` and `sheets: Mapping[str, GoogleSheets]`, keyed by account. `_run_sinks` uses `context.drives[sinks.drive.account]` and `context.sheets[sinks.sheets.account]`.
- `deliver_google._sinks` returns `_Sinks` (never None: step 5 guarantees the pair and the dispatch only routes flows whose first sink is `google_drive`); delete the `no_drive_sink` branch and the `RuntimeError`.
- `app.py`: `_google_deliver_handler` builds one `GoogleDrive` and one `GoogleSheets` per account in `tokens` and returns None when no flow has a `google_drive` sink. Delete `_google_sinks` and its `ValueError`.
- Test (R-6.1, D128): two flows, each with Drive and Sheets sinks on a different account, each backed by its own fake; an invoice of the second flow is filed only through the second account's fakes.

Test: `uv run pytest -q tests/integration/test_pipeline_deliver_google.py tests/integration/test_flow_a.py tests/unit/test_app_wiring.py`.

## Step 7: A Google rate limit is retried `[worker]`

Files: `src/invoicebot/sinks/google_api.py`, `tests/unit/test_sinks_google_api.py`.

- In `raise_google_error`, a 403 whose JSON body has `error.errors[*].reason` in `{"rateLimitExceeded", "userRateLimitExceeded"}` raises `GoogleTransient(403)`. Read the body only when the status is 403 and the response is already read (`response.is_stream_consumed` or not a stream; a streamed 403 stays `GoogleError`); a body that is not that shape stays `GoogleError(403)`. Constants for the reason set.
- Move the duplicated `_json_object(response)` from `google_drive.py` and `google_sheets.py` into `google_api.py` as `json_object` and import it in both. Correct the module docstring: a network failure propagates as an `httpx` error, which the queue retries.
- Tests (R-6.4): 403 `rateLimitExceeded` → `GoogleTransient`; 403 `userRateLimitExceeded` → `GoogleTransient`; 403 `insufficientPermissions` → `GoogleError` and not `GoogleTransient`; 403 with a non-JSON body → `GoogleError`.

Test: `uv run pytest -q tests/unit/test_sinks_google_api.py tests/unit/test_sinks_google_drive.py tests/unit/test_sinks_google_sheets.py`.

## Step 8: Sign-in checks granted scopes and ignores stray requests `[worker]`

Files: `src/invoicebot/cli.py`, `tests/integration/test_cli_auth_google.py`.

- `_refresh_token` becomes `_granted_refresh_token(response, scopes)`: besides the refresh token, it reads the response's `scope` (space-separated) and raises `_SignInError("Google did not grant: <missing scopes>; sign in again and allow every permission")` when any of `scopes` (from `google_scopes_for`) is missing.
- The loopback handler settles `redirect_path` only when the request's query has a `state` or an `error` parameter; any other request gets the same response and is ignored.
- Use one constant for the client secret's view name: import `GOOGLE_OAUTH_ADAPTER` (move it from `app.py` to `auth/google_oauth.py`, and have `app.py` import it) and use it at both `scoped(...)` calls in `cli.py` (today `"auth_google"` and `"google_oauth"`).
- Tests (R-6.5): a token response missing `drive.file` from `scope` fails with that scope named and saves no token file; a `GET /favicon.ico` before the real redirect does not end the sign-in.

Test: `uv run pytest -q tests/integration/test_cli_auth_google.py tests/unit/test_cli_google_setup.py`.

## Step 9: Gmail caps the full-message read `[worker]`

Files: `src/invoicebot/config/models.py`, `src/invoicebot/fetchers/gmail.py`, `tests/unit/test_fetchers_gmail.py`.

- `GoogleConfig` gets `max_message_bytes: int = 8_388_608` (`gt=0`). `GmailMailbox.__init__` takes `max_message_bytes` and its wiring in `app.py` passes `cfg.google.max_message_bytes` (one-line change there).
- `attachments()` streams the `messages.get?format=full` response like `download()` does and raises `MailboxError` once the body passes `max_message_bytes`. Share the streaming loop between the two in one private helper `_read_capped(url, params, headers, cap) -> bytes`.
- Tests (R-5.2): a full-message body over the cap raises `MailboxError` without decoding; one under it still lists its attachments.

Test: `uv run pytest -q tests/unit/test_fetchers_gmail.py tests/unit/test_config_models.py`.

## Step 10: Drive reads photos off the event loop, and cells are escaped more widely `[worker:low]`

Files: `src/invoicebot/sinks/drive_files.py`, `src/invoicebot/text.py`, `tests/unit/test_text.py`.

- `drive_files._upload_photos`: `await asyncio.to_thread(photo.read_bytes)`.
- `text.escape_formula` also escapes a value whose `lstrip()` starts with a formula prefix, and the prefixes include the full-width `＝ ＋ － ＠`.
- Tests (R-6.6, R-7.10): `" =1+1"`, `"＝1"`, `"＋1"` get the leading `'`; `"abc"` and `""` do not.

Test: `uv run pytest -q tests/unit/test_text.py tests/unit/test_sinks_drive_files.py`.

## Step 11: A full sheet is an error, not a duplicate `[worker:low]`

Files: `src/invoicebot/sinks/google_sheets.py`, `tests/unit/test_sinks_google_sheets.py`.

- `keys()` raises `GoogleError()` when the read returns `max_rows` rows, since a key below that row would be missed and appended again (R-6.3). Say so in the docstring.
- Test (R-6.3): with `max_rows=3` and three rows returned, `keys()` raises `GoogleError`; with two rows it returns them.

Test: `uv run pytest -q tests/unit/test_sinks_google_sheets.py`.

## Step 12: SOL requests are allowed only to SUNAT hosts `[worker]`

Files: `src/invoicebot/fetchers/browser/sol_login.py`, `src/invoicebot/fetchers/browser/sol.py`, `tests/unit/test_browser_sol_login.py`.

- Before the first `fill`, register a route on the page for `"**/*"` whose handler aborts any request whose URL fails `is_sunat_origin` and continues the rest. Add the `route` method to the `LoginPage` protocol. Keep the existing per-step URL checks.
- Tests (R-9.10, R-9.16): a fake page records the route; the handler aborts `https://evil.example/x` and continues `https://` plus a SUNAT host from `sunat_origins`; the route is registered before any `fill`.

Test: `uv run pytest -q tests/unit/test_browser_sol_login.py tests/unit/test_browser_sol.py`.

## Step 13: One home for `"sunat_sol"` `[worker:low]`

Files: `src/invoicebot/sunat_origins.py`, `src/invoicebot/fetchers/sunat_sol.py`, `src/invoicebot/config/checks.py`, `src/invoicebot/secrets.py`, `src/invoicebot/pipeline/manual_message.py`.

- Define `SUNAT_SOL = "sunat_sol"` in `sunat_origins.py` with a one-line comment (the fetcher's and the secrets view's name); the four modules import it instead of their own literal or constant. Keep each module's public constant name as an alias only if something outside `src` imports it (check `grep -rn` in `tests`); otherwise update the tests.

Test: `uv run pytest -q tests/unit/test_fetchers_sunat_sol.py tests/unit/test_config_checks.py && uv run lint-imports`.

## Step 14: One helper runs a browser job for both fetchers `[worker]`

Files: `src/invoicebot/fetchers/portal.py`, `src/invoicebot/fetchers/sunat_sol.py`, `tests/unit/test_fetchers_sunat_sol.py`.

- Extract the shared part of `PortalFetcher._run` and `SunatSolFetcher._run` (the temp dir, `run_helper`, and the TIMEOUT / TECHNICAL / `ValidationError` mapping) into a module-level `run_browser_job(...)` in `portal.py` that returns the helper output or the `FetchResult` failure. Both `_run` methods call it; behavior is unchanged.

Test: `uv run pytest -q tests/unit/test_fetchers_sunat_sol.py tests/integration/test_fetchers_portal.py`.

## Step 15: The browser entry exports no private names `[worker:low]`

Files: `src/invoicebot/fetchers/browser/__main__.py`, `src/invoicebot/fetchers/browser/session.py`, and the tests that import those names.

- Make `_guarded_result` and `_launch_options` public in `session.py` (`guarded_result`, `launch_options`); delete `__all__` and the `_BadRequest`/`_read_request` aliases from `__main__.py`; tests import from the defining module. `grep -rn "_guarded_result\|_launch_options\|_BadRequest\|_read_request" src tests` must list only definitions afterwards.

Test: `uv run pytest -q tests/unit -k "browser"`.

## Step 16: Config checks take one field prefix `[worker:low]`

Files: `src/invoicebot/config/checks.py`, `tests/unit/test_config_checks.py`.

- `_portal_problems` takes one `field_prefix` (e.g. `"portals/<key>.yaml:"` or `"sunat_sol.yaml:query."`) in place of `path_prefix` plus `field_separator`; `_form_incomplete_problem` gets the prefix without its trailing separator. Problem texts are unchanged (existing tests stay as they are and pass).

Test: `uv run pytest -q tests/unit/test_config_checks.py`.

## Step 17: The Drive and Sheets sinks depend on ports `[worker]`

Files: `src/invoicebot/sinks/base.py`, `src/invoicebot/sinks/drive_files.py`, `src/invoicebot/sinks/sheet_rows.py`.

- Add `Protocol`s `FolderDrive` (`ensure_folder`, `put_file`, `folder_link`) and `RowSheet` (`keys`, `append`, `update`, `row_link`) to `sinks/base.py`, each with a docstring stating its invariant (Drive reuses by name; `keys` maps column A to rows). `drive_files` and `sheet_rows` type against them and no longer import `google_drive`/`google_sheets` (except `DriveFile`, which moves to `base.py` if `drive_files` needs it).

Test: `uv run pytest -q tests/unit/test_sinks_drive_files.py tests/unit/test_sinks_sheet_rows.py && uv run mypy src`.

## Step 18: The graph mail tests check the status, not the text `[worker:low]`

Files: `tests/unit/test_fetchers_graph_mail.py`.

- The three assertions on `str(raised.value)` (around lines 110, 131, 233) become `assert isinstance(raised.value.__cause__, GraphError)` and `assert raised.value.__cause__.status == <the status the test sets>`.

Test: `uv run pytest -q tests/unit/test_fetchers_graph_mail.py`.
