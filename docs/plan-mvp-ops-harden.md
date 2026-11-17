# Plan: MVP phase 9a · ops-harden

Roadmap: `docs/plan-mvp.md` row 9. Phase 9 `ops` is split (D136, proposed): 9a `ops-harden` (this plan: the code leftovers that need no host, plus S8.9), 9b `ops-reload` (S8.7 config reload and auto-resume, S10.8's `SIGHUP`, S12.1 memory and latency in CI, S12.4 housekeeping, S12.5 runbook), 9c `ops-host` (needs the VPS: helper sandboxing, cgroup caps, the Compose database password, the first real deploy, the M7 demo) and 9d `ops-live` (S12.7 repo private, then S16.2). M7 is reached when 9c and 9d end.

**Goal:** every `ops` leftover from earlier reviews that is plain code is fixed and tested, and the owner has `/gaps`, `/gap ignorar <id>` and a daily GitHub App health check (S8.9).

**Needs from the owner first:** the VPS is not chosen (owner, 2026-11-17), and the owner said to draft anyway. Nothing in 9a needs a host: the leftovers about the host (helper users and network, cgroup memory caps, Chromium's sandbox, the browser helper's disk cap and kill test, the Compose database password) go to 9c.

## Design choices

Step 1 writes these into the specs (spec 05, D136 to D140). They are proposals for the owner's review of this plan.

- **D136 · Phase 9 split.** `ops` becomes 9a `ops-harden`, 9b `ops-reload`, 9c `ops-host`, 9d `ops-live`, as in the roadmap note above. Only 9c and 9d need the VPS (D42).
- **D137 · A taught link carries a version (amends D118).** `taught_links` gets `version INT NOT NULL DEFAULT 1`; `propose` sets `version = version + 1` when it overwrites a row. The BR-S06 buttons become `t<ruc>v<version>:ok` and `t<ruc>v<version>:no`, and `decide` matches the RUC, the version and `state = 'proposed'`. A press from an older proposal finds nothing and gets the existing "nothing to decide" path. Old-format ids (`t<ruc>:ok`) no longer match the pattern and are ignored as unknown buttons.
- **D138 · Google accounts are pinned to an address.** `google.accounts` maps each account name used by a Google sink or a `gmail` inbox to its email address (config validator: every used name is listed). `auth-google` adds the `openid email` scopes, reads the `id_token` from the token response (received straight from Google's token endpoint over TLS, so its payload is read without a signature check, as Google documents), and refuses to save the token unless `email_verified` is true and the email matches the configured one (case-insensitive). The loopback listener ignores a request whose `state` does not match and keeps waiting until its timeout.
- **D139 · Owner gap commands (S8.9).** `/gaps` lists open and reported gaps (`kind`, `subject`, `count`, issue link) by `last_seen`, at most 20 lines then `…`. `/gap ignorar <fingerprint>` sets the gap `ignored` (alerts and issues skip it already); a later sighting does not reopen it. Both are admin-only: `Command` gains `admin_only: bool`, `handle_command` answers a non-admin as an unknown command, and `help_text` lists admin commands only to admins. A daily task `github_health` (at `gaps.digest_at`) drops the cached installation token, asks for a new one and raises BR-D05 through the existing rate-limited alert on failure; it runs only when GitHub is configured.
- **D140 · Small text changes.** BR-D14 lists at most 10 invoices (newest first) and then `…`. BR-E07 quotes the photo and the PDF caps separately: `… una foto (hasta {image_mb} MB) o un PDF (hasta {pdf_mb} MB) …`, taken from `limits.media.max_bytes`.

Conventions: lint is `uv run ruff check src tests scripts migrations && uv run ruff format --check src tests scripts migrations && uv run mypy src && uv run lint-imports`. Integration tests need Postgres (`podman compose -f deploy/docker-compose.yml up -d postgres`). Every user text is a constant in `rules/messages_es.py`, quoted in spec 06 by step 1 (the parity test needs both). No real RUCs, phone numbers or photos in tests; use `tests/synthetic_rucs.txt`. Integration fixtures date invoices in Lima time. Times come from the database clock. HTTP is faked only with `httpx.MockTransport`.

Out of scope here: the Telegram media port (waits for a second channel), everything in 9b to 9d, and the flow-a live check items.

## Step 1: Record D136–D140 and the new texts `[inline: spec and roadmap text the owner reviews with this plan]`

Files: `specs/05-open-questions.md`, `specs/06-business-rules.md`, `specs/04-tasks.md`, `docs/plan-mvp.md`, `src/invoicebot/rules/messages_es.py`.

- Spec 05: rows D136 to D140 with the text from "Design choices"; D118 notes "amended by D137".
- Spec 06 and `messages_es.py` together: BR-D14's `{invoices}` note says at most 10 items, newest first, then `…`; BR-E07's text uses `{image_mb}` and `{pdf_mb}` (D140); the admin command texts of D139: `GAPS_EMPTY` `No hay brechas abiertas.`, `GAPS_HEADER` `Brechas abiertas:`, a line `{fingerprint} · {kind} · {subject} · {count}×{issue}` (`{issue}` is ` · <url>` or empty), `GAP_IGNORED` `Listo, ignoro la brecha {fingerprint}.`, `GAP_UNKNOWN` `No encuentro la brecha {fingerprint}.`, and the help lines `HELP_GAPS`, `HELP_GAP`.
- Spec 04: S8.9 cites D139.
- Roadmap: row 9 is replaced by rows 9a to 9d (D136) with their backlog and owner needs; the `ops` leftovers are tagged with the sub-phase that takes them (9a for the ones in this plan; 9c for the helper user and network, cgroup caps, Chromium sandbox, helper disk cap, browser kill test and the Compose password; the Telegram item stays open).

Test: `uv run pytest -q tests/unit/test_rules_parity.py`.

## Step 2: State constants from `states.py`, part 1 `[worker:low]`

Files: `src/invoicebot/pipeline/actions.py`, `src/invoicebot/pipeline/route.py`, `src/invoicebot/pipeline/extract.py`.

- Delete the local state aliases (`CONFIRMING_STATE`, `ROUTING_STATE`, `BATCHED_STATE`, `CANCELLED_STATE`, `DONE_STATE`, `ACTIVE_STATES`' literals, `"EXTRACT_FAILED"`, the `"EXTRACTING"` and `"ROUTING"` literals) and import the names from `invoicebot/states.py`. Before deleting an alias, `grep -rn` for importers in `src` and `tests`; repoint them to `states`. Keep `ACTIVE_STATES` as a local `frozenset({RECEIVED, EXTRACTING})`. Batch states (`"sent"`, `"failed"`) and gap kinds stay where they are.
- No behaviour change, no new tests.

Test: `uv run pytest -q tests/integration/test_pipeline_actions.py tests/integration/test_pipeline_route.py tests/integration/test_pipeline_extract.py`.

## Step 3: State constants from `states.py`, part 2 `[worker:low]`

Files: `src/invoicebot/pipeline/fetch.py`, `src/invoicebot/pipeline/deliver.py`, `src/invoicebot/pipeline/handlers.py`.

- Same as step 2 for `FETCHING_STATE`, `DELIVERING_STATE`, `MANUAL_STATE`, `BATCHED_STATE`, `DONE_STATE`, `MANUAL_REQUIRED_STATE` and the `"CANCELLED"` literal in `handlers.py`. `teach.py` imports `PORTAL_MISSING` from `fetch` (a gap kind, not a state): leave it.
- No behaviour change, no new tests.

Test: `uv run pytest -q tests/integration/test_pipeline_fetch.py tests/integration/test_pipeline_deliver.py tests/integration/test_pipeline_handlers.py`.

## Step 4: A permanent Meta error stores only its status and code `[worker]`

Files: `src/invoicebot/channels/whatsapp/transport.py`, `src/invoicebot/queue/outbox.py`, `tests/integration/test_whatsapp_transport.py`.

- `transport.py`: raise `PermanentSendError(f"WhatsApp API error {exc.status}: code {exc.code}")` (the attribute names `WhatsAppError` has), never `str(exc)`, so Meta's free text is neither stored nor logged.
- `outbox.py`: the dead-row log line says `permanent` when the failure was permanent (`"outbox row %d on channel %s is dead (permanent) after %d attempts: %s"`), else as today.
- Tests: R-9.16 a permanent code whose Meta message echoes a parameter (`"Param text['body'] = 0000"`) leaves `last_error` with the status and code and without that text; the existing permanent-failure test still passes. If `test_outbox.py` holds the log assertion, add the `permanent` check there.

Test: `uv run pytest -q tests/integration/test_whatsapp_transport.py tests/integration/test_outbox.py`.

## Step 5: DNS gets its own executor `[worker]`

Files: `src/invoicebot/net/guard.py`, `tests/unit/test_net_guard.py`.

- `SystemResolver.__init__` gains `getaddrinfo: Callable[..., list[...]] = socket.getaddrinfo` and creates `concurrent.futures.ThreadPoolExecutor(max_workers=DNS_WORKERS, thread_name_prefix="dns")` (`DNS_WORKERS = 4`, a module constant). `resolve` runs `loop.run_in_executor(self._executor, partial(getaddrinfo, host, None, type=socket.SOCK_STREAM))` under the same deadline. A `close()` method calls `self._executor.shutdown(wait=False, cancel_futures=True)`; the owner of the resolver (grep its constructions in `src`) calls it on shutdown.
- Tests: R-9.10 with a `getaddrinfo` that blocks on a `threading.Event`, `DNS_WORKERS + 2` resolves all return `[]` at the deadline, and `asyncio.to_thread(lambda: 1)` still answers within 1 s afterwards (the default executor is free); set the event at the end. The existing deadline and malformed-host tests still pass.

Test: `uv run pytest -q tests/unit/test_net_guard.py`.

## Step 6: Cap the BR-D14 list `[worker]`

Files: `src/invoicebot/pipeline/intake.py`, `tests/integration/test_intake.py`.

- `_ask_which_invoice`: `BR_D14_MAX_ITEMS = 10`; order the open invoices newest first (by id, descending), render the first 10 and append `, …` when there are more (D140).
- Tests: BR-D14 with 12 stuck invoices the message lists 10 `#id` items, the newest first, and ends with `…`; BR-D14 with 2 it lists both with no `…`.

Test: `uv run pytest -q tests/integration/test_intake.py`.

## Step 7: A real NUL byte in a webhook batch `[worker]`

Files: `tests/integration/test_webhook.py`.

- New test: R-1.2 a signed batch of three text messages whose middle one has `"\u0000"` inside its `id` (a field the parser does not clean) answers 200, stores the first and the third message, and stores nothing for the middle one. No monkeypatching: Postgres (or psycopg) refuses the NUL for real. If the webhook answers 503 instead, stop and report: that is a bug, not a test to adjust.

Test: `uv run pytest -q tests/integration/test_webhook.py`.

## Step 8: Pin the image bases `[worker:low]`

Files: `deploy/Dockerfile`, `tests/unit/test_deploy_files.py` (new).

- Resolve the current digests with `podman pull <ref>` then `podman image inspect --format '{{index .RepoDigests 0}}' <ref>` for `docker.io/library/python:3.12.16-slim` and `ghcr.io/astral-sh/uv:0.13.7`, and write each reference as `<name>:<tag>@sha256:<digest>` (both `FROM` lines and the `COPY --from`). Add `UV_PYTHON_DOWNLOADS=never` to the builder's `ENV`.
- Tests: R-9.13 every `FROM` and `COPY --from=` image in `deploy/Dockerfile` has `@sha256:` followed by 64 hex characters; R-9.13 the builder sets `UV_PYTHON_DOWNLOADS=never`.

Test: `uv run pytest -q tests/unit/test_deploy_files.py`.

## Step 9: The laptop agent refuses a replay of a failed job `[worker]`

Files: `scripts/laptop_agent.ps1`, `tests/laptop/laptop_agent.Tests.ps1`.

- A second state file `failed-jobs.txt` beside `sent-jobs.txt`. When a send fails, `Send-Job` appends the id to `failed-jobs.txt` first, then removes it from `sent-jobs.txt` (both in a `try`; a state-write failure is logged and the error receipt is still written). Before sending, a job whose id is in either file is refused: the job file is removed, an error receipt with reason `replay` is written, and nothing is sent. A `sent` receipt is never written for a refused job.
- Tests: S11.14 a failed send leaves the id in `failed-jobs.txt` and not in `sent-jobs.txt`; S11.14 the same job dropped into `_outbox/` again is refused with a `replay` error receipt and `Send` is never called; S11.14 a job id already in `sent-jobs.txt` is still refused as before.

Test: `pwsh -NoProfile -c "Invoke-Pester -CI tests/laptop"`.

## Step 10: Migration for the taught-link version `[worker:low]`

Files: `migrations/versions/0011_taught_link_version.py` (new), `tests/integration/test_migrations.py`.

- Revision `0011`, `down_revision = "0010"`: `op.execute("ALTER TABLE taught_links ADD COLUMN version INT NOT NULL DEFAULT 1")`. Expand only; the downgrade drops the column, as earlier revisions do.
- Test: D137 after upgrading, an existing row has `version = 1`. (If the migration tests live in another file, add it there.)

Test: `uv run pytest -q tests/integration/test_migrations.py`.

## Step 11: [Aprobar] matches the proposal version `[worker]`

Files: `src/invoicebot/store/taught_links.py`, `src/invoicebot/pipeline/teach.py`, `tests/integration/test_pipeline_teach.py`.

- `store`: `propose` sets `version = taught_links.version + 1` on conflict and returns the version; `decide(conn, ruc_emisor, version, approved)` adds `AND version = %s` to its `SELECT … FOR UPDATE`.
- `teach.py`: `_decision_buttons` builds `t<ruc>v<version>:ok|no`; `_PRESS_PATTERN` becomes `t([0-9]{11})v([0-9]{1,9}):(ok|no)`; `handle_press` passes the version (D137).
- Tests: BR-S06 a press on the current proposal approves it; BR-S06 after [Rechazar] and a new proposal for the same RUC, a press of the first proposal's [Aprobar] changes nothing and the new proposal stays `proposed`; BR-S06 a button id in the old `t<ruc>:ok` form is not handled. The existing teach tests pass with the new id format (update their ids, not their assertions).

Test: `uv run pytest -q tests/integration/test_pipeline_teach.py`.

## Step 12: The restore drill keeps the password off the command line `[worker:low]`

Files: `scripts/restore_drill.sh`, `tests/integration/test_restore_drill.py`.

- Split the URL once into `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE` (export them; use `python3 -c` with `urllib.parse` reading the URL from stdin, not argv, if bash parsing is fragile) and call `psql`/`pg_restore` with no `--dbname` URL. The URL itself is read from the environment variable `RESTORE_DATABASE_URL` instead of `$3`; update the usage line.
- Test: R-9.15 the drill still restores a backup (existing test, now passing the URL in the environment); R-9.15 a fake `psql` earlier on `PATH` records its argv, and no argument contains the password.

Test: `uv run pytest -q tests/integration/test_restore_drill.py`.

## Step 13: Backup keeps known URL options `[worker]`

Files: `src/invoicebot/ops/backup.py`, `tests/unit/test_ops_backup.py`.

- `_pg_environment`: map query keys through one table, `sslmode → PGSSLMODE`, `sslrootcert → PGSSLROOTCERT`, `connect_timeout → PGCONNECT_TIMEOUT`, `application_name → PGAPPNAME`; any other key raises `ValueError` naming the key (not the value). Keep the host's case (`parts.hostname` lower-cases it: read it from `netloc`).
- Tests: R-9.14 `?sslmode=require` sets `PGSSLMODE=require`; R-9.14 `?options=x` raises `ValueError` naming `options`; R-9.14 a URL without a query sets no `PGSSL*`. (If `test_ops_backup.py` is an integration file, add the tests to a new unit file with this name.)

Test: `uv run pytest -q tests/unit/test_ops_backup.py`.

## Step 14: A gap is raised once under concurrent sightings `[worker]`

Files: `src/invoicebot/store/gaps.py`, `tests/integration/test_store_gaps.py`.

- `upsert_gap` no longer sets `raised_at` in the upsert. After it, run `UPDATE gaps SET raised_at = now() WHERE fingerprint = %s AND raised_at IS NULL AND count >= %s RETURNING 1`; `raised_now` is whether that returned a row. Under READ COMMITTED a second writer waits on the row lock and re-checks `raised_at IS NULL`, so only one sees it.
- Tests: R-11.2 two connections upsert the same new fingerprint at threshold 1 in parallel (start both transactions, run both upserts with `asyncio.gather`, commit both): exactly one result has `raised_now`; the existing first-upsert and threshold tests still pass.

Test: `uv run pytest -q tests/integration/test_store_gaps.py`.

## Step 15: A comment row can wait for its issue `[worker]`

Files: `src/invoicebot/github/transport.py`, `src/invoicebot/app.py`, `tests/unit/test_github_transport.py`.

- A `comment` row without `issue_url` asks a new hook `GitHubHooks.issue_url(fingerprint) -> Awaitable[str | None]`; with no issue yet it raises `SendLater(BUDGET_DELAY, "issue pending")`. `app.py` wires the hook to `store.gaps.get_gap(...).issue_url` in its own connection.
- Tests: D118 a comment row without `issue_url` is sent to the URL the hook returns; D118 with no URL yet it raises `SendLater`; D118 a row with `issue_url` does not call the hook. (If the transport tests live in another file, add them there.)

Test: `uv run pytest -q tests/unit/test_github_transport.py tests/unit/test_app_wiring.py`.

## Step 16: An approval during issue creation keeps its hint `[worker]`

Files: `src/invoicebot/queue/outbox.py`, `src/invoicebot/pipeline/teach.py`, `tests/integration/test_pipeline_teach.py`.

- `set_pending_issue_hint` returns the number of rows it updated.
- `_record_hint`: when the gap has no `issue_url` and no pending row was updated (the `create_issue` row is already `sending`), queue a `comment` row with `fingerprint` and `hint_url` and no `issue_url` (step 15 resolves it at send time), if `setup.github_repo` is set.
- Tests: D118 an approval while the `create_issue` row is `sending` queues one comment row without `issue_url`; D118 an approval while it is `pending` still writes the hint into that row and queues no comment.

Test: `uv run pytest -q tests/integration/test_pipeline_teach.py`.

## Step 17: A 401 from SUNAT drops the cached token `[worker]`

Files: `src/invoicebot/store/kv.py`, `src/invoicebot/sunat/validez.py`, `src/invoicebot/sunat/token.py`.

- `kv.delete(conn, key)`. `validez.py`: a 401 raises `SunatUnauthorized(SunatUnavailable)` (status only in the text). `token.py`: `forget(pool)` deletes `kv['sunat_token']`. The caller of validez (find it: `pipeline/validate.py` `ask_sunat`) on `SunatUnauthorized` calls `forget`, gets a new token and retries once; a second 401 is `SunatUnavailable` as today. No connection is held across the HTTP calls (module docstring).
- Tests in `tests/integration/test_sunat_token.py`: R-5.4 a 401 then a 200 (MockTransport) fetches a second token and returns the answer; R-5.4 two 401s end as unavailable and leave no token cached.

Test: `uv run pytest -q tests/integration/test_sunat_token.py tests/unit/test_sunat_validez.py`.

## Step 18: A recheck with no answer keeps the last answer `[worker]`

Files: `src/invoicebot/store/invoices.py`, `src/invoicebot/pipeline/sunat_recheck.py`, `tests/integration/test_pipeline_sunat_recheck.py`.

- `store`: `bump_rechecks(conn, invoice_id, rechecks)` updates only `rechecks`. `sunat_recheck._record` calls it instead of `set_sunat_status` when the status is `None`.
- Tests: R-5.4 an invoice with a stored `estado_cp` whose recheck gets no answer keeps `estado_cp` and `sunat_status_json` and has `rechecks` bumped; the existing outage-backoff and three-missing tests still pass.

Test: `uv run pytest -q tests/integration/test_pipeline_sunat_recheck.py`.

## Step 19: BR-E07 quotes each cap `[worker]`

Files: `src/invoicebot/pipeline/intake.py`, `src/invoicebot/app.py`, `tests/integration/test_intake.py`.

- Replace the single `media_max_mb` int passed to intake with the media limits (`limits.media.max_bytes`); `_refuse_lone_xml` renders `image_mb` and `pdf_mb` (D140). Follow the value through the webhook deps (grep `media_max_mb`); if that touches a fourth file, it is the deps dataclass only.
- Tests: BR-E07 with image cap 10 MB and PDF cap 20 MB the refusal says `hasta 10 MB` for the photo and `hasta 20 MB` for the PDF.

Test: `uv run pytest -q tests/integration/test_intake.py tests/integration/test_webhook.py`.

## Step 20: Google account emails in config `[worker]`

Files: `src/invoicebot/config/models.py`, `config/example.yaml`, `tests/unit/test_config_models.py`.

- `Google.accounts: dict[str, str]` (name pattern `^[a-z0-9_]+$`, value a lower-cased email checked by a simple `^[^@\s]+@[^@\s]+$`), default empty. The `Config` validator that requires `google` also requires every account a Google sink or `gmail` inbox names to be a key of `google.accounts` (D138).
- `example.yaml`: `accounts: { owner: owner@example.com, invoices_a: invoices@example.com }`.
- Tests: R-6.5 the example loads; R-6.5 a sink naming an account missing from `google.accounts` is refused; R-6.5 an address without `@` is refused.

Test: `uv run pytest -q tests/unit/test_config_models.py && uv run python scripts/check_config.py config/example.yaml`.

## Step 21: `auth-google` refuses the wrong Google user `[worker]`

Files: `src/invoicebot/auth/providers.py`, `src/invoicebot/cli.py`, `tests/integration/test_cli_auth_google.py`.

- `google_scopes_for` adds `openid` and `email` to every account's scopes.
- `cli.py`: after the code exchange, decode the `id_token` payload (base64url, the middle part; no signature check, D138), require `email_verified is True` and `email.casefold() == cfg.google.accounts[account]`; otherwise print a refusal naming the expected and the signed-in address and save nothing (exit non-zero). A missing or malformed `id_token` is refused the same way.
- Tests: R-6.5 a matching email saves the token; R-6.5 another email saves nothing and exits non-zero; R-6.5 `email_verified: false` is refused; R-6.5 the consent URL asks for `openid email` besides the data scopes.

Test: `uv run pytest -q tests/integration/test_cli_auth_google.py tests/unit/test_auth_google_consent.py`.

## Step 22: The loopback listener waits for its own `state` `[worker]`

Files: `src/invoicebot/cli.py`, `tests/integration/test_cli_auth_google.py`.

- The listener checks a request's `state` with `hmac.compare_digest` before resolving the future; a request with another `state` (or none) gets a plain 400 page and the listener keeps waiting until the existing timeout. An `error` redirect carrying the right `state` still ends the sign-in.
- Tests: R-9.9 a request with a wrong `state` followed by the real redirect completes the sign-in; R-9.9 only wrong-state requests end in the timeout, not a consent error.

Test: `uv run pytest -q tests/integration/test_cli_auth_google.py`.

## Step 23: Detect a rewritten Google token file by more than its mtime `[worker:low]`

Files: `src/invoicebot/auth/google_oauth.py`, `tests/unit/test_auth_google_oauth.py`.

- Record `(st_ino, st_mtime_ns, st_size)` instead of `st_mtime_ns`; the file counts as changed when any differs.
- Tests: R-4.3 a file replaced by a new one (new inode) with the same mtime is detected; R-4.3 the existing mtime tests still pass.

Test: `uv run pytest -q tests/unit/test_auth_google_oauth.py`.

## Step 24: Test that Chromium sends loopback through the guard `[worker]`

Files: `tests/integration/test_browser_proxy.py`.

- New test: launch Chromium with `launch_options(...)` as the existing fixture does, open `chrome://version`, read the `#command_line` text, and assert it contains `--proxy-bypass-list=<-loopback>` and the guard's `--proxy-server`. This fails loudly if a Playwright upgrade stops adding the flag (R-9.10).

Test: `uv run pytest -q tests/integration/test_browser_proxy.py`.

## Step 25: Store queries for the gap commands `[worker]`

Files: `src/invoicebot/store/gaps.py`, `tests/integration/test_store_gaps.py`.

- `open_gaps(conn, limit) -> list[GapRow]`: state `open` or `reported`, newest `last_seen` first. `ignore_gap(conn, fingerprint) -> bool`: sets `state = 'ignored'` for a gap that is not already `ignored`, returns whether a row changed. Check that `upsert_gap` keeps `ignored` on a later sighting (it does not reset state); add a test.
- Tests: S8.9 `open_gaps` skips ignored and fixed gaps and orders by `last_seen`; S8.9 `ignore_gap` returns True once, then False; S8.9 a sighting after ignoring keeps the gap `ignored`.

Test: `uv run pytest -q tests/integration/test_store_gaps.py`.

## Step 26: `/gaps` and `/gap ignorar` `[worker]`

Files: `src/invoicebot/pipeline/commands.py`, `tests/integration/test_commands.py`.

- `Command` gains `admin_only: bool = False`. `handle_command` answers a non-admin's admin command exactly as an unknown command; `help_text(is_admin)` lists admin commands only for admins (update `_ayuda`).
- `gaps` (admin-only): lists `open_gaps(conn, 20)` with the step 1 texts, `…` when 20 came back; `gap` (admin-only): `ignorar <fingerprint>` calls `ignore_gap` and answers `GAP_IGNORED` or `GAP_UNKNOWN`; any other argument answers the help line.
- Tests: S8.9 an admin's `/gaps` lists two open gaps with their issue links; S8.9 `/gaps` with none answers `GAPS_EMPTY`; S8.9 `/gap ignorar <fp>` ignores it and a second `/gaps` omits it; S8.9 a non-admin's `/gaps` gets the unknown-command answer; S8.9 `/ayuda` shows the gap lines to an admin only.

Test: `uv run pytest -q tests/integration/test_commands.py`.

## Step 27: Daily GitHub App health check `[worker]`

Files: `src/invoicebot/app.py`, `tests/unit/test_app_wiring.py`, `tests/integration/test_ops_daily.py`.

- `_daily_tasks` gains a `github_health` task (key `github_health`, at `cfg.gaps.digest_at`) when GitHub is configured: `app.invalidate()`, then `await app.token()`; on `GitHubTokenError` call `_alert_token_error(pool, cfg, exc.reason)` (BR-D05, already rate-limited daily). No task without GitHub.
- Tests: BR-D05 with GitHub configured the task list has `github_health`; BR-D05 without it the list does not; BR-D05 a token failure queues one BR-D05 admin alert and a success queues none (fake `GitHubApp`).

Test: `uv run pytest -q tests/unit/test_app_wiring.py tests/integration/test_ops_daily.py`.

## Step 28: Close the leftovers `[inline: roadmap text]`

Files: `docs/plan-mvp.md`.

- Strike the leftovers this plan closed (with "Done in `ops-harden`"), and note in row 9a what landed.

Test: `uv run python scripts/check_fixtures.py`.
