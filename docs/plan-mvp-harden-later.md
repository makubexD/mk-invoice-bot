# Plan: MVP phase 9j · harden-later

Roadmap: `docs/plan-mvp.md`, new row 9j. It runs while Meta verification is pending, before 9d2 `live-chat`, and needs nothing from Meta. It fixes the roadmap's open "Later" items that are cheap and still real, pins each Microsoft account to its mailbox (D164), brings the specs and the roadmap up to date, and prepares the Google side of flow A so 9d2 starts configured.

**Goal:**

- The secret views each feature needs have one home, shared by startup and the reload check.
- The padrón import has a total deadline, as the download already does.
- Log redaction also catches personal keys inside longer names (`buyer_ruc`, `phone_number`) and inside nested values.
- `gaps.github.repo` must be `owner/name`.
- Housekeeping cannot delete the tmp folder of an invoice retried while it runs.
- A Microsoft account signs in and refreshes only as the mailbox the config pins it to (D164).
- Spec 03 §4 matches the code, D164 is in spec 05, and the roadmap shows only what is still open.
- The owner's config has the Google OAuth client, the Drive folder and both spreadsheet ids, and the Microsoft mailboxes; `scripts/check_config.py` exits 0 on it.

**Out of scope:**

- SOL's host allowlist and S0.4: SOL stays `todo` until the owner's "Last" items (D153).
- The `existing` sheet header check: no sink is `existing: true` (YAGNI).
- The browser helper's real `/proc` (rootless Podman refuses a fresh procfs) and the VPS items (D149).
- The optional cleanups (sink kind literals, Google account derivation, `restore_drill.sh` keys).

**Needs from the owner:** a Google Cloud OAuth desktop client and a browser sign-in for step 10; the real Microsoft mailbox addresses for the config (step 10). Nothing from Meta.

## Step 1: The secret views have one home `[worker]`

Files: `src/invoicebot/app.py`, `tests/unit/test_app_wiring.py`.

Startup and `_secret_problem` each build the same scoped views with the same key sets (WhatsApp, GitHub, laptop agent, SOL), and repeat the SOL "site active" test (py-code-reviewer, harden-live review). Behavior does not change.

1. Next to `_google_secrets`, add one function per feature that returns its scoped view, each with a one-line docstring citing R-9.10:
   - `_whatsapp_secrets(store) -> Secrets`: `store.scoped(WHATSAPP_ADAPTER, set(WHATSAPP_SECRETS))`;
   - `_github_secrets(store) -> Secrets`: `store.scoped(GITHUB_ADAPTER, {GITHUB_APP_PRIVATE_KEY})`;
   - `_laptop_secrets(store) -> Secrets`: `store.scoped(LAPTOP_ADAPTER, {LAPTOP_AGENT_KEY})`;
   - `_sol_secrets(store) -> Secrets`: `store.scoped(SUNAT_SOL, set(SECRETS))`.
