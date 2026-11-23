# Plan: MVP phase 9p · harden-leftovers

Roadmap: `docs/plan-mvp.md`. While 9d2 `live-chat` waits for Meta, this phase clears the code-only leftovers from the
roadmap's "Carried over" sections. None of them needs the owner, the live host or a chat reply.

In scope:

1. **`fetch-harden` review:** `no_retry` and `reprocess_min_interval_s` become required, so a caller that forgets
   them fails the type check instead of losing D170's or D171's brake. An index bounds `technical_streak`'s
   `DISTINCT ON`. Add the missing tie test and the `/reprocesar` brake tests.
2. **`harden-later` review:** give the Google secret view one home that `cli.py` and `app.py` both import (R-9.10),
   and scrub dataclasses and pydantic models in log redaction (R-9.12).
3. **`flows-config` and `ops-harden` reviews:** derive the Google accounts in one place, and keep the sink kind
   names in one place.
4. **Recorded, not coded:**
   - D164's pin compares `preferred_username`, which Microsoft says can change. This is an accepted limit: the mailbox
     is the owner's own personal account.
   - An `existing` sheet's header is checked only by `google-setup`. Under D145, a config edit to its columns or
     `tab` means running `google-setup --flow` again. A header cell starting with `=`, `+` or `-` is not supported.
   - `restore_drill.sh` keeps its own `PG*` list on purpose, as a second check after the Python side.

Out of scope (still in the roadmap): the SUNAT token compare-and-delete, revoking a refused sign-in's grant, one DNS
executor, `/gaps` 21-row fetch, the `email` normalization, portal selector rendering in `check_config`, and the
VPS items.

**Needs from the owner first:** nothing.

## Step 1: D172, D145 and D164 notes, roadmap row 9p `[inline: decision and spec wording]`

1. `specs/05-open-questions.md`, after D171, add a row **D172** (status "9p plan, proposed"), "The BR-F03 streak
   reads an index": migration 0015 adds `ix_fetch_attempts_portal_invoice_at` on
   `fetch_attempts (portal_key, invoice_id, at DESC, id DESC)`, so `technical_streak`'s `DISTINCT ON (invoice_id)`
   reads it in order instead of sorting every attempt the portal ever had. The streak's meaning (D170) does not change.
   A time window was rejected because it would change which invoices count.
2. Same file, the D145 row: append "An existing sheet's header is checked only by `google-setup`; after changing
   that sink's `columns` or `tab`, run `google-setup --flow <name>` again before the bot writes a row. A header
   cell starting with `=`, `+` or `-` is not supported, because the written cell is escaped and would never match
   (9p)."
3. Same file, the D164 row: append "The pin compares the id token's `preferred_username`, which Microsoft documents
   as mutable; accepted, because the one account is the owner's own personal mailbox (D91, 9p)."
4. `docs/plan-mvp.md`: add row 9p `harden-leftovers` (plan: `docs/plan-mvp-harden-leftovers.md`, D172) after 9o,
   before 9d2. Strike through (`~~…~~`) and end with "Done in 9p" (or "Recorded in 9p" for the notes):
   - "Carried over from the `fetch-harden` review": the "Small", "Before many invoices" and "Tests" bullets.
   - "Carried over from the `harden-later` review": the `cli.py` Google secret view bullet, the D164
     `preferred_username` bullet and the log-redaction bullet.
   - "Carried over from the `flows-config` review": the "Later" header bullet and the "Later (optional)" sink-kind
     bullet.
   - "Carried over from the `ops-live` review": the `restore_drill.sh` `PG*` bullet ("Kept on purpose in 9p").
   - "Carried over from the `ops-harden` review", "Later (optional)": the Google account derivation is done, the
     rest of that bullet stays open. Split that bullet into a struck-through part and an open part.

Test: `grep -n "D172" specs/05-open-questions.md docs/plan-mvp.md && grep -n "9p" docs/plan-mvp.md` shows each line.

## Step 2: `no_retry` is a required keyword `[worker]`

Files: `src/invoicebot/fetchers/chain.py`, `src/invoicebot/pipeline/fetch.py`, `tests/unit/test_fetchers_chain.py`.

1. `chain.py`: in `decide` and `technical_retry_delay`, make `no_retry` a required keyword-only argument (put `*`
   before it, drop the `= frozenset()` default). `_decide_one` keeps its positional parameter. Add to each docstring
   "`no_retry` is required so no caller loses D170's brake by omission."
