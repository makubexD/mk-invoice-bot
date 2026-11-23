# Plan: MVP phase 9q · harden-tail

Roadmap: `docs/plan-mvp.md`. While 9d2 `live-chat` waits for Meta, this phase clears the last code-only leftovers in
the roadmap's "Carried over" sections. None needs the owner, the live host, a VPS or a chat reply. Telegram work is
its own roadmap (`docs/plan-telegram.md`) and is not in this phase.

In scope:

1. **`harden-leftovers` review:** `IntakeSettings.roles` becomes required; the remaining log-redaction gaps (pydantic
   aliases, a field that raises on read, `NamedTuple`, mappings and sets); the gmail provider and OneDrive sink kind
   get one home; the R-9.10 view test covers an enabled view.
2. **`ops-harden` review, "Later (optional)":** compare-and-delete the SUNAT token on a 401; revoke the grant of a
   refused wrong-mailbox Google sign-in; `/gaps` shows `…` only when it really cut the list; a wiring test for the
   `github_health` choice; one email normalization.
3. **Recorded, not coded:**
   - `handle_inbound(roles=)` keeps its `NO_ROLES` default for the ~150 test calls; production builds `IntakeSettings`,
     whose `roles` is now required, and Telegram's shared intake route (`docs/plan-telegram.md`, T0) goes through it.
   - One DNS executor per browser session is already one per helper process: each session runs in its own helper.
   - `file-b` note on E11 (R-8.2 failure message for flow B): done by BR-D03, BR-S03 and BR-S03_admin.
   - The `spreadsheets` scope blast radius under D145 (no sink is `existing: true` today).

Out of scope (still in the roadmap): the `check_config` selector render (with the next portal), `batch_preview`
optionality (post-MVP `on_command`), everything Meta, VPS or owner.

## Step 1: Roadmap row 9q and the recorded notes `[inline: spec and roadmap wording]`

1. `docs/plan-mvp.md`: add row 9q `harden-tail` (plan: `docs/plan-mvp-harden-tail.md`) after 9p, before 9d2.
   Strike through (`~~…~~`) and end with "Done in 9q" (or "Recorded in 9q"):
   - "Carried over from the `harden-leftovers` review": all four bullets (the `roles` bullet "Recorded in 9q" for
     `handle_inbound`, done for `IntakeSettings`).
   - "Carried over from the `ops-harden` review", "Later (optional)": the SUNAT token, revoke, DNS executor (recorded),
     `github_health`, `/gaps` and email normalization parts. Split the bullet so any part left stays open.
   - The `file-b` note on E11 (recorded: BR-D03, BR-S03, BR-S03_admin).
   - "Carried over from the `flows-config` review": the `spreadsheets` scope bullet (recorded under D145).
2. `specs/05-open-questions.md`, the D145 row: append "With `existing: true` the `spreadsheets` scope reaches every
   sheet the Google account can open, not only the configured one; use an account that holds only the target sheet
   (9q)."

Test: `grep -n "9q" docs/plan-mvp.md specs/05-open-questions.md` shows each line.

## Step 2: `IntakeSettings.roles` is required `[worker]`

Files: `src/invoicebot/pipeline/intake.py`, and the test files that build `IntakeSettings(...)` without `roles`.

1. `intake.py`: `IntakeSettings.roles` loses its `= NO_ROLES` default. Its docstring adds "`roles` has no default, so
   an entry point built from config cannot drop D171's brake." `handle_inbound`'s `roles` default stays.
2. Every `IntakeSettings(...)` in `src` and `tests` passes `roles=`; tests pass `roles=NO_ROLES`. `intake_config.py`
   already passes it. If more than three test files need the change, stop and report the list.

Test: `uv run pytest -q tests/integration/test_intake.py tests/integration/test_webhook.py && uv run mypy src`

## Step 3: Redaction checks pydantic aliases and survives a failing field `[worker]`

Files: `src/invoicebot/logging.py`, `tests/unit/test_logging.py`.

1. `_fields_of`: for a pydantic model, a field whose `alias` or `serialization_alias` is personal (`_is_personal`) is
   keyed by that alias, so the dict branch redacts it. A dataclass field or model attribute that raises
   `AttributeError` on read becomes `_UNREADABLE = "[unreadable]"` (a new module constant next to `_NESTED`) instead of
   dropping the log line.