2. Add `_sol_site(cfg, directory) -> <the site type> | None`: the site when `cfg.sunat.sol.enabled`, `directory.sunat_sol` is set and its `status == "active"`, else `None`.
3. Replace every inline `store.scoped(...)` for these four features in `app.py` (the lifespan's WhatsApp check, `_secret_problem`, `_laptop_root_and_key`, `_github_app`, `_sol_secrets_unusable`, `_sol_fetcher`) with the new functions, and both SOL "active" tests with `_sol_site`. Grep `app.py` afterwards: `store.scoped(` appears only inside the five view functions (the four new ones and `_google_secrets`) and `_validation_secrets`.

Tests: the existing `_secret_problem` and startup tests pass unchanged. Add one:

- `_whatsapp_secrets`, `_github_secrets`, `_laptop_secrets` and `_sol_secrets` on a `SecretStore` holding every key of each feature (synthetic values) → each view is `enabled`; on an empty store → none is.

Docstring starts with `R-9.10`.

Test: `uv run pytest -q tests/unit/test_app_wiring.py`

## Step 2: The padrón import has a total deadline `[worker]`

Files: `src/invoicebot/config/models.py`, `src/invoicebot/ops/padron_refresh.py`, `tests/integration/test_ops_padron_refresh.py`.

The download is bounded by `download_timeout_s`, but the unzip and COPY are not (py-security-auditor, harden-live review). The live import of 18.5 M rows took 629 s (9e). The ZIP's entry count and ratio are already capped by `check_zip`.

1. `Padron` gets `import_timeout_s: float = Field(1800, gt=0)` with the comment `#: S12.4: the whole import's deadline in seconds (unzip, COPY and swap).`
2. In `refresh_padron`, run `_import(pool, zip_path)` under its own `asyncio.timeout(padron_cfg.import_timeout_s)`, with the same pattern as the download: a `TimeoutError` while the deadline has expired raises `PadronRefreshError("timeout")` from it; any other `TimeoutError` is re-raised. The ZIP is still removed in `finally`. A cancelled import leaves the old padrón in place, because the swap is the last transaction.
3. Update the docstring: both the download and the import are bounded.

Tests (the file's existing fixtures and `httpx.MockTransport`):

- `import_timeout_s` set to a tiny value (e.g. `0.001`) with a valid small ZIP → `PadronRefreshError` with `reason == "timeout"`, the ZIP file is gone, and the `padron` rows from before the call are unchanged;
- the default config → `import_timeout_s == 1800`.

Docstrings start with `S12.4`.

Test: `uv run pytest -q tests/integration/test_ops_padron_refresh.py tests/unit/test_config_models.py`

## Step 3: Log redaction catches personal keys in longer names and nested values `[worker]`

Files: `src/invoicebot/logging.py`, `tests/unit/test_logging.py`.

`JsonFormatter` redacts an `extra=` key only when the whole lower-cased key is in `_PERSONAL_KEYS`, so `buyer_ruc` and `phone_number` pass, and a `dict` or `list` value is written as `str(value)` with its personal keys inside. `"name"` can never match, because `name` is a standard `LogRecord` attribute (both reviewers, harden-live review).

1. Add `_is_personal(key: str) -> bool`: true when any `_`-separated part of `key.lower()` is in `_PERSONAL_KEYS`, or the whole lower-cased key is. Remove `"name"` from `_PERSONAL_KEYS` and add `"nombre"` (the field name the Spanish texts use); keep the rest.
2. Add `_scrub(value: Any, secret_re) -> Any`: a `dict` becomes a new dict whose values under `_is_personal` keys are `_PERSONAL_REDACTED` and whose other values are scrubbed recursively; a `list` or `tuple` becomes a list of scrubbed items; a `str` goes through `_redact`; `int`, `float`, `bool` and `None` pass through; anything else is `_redact(str(value), secret_re)`. Cap the depth at 5: deeper values become `"[nested]"`.
3. In `JsonFormatter.format`, use `_is_personal(key)` for the top-level check and `_scrub(value, self._secret_re)` for every other value, replacing the current `str(value)` conversion and `_redact_value` call. Delete `_redact_value` if nothing else uses it.

Tests (same file, the existing capture pattern):

- `extra={"buyer_ruc": "20000000001"}` → `"buyer_ruc": "[redacted]"`, and the line does not contain `20000000001`;
- `extra={"phone_number": "51999999999"}` → `"[redacted]"`;
- `extra={"payload": {"ruc": "20000000001", "count": 3}}` → `"payload": {"ruc": "[redacted]", "count": 3}`;
- `extra={"items": [{"email": "a@example.com"}]}` → `"items": [{"email": "[redacted]"}]`;
- `extra={"reason": "status", "rucs_checked": 4}` → `"reason": "status"`, and `"rucs_checked": "[redacted]"` (a part match; documents that counts need a key without a personal part);
- six nested dict levels → the sixth is `"[nested]"`.

Docstrings start with `R-9.12`.

Test: `uv run pytest -q tests/unit/test_logging.py`

## Step 4: `gaps.github.repo` must be `owner/name` `[worker:low]`

Files: `src/invoicebot/config/models.py`, `tests/unit/test_config_models.py`.

`GapsGithub.repo` is any string, and `close_issue` and the issue URLs are built from it (py-security-auditor, harden-live review).

1. `repo: str = Field(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")`, with the comment `#: R-11.5: the GitHub repository as owner/name.`

Tests, parametrized, on `GapsGithub.model_validate` with the other fields from the example config:

- `"makubexD/mk-invoice-bot"`, `"a/b"`, `"org-1/repo.name_2"` → valid;
- `"mk-invoice-bot"`, `"a/b/c"`, `"a/"`, `"/b"`, `"a b/c"`, `"https://github.com/a/b"`, `"-a/b"` → `ValidationError`.

Docstrings start with `R-11.5`.

Test: `uv run pytest -q tests/unit/test_config_models.py`

## Step 5: Housekeeping holds the invoices it clears `[worker]`

Files: `src/invoicebot/ops/housekeeping.py`, `tests/integration/test_ops_housekeeping.py`.

`_remove_stale_tmp` reads the states, releases the connection, then removes the folders. A FAILED invoice retried in between moves out of FAILED and starts a new download into the folder that is then removed (harden-live review, "Later").

1. In `_remove_stale_tmp`, open `pool.connection()` and `conn.transaction()` around both the state read and the removal. `_invoice_states` selects `... FOR SHARE` (rename nothing else), so a concurrent state change on those rows waits until the folders are gone and the transaction ends. Invoice ids no row has are not locked and stay removable, as today.
2. Update the docstrings: the rows are held while their folders are removed (R-9.7).

Tests (the file's fixtures, real Postgres):

- a FAILED invoice with a tmp folder; a second connection opens a transaction and runs `UPDATE invoices SET state = 'FETCHING' WHERE id = ...` without committing; `housekeep` under `asyncio.timeout(1)` raises `TimeoutError` (it waits for the row lock); after that connection rolls back, `housekeep` removes the folder;
- the existing cases still pass (terminal removed, non-terminal kept, unknown id removed, symlink kept).

Use the state constants from `invoicebot.states`, not literals. Docstrings start with `R-9.7`.

Test: `uv run pytest -q tests/integration/test_ops_housekeeping.py`

## Step 6: The config pins each Microsoft account to a mailbox (D164) `[worker]`

Files: `src/invoicebot/config/models.py`, `config/example.yaml`, `tests/unit/test_config_models.py`.

`TokenProvider` uses the one account in its cache without checking that it is the mailbox the account name means, so a device-code sign-in with the wrong Microsoft account goes unnoticed (py-security-auditor, ops-live review). Google already pins accounts to mailboxes (D138); Microsoft gets the same map.

1. Move the body of `Google._accounts_map_names_to_addresses` into a module function `_account_addresses(value: dict[str, str]) -> dict[str, str]` (same checks and messages). `Google`'s validator calls it.
2. `Microsoft` gets `accounts: dict[str, str] = Field(default_factory=dict)` with the comment `#: D164: the mailbox each account name is pinned to, checked at sign-in and on every token.` and a `field_validator("accounts")` that calls `_account_addresses`.
3. In `config/example.yaml`, change the one-line `microsoft:` entry to a block with `client_id`, `timeout_s` and `accounts`, mapping every account name the example's flows use for Microsoft (OneDrive targets, Graph mailers, Graph inboxes; `invoicebot.auth.providers.microsoft_accounts` lists them) to a synthetic `@example.com` address. Add the comment `# D164: each account name is pinned to the mailbox auth-microsoft signs in.`

Tests:

- the example config loads, and `microsoft.accounts` has an entry for every name in `microsoft_accounts(cfg)`;
- `Microsoft` with `accounts={"Bot": "bot@example.com"}` → `ValidationError` (name not lowercase); `{"bot": "not-an-address"}` → `ValidationError`; `{"bot": "Bot@Example.com"}` → stored as `bot@example.com`;
- the existing Google account tests pass unchanged.

Docstrings start with `R-7.9` and `D164`.

Test: `uv run pytest -q tests/unit/test_config_models.py tests/unit/test_runtime_config.py`

## Step 7: A Microsoft token is used only for its pinned mailbox (D164) `[worker]`

Files: `src/invoicebot/auth/msal_cache.py`, `src/invoicebot/auth/providers.py`, `tests/unit/test_auth_msal_cache.py`.

1. `TokenProvider.__init__` takes `mailbox: str` after `account`, stored lower-cased.
2. Add `class WrongMailbox(AuthRequired)` whose message is `Microsoft account {account} signed in as another mailbox than microsoft.accounts names (D164)`; it never includes either address.
3. `login_device_code`: after a successful `acquire_token_by_device_flow`, read `result["id_token_claims"]["preferred_username"]` (missing → `WrongMailbox`); if its `casefold()` differs from the pinned mailbox, raise `WrongMailbox` and do **not** save the cache.
4. `_token`: with exactly one account, compare `accounts[0]["username"].casefold()` with the pinned mailbox; a mismatch raises `AuthRequired(self._account)` before `acquire_token_silent`.
5. `build_token_provider`: `mailbox = cfg.microsoft.accounts.get(account)`; `None` raises `ValueError(f"microsoft.accounts has no mailbox for account '{account}' (D164)")`, so startup fails fast. Pass it to `TokenProvider`.

The CLI already prints `sign-in failed: {error}` for any `AuthRequired`, so `cli.py` does not change.

Tests (the file's existing fake MSAL application pattern; synthetic addresses only):

- device-code sign-in whose id token names the pinned mailbox in another case → cache saved;
- sign-in naming another mailbox → `WrongMailbox`, and no cache file is written (an existing file is left as it was);
- sign-in result without `id_token_claims` → `WrongMailbox`;
- a cache whose one account's `username` is another mailbox → `token()` raises `AuthRequired`;
- a cache whose account matches → a token is returned;
- `build_token_provider` for an account missing from `microsoft.accounts` → `ValueError` naming the account.

Docstrings start with `R-7.9` and `D164`.

Test: `uv run pytest -q tests/unit/test_auth_msal_cache.py tests/integration/test_cli_auth.py`

## Step 8: Specs follow the code; D164 recorded `[inline: spec wording and owner decisions]`

Files: `specs/03-design.md`, `specs/05-open-questions.md`.

1. Spec 03 §4: an answer in `CONFIRMING` re-enters `ROUTING` (which then moves to `VALIDATING`), and `/reprocesar` on a failed batch moves the invoice back to `DELIVERING` to resend, as the code does (9i review).
2. Spec 05: add D164 (Microsoft accounts pinned to mailboxes in `microsoft.accounts`, checked at sign-in and on every token, mirrors D138; 9j plan, proposed).

Test: `uv run pytest -q tests/unit/test_docs.py`

## Step 9: The roadmap lists only what is open `[inline: judgment, each item checked against the code]`

Files: `docs/plan-mvp.md`.

1. Add row 9j `harden-later` (plan: `docs/plan-mvp-harden-later.md`, D164).
2. For every carried-over item that is not struck through, check the code and the archived plans: strike through the ones done (`~~…~~ Done in <phase>.`), and strike this plan's items as done in 9j.
3. "Known drift": strike the `portals.yaml` and S1.2 entries (`config/stores/` landed in 9e; `BR-R10` and per-flow inboxes exist) and the spec 03 §4 entry (step 8).

Test: `uv run pytest -q tests/unit/test_docs.py` and `npx prettier --check docs/plan-mvp.md`

## Step 10: The Google side of flow A and the Microsoft mailboxes in the live config `[inline: owner at the Google console and in the browser]`

Files: none in git (the owner's `config/config.yaml` and age store stay out of the repo).

With the owner, following `docs/archive/plan-mvp-flow-a.md` § "Deferred: live check" and `deploy/README.md`:

1. The owner creates the OAuth desktop client in Google Cloud; `google.client_id` goes into the config and `google_client_secret` into the age store (`scripts/secrets_edit.py`).
2. `invoicebot auth-google` for each account in `google.accounts`; a wrong mailbox is refused (D138).
3. `invoicebot google-setup --flow personal_google` and `--flow personal_dni`; the Drive `root_folder_id` and both `spreadsheet_id`s go into the config.
4. The owner adds `microsoft.accounts` with the real mailboxes (D164), then runs `invoicebot auth-microsoft` once per account to confirm the pin accepts them.
5. Reload the live app with the new config (`SIGHUP`, D155) and check `/health`.

Not in this step: the M6 run through Drive and Sheets and the Gmail fetch need a chat, so they stay in 9d2.

Test: `uv run python scripts/check_config.py config/config.yaml` exits 0 (only `PORTAL_NOT_ACTIVE` warnings), and `curl -s localhost:8080/health` answers `{"status":"ok","db":"ok"}`.