2. `pipeline/fetch.py`: every call to `decide` or `technical_retry_delay` passes `no_retry=…` by keyword.
3. `tests/unit/test_fetchers_chain.py`: every call passes `no_retry=frozenset()` (or the set the test already
   uses) by keyword. Add one test, docstring starting `D170`: calling `decide` without `no_retry` raises
   `TypeError`.

Test: `uv run pytest -q tests/unit/test_fetchers_chain.py && uv run mypy src`

## Step 3: `CommandContext.reprocess_min_interval_s` is required `[worker]`

Files: `src/invoicebot/pipeline/commands.py`, `tests/integration/test_commands.py`, `tests/integration/test_payments.py`.

1. `commands.py`: `CommandContext.reprocess_min_interval_s` loses its `= 0` default. If a required field may not
   follow a defaulted one in the dataclass, move it before the first defaulted field. Its docstring line adds "No
   default, so a new entry point cannot drop D171's brake."
2. The two test files: every `CommandContext(...)` passes `reprocess_min_interval_s=0`, except where a test
   already passes a value.
3. `src/invoicebot/pipeline/intake.py` already passes it; do not edit it. If mypy names another caller, stop and
   report it instead of editing a fourth file.

Test: `uv run pytest -q tests/integration/test_commands.py tests/integration/test_payments.py && uv run mypy src`

## Step 4: `IntakeRoles.reprocess_min_interval_s` is required `[inline: one keyword at about 9 call sites in 5 files, over the worker file cap]`

Files: `src/invoicebot/pipeline/intake.py`, `tests/integration/test_intake.py`,
`tests/integration/test_pipeline_actions.py`, `tests/integration/test_commands.py`,
`tests/integration/test_pipeline_teach.py`.

1. `intake.py`: `IntakeRoles.reprocess_min_interval_s` loses its `= 0` default (moved before the first defaulted
   field if needed). Its docstring says it is required for the same reason as `CommandContext`'s.
2. Every `IntakeRoles(...)` in the four test files passes `reprocess_min_interval_s=0`, except where a test already
   passes a value. `pipeline/intake_config.py` already passes it.

Test: `uv run pytest -q tests/integration/test_intake.py tests/integration/test_pipeline_actions.py tests/integration/test_commands.py tests/integration/test_pipeline_teach.py && uv run mypy src`

## Step 5: Index for the BR-F03 streak, and the tie test `[worker]`

Files: `migrations/versions/0015_fetch_attempts_streak_index.py` (new), `tests/integration/test_fetch_attempts_store.py`.

1. New migration, revision `"0015"`, `down_revision` `"0014"`, shaped like `0014_fetch_round.py`. Module docstring:
   adds the index D172 names; adding an index is expand-only and the previous image ignores it.
   `upgrade()`:
   `op.execute("CREATE INDEX IF NOT EXISTS ix_fetch_attempts_portal_invoice_at ON fetch_attempts (portal_key, invoice_id, at DESC, id DESC)")`.
   `downgrade()`: `op.execute("DROP INDEX IF EXISTS ix_fetch_attempts_portal_invoice_at")`.
2. `test_fetch_attempts_store.py`, two tests, docstrings starting `D172` and `D170`:
   - the index exists: `SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_fetch_attempts_portal_invoice_at'`
     returns one row containing `portal_key, invoice_id, at DESC, id DESC`.
   - a tie: one invoice gets two attempts on the same portal in one transaction (same `now()`, so the same `at`),
     first `technical` then `ok`; with `n=1`, `technical_streak` returns `False`, because `id DESC` makes the `ok`
     attempt the newest. Then the reverse order (`ok` then `technical`) on a second invoice of a second portal
     returns `True`. Use the same helpers the file's other streak tests use.

Test: `uv run pytest -q tests/integration/test_fetch_attempts_store.py`

## Step 6: `/reprocesar` brake for a failed invoice and for an admin `[worker]`

Files: `tests/integration/test_commands.py`.

Next to `test_reprocesar_too_soon_refuses_the_round_and_keeps_the_state`, using its fixtures and helpers, add two
tests, docstrings starting `D171`:

1. An invoice in `FAILED` whose `fetch_round_at` is within `reprocess_min_interval_s` of now: `/reprocesar <id>`
   from its sender gets `REPROCESS_TOO_SOON.format(id=invoice_id)`, and the invoice's state and `fetch_round_at` are
   unchanged.
2. An invoice in `MANUAL_REQUIRED` owned by another sender, with a recent `fetch_round_at`: `/reprocesar <id>` from
   an admin gets the same refusal, and the state and `fetch_round_at` are unchanged.

If a `FAILED` invoice is not reprocessable at all under the current code (a different reply), stop and report the
reply instead of changing the code.