2. Tests, docstrings starting `R-9.12`: a model with `tax_id: str = Field(alias="ruc")` logs `ruc` redacted and never
   the value; a dataclass with an unset `init=False` field logs the other fields and `[unreadable]`.

Test: `uv run pytest -q tests/unit/test_logging.py`

## Step 4: Redaction keeps `NamedTuple` field names and scrubs mappings and sets `[worker]`

Files: `src/invoicebot/logging.py`, `tests/unit/test_logging.py`.

1. `_fields_of`: a `NamedTuple` instance (a `tuple` with `_fields`) converts with `value._asdict()`. `_scrub`: any
   `collections.abc.Mapping` goes through the dict branch (not only `dict`); a `set` or `frozenset` is scrubbed as a
   sorted-by-`str` list. Keep complexity under the lint caps (a helper is fine).
2. Tests, docstrings starting `R-9.12`: a `NamedTuple` with a `ruc` field logs `ruc` redacted; a `MappingProxyType`
   with `phone_number` logs it redacted; a `frozenset` holding a loaded secret logs the secret masked.

Test: `uv run pytest -q tests/unit/test_logging.py`

## Step 5: One home for the gmail provider `[worker]`

Files: `src/invoicebot/config/models.py`, `src/invoicebot/auth/providers.py`, `src/invoicebot/pipeline/deliver.py`,
`src/invoicebot/app.py`.

1. `models.py`: add `GMAIL_PROVIDER = "gmail"` next to the email inbox model, with the same comment as
   `GOOGLE_DRIVE_KIND` that the `Literal[...]` annotation repeats the text. The `"gmail"` comparisons in `models.py`
   (about lines 603 and 876) use the constant.
2. `providers.py`: delete its own `GMAIL_PROVIDER` and import it from `config.models`.
3. `deliver.py` and `app.py`: import `GMAIL_PROVIDER` from `config.models`, not `auth.providers` (mypy is strict, so
   no implicit re-export).

Test: `uv run pytest -q tests/unit/test_config_models.py tests/unit/test_app_wiring.py tests/integration/test_pipeline_deliver_dispatch.py && uv run mypy src && uv run lint-imports`

## Step 6: One home for the OneDrive sink kind and the Google account derivation `[worker]`

Files: `src/invoicebot/config/models.py`, `src/invoicebot/auth/providers.py`, `src/invoicebot/app.py`,
`tests/unit/test_config_models.py`.

1. `models.py`: add `ONEDRIVE_FOLDER_KIND = "onedrive_folder"` next to `GOOGLE_DRIVE_KIND`, with the same comment.
   Add `google_sink_accounts_in(flow)` and `gmail_inbox_accounts_in(flow)`; `google_accounts_in` returns their
   concatenation, in the same order as today.
2. `providers.py`: `_google_sink_accounts` and `_gmail_inbox_accounts` become set comprehensions over the two new
   helpers.
3. `app.py`: delete `ONEDRIVE_FOLDER_KIND`, import it from `config.models`; reword the comment above it.
4. Test, docstring starting `R-6.5`: `GMAIL_PROVIDER` and `ONEDRIVE_FOLDER_KIND` each equal the single argument of
   their model's `Literal` annotation (`typing.get_args` on the model field), so the two cannot drift.

Test: `uv run pytest -q tests/unit/test_config_models.py tests/unit/test_app_wiring.py && uv run mypy src && uv run lint-imports`

## Step 7: The Google secret view test covers an enabled view `[worker]`

Files: `tests/unit/test_app_wiring.py`.

Add one test next to `test_google_secret_view_declares_only_the_client_secret`, docstring starting `R-9.10`: a
`_RecordingStore` holding `google_client_secret` and `other_key` gives a `google_secrets` view whose `enabled` is true,
whose `missing` is empty, and whose `get("other_key")` raises (use whatever the view raises for an undeclared key; read
`src/invoicebot/secrets.py`).

Test: `uv run pytest -q tests/unit/test_app_wiring.py`

## Step 8: Compare-and-delete the SUNAT token on a 401 `[worker]`

Files: `src/invoicebot/store/kv.py`, `src/invoicebot/sunat/token.py`, `src/invoicebot/pipeline/validate.py`,
`tests/integration/test_sunat_token.py`.

1. `kv.py`: add `delete_if(conn, key, value) -> bool`, `DELETE FROM kv WHERE key = %s AND value = %s`, returning
   whether a row went. Match the column types `kv.delete` and `kv.set` use.
2. `token.py`: `forget(pool, rejected)` deletes the stored token only when it is still `rejected`, so a token another
   worker fetched after the 401 survives. Docstring cites R-4.4 or the rule the module already cites.
3. `validate.py` (about line 183): pass the access token that got the 401.
4. Tests (integration, real Postgres), docstrings citing the same rule: a stored token A is deleted by `forget(pool, A)`;
   a stored token B survives `forget(pool, A)`.

Test: `uv run pytest -q tests/integration/test_sunat_token.py tests/integration/test_pipeline_validate.py && uv run mypy src`

## Step 9: Revoke the grant of a refused wrong-mailbox Google sign-in `[worker]`

Files: `src/invoicebot/auth/google_oauth.py`, `src/invoicebot/cli.py`, `tests/integration/test_cli_auth_google.py`.

1. `google_oauth.py`: add `async def revoke(client: httpx.AsyncClient, token: str) -> None`, a POST to
   `https://oauth2.googleapis.com/revoke` with form field `token`, under the module's existing timeout constant. A
   non-2xx answer or a transport error is logged at warning without the token and not raised: the refusal still stands.
2. `cli.py` (about line 606, where `auth-google` refuses a sign-in whose mailbox is not the pinned one): before
   refusing, call `revoke` with the refresh token (or the access token if there is none) through the same client, then
   discard it as today.
3. Test, docstring starting `D138`: with an `httpx.MockTransport`, a wrong-mailbox sign-in posts exactly one revoke
   request carrying the token and still exits non-zero with today's message; a revoke answering 500 changes neither.

Test: `uv run pytest -q tests/integration/test_cli_auth_google.py`

## Step 10: `/gaps` shows `…` only when it cut the list `[worker]`

Files: `src/invoicebot/pipeline/commands.py`, `src/invoicebot/rules/messages_es.py`,
`tests/integration/test_commands.py`.

1. `commands.py` (about lines 93-95 and 409-415): fetch `open_gaps(conn, MAX_GAP_LINES + 1)`, show the first
   `MAX_GAP_LINES`, and add the truncation line only when `MAX_GAP_LINES + 1` came back. Move `GAPS_TRUNCATED` to
   `messages_es.py` next to `GAPS_HEADER` and import it.
2. Tests, docstrings citing the rule `/gaps` already cites: exactly 20 open gaps give no `…`; 21 give 20 lines and `…`.

Test: `uv run pytest -q tests/integration/test_commands.py -k gaps`

## Step 11: A wiring test for the `github_health` choice `[worker]`

Files: `src/invoicebot/app.py`, `tests/unit/test_app_wiring.py`.

1. `app.py` (about line 383): extract the `None if github_app is None else partial(...)` choice into
   `_github_health_run(github_app, ...) -> Callable[..., Awaitable[None]] | None` with the same arguments; the caller
   uses it.
2. Tests, docstrings citing the requirement the daily GitHub check cites: no App gives `None`; an App gives a callable
   bound to it.

Test: `uv run pytest -q tests/unit/test_app_wiring.py && uv run mypy src`

## Step 12: One email normalization helper `[worker]`

Files: `src/invoicebot/text.py`, `tests/unit/test_text.py`, `src/invoicebot/config/models.py`, `src/invoicebot/cli.py`.

1. `text.py`: `def normalize_email(address: str) -> str`, `address.strip().casefold()`, docstring "Return the form
   mailbox pins are compared in (D138, D164)."
2. `config/models.py` (about line 334) and `cli.py` (about line 606): replace their inline `casefold` comparison of an
   email with `normalize_email`.
3. Tests, docstring starting `D138`: case, surrounding spaces and a non-ASCII letter (`ß` casefolds to `ss`).

Test: `uv run pytest -q tests/unit/test_text.py tests/unit/test_config_models.py tests/integration/test_cli_auth_google.py`

## Step 13: The Microsoft mailbox pin uses the same helper `[worker]`

Files: `src/invoicebot/auth/msal_cache.py`, `tests/unit/test_msal_cache.py` (or the file that tests it today).

`msal_cache.py` (about lines 83, 136 and 149): the mailbox comparisons use `normalize_email` from `text.py`. Add one
test, docstring starting `D164`: a cached username differing from the pinned mailbox only by case and spaces is the
same mailbox.

Test: `uv run pytest -q tests/unit -k msal && uv run mypy src`