Test: `uv run pytest -q tests/integration/test_commands.py -k reprocesar`

## Step 7: One home for the Google secret view `[worker]`

Files: `src/invoicebot/auth/google_oauth.py`, `src/invoicebot/app.py`, `src/invoicebot/cli.py`,
`tests/unit/test_app.py`.

1. `google_oauth.py`: add `def google_secrets(store: SecretStore) -> Secrets` returning
   `store.scoped(GOOGLE_OAUTH_ADAPTER, {GOOGLE_CLIENT_SECRET})`, docstring "Return the age-store view the Google
   OAuth clients read their secret through (R-9.10)." Import `SecretStore` and `Secrets` from wherever `app.py`
   imports them.
2. `app.py`: delete `_google_secrets`; its callers use `google_secrets` from `google_oauth`.
3. `cli.py`: the two inline `.scoped(GOOGLE_OAUTH_ADAPTER, {GOOGLE_CLIENT_SECRET})` builds (around lines 276 and 337)
   call `google_secrets(...)` on the same store instead, keeping whatever follows the `.scoped(...)` call. Drop
   imports that become unused.
4. `tests/unit/test_app.py`: one test, docstring starting `R-9.10`: `google_secrets` on a store holding
   `google_client_secret` and another key gives a view whose `enabled` is true and which does not expose the other
   key (use the store helper the file already uses; if none exists, build a `SecretStore` the way other unit
   tests do).

Test: `uv run pytest -q tests/unit/test_app.py tests/unit/test_cli.py tests/integration/test_cli_auth_google.py && uv run lint-imports`

## Step 8: One home for Google account derivation and sink kind names `[worker]`

Files: `src/invoicebot/config/models.py`, `src/invoicebot/auth/providers.py`, `src/invoicebot/app.py`,
`tests/unit/test_config_models.py`.

1. `models.py`: rename `_google_accounts_in` to `google_accounts_in` (public, same docstring). Add module constants
   `GOOGLE_DRIVE_KIND = "google_drive"` and `GOOGLE_SHEETS_KIND = "google_sheets"` next to the sink models with a
   comment that the `Literal[...]` annotations must repeat the text (pydantic needs a literal). The flow check
   (around line 634) and its error message use the constants.
2. `providers.py`: `google_accounts(cfg)` becomes
   `{account for flow in cfg.flows.values() for account in google_accounts_in(flow)}`. Keep `_google_sink_accounts`
   and `_gmail_inbox_accounts`; `google_scopes_for` still needs each part. Docstring cites D138 too.
3. `app.py`: delete its `GOOGLE_DRIVE_KIND`/`GOOGLE_SHEETS_KIND` and import them from `config.models`. If another
   module imports them from `app`, point it at `config.models` only when that module is one of these four files;
   otherwise stop and report.
4. `tests/unit/test_config_models.py`, docstrings starting `R-6.5`: a flow with a Drive sink, a Sheets sink and a
   `gmail` inbox on three accounts gives all three from `google_accounts_in`; a flow with a Graph inbox gives none
   for the inbox.

Test: `uv run pytest -q tests/unit/test_config_models.py tests/unit/test_app_wiring.py && uv run mypy src && uv run lint-imports`

## Step 9: Log redaction scrubs dataclasses and pydantic models `[worker]`

Files: `src/invoicebot/logging.py`, `tests/unit/test_logging.py`.

1. `_scrub`: before the final `str(value)` fallback, a dataclass instance
   (`dataclasses.is_dataclass(value) and not isinstance(value, type)`) is scrubbed as
   `{field.name: getattr(value, field.name) for field in dataclasses.fields(value)}` at the same `depth` (not
   `asdict`, which deep-copies and recurses past the depth cap), and a pydantic `BaseModel` instance as
   `dict(value)` (its top-level fields, same reason). Both go through the dict branch, so personal keys are
   redacted. Docstring adds "dataclasses and pydantic models are scrubbed as their fields (R-9.12)."
2. `tests/unit/test_logging.py`, docstrings starting `R-9.12`, using the file's existing record helper:
   - a frozen dataclass with a `ruc` (a synthetic value from `config/example.yaml`) and `count=2` in `extra=` logs `ruc` redacted and `count` 2.
   - a pydantic model with `phone_number` and `label` logs `phone_number` redacted and `label` kept.
   - a dataclass nested past `_SCRUB_MAX_DEPTH` becomes the nested placeholder.
   - a dataclass class (not an instance) is logged as its `str`, not scrubbed as fields.

Use only the synthetic values already used in `test_logging.py` or `config/example.yaml`.

Test: `uv run pytest -q tests/unit/test_logging.py`
