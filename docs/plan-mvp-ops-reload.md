# Plan: MVP phase 9e · ops-reload

Roadmap: `docs/plan-mvp.md` row 9e (D136, D154). It runs while Meta verification is pending, before 9d2 `live-chat`. Backlog: S8.7 (config reload and gap auto-resume), S10.8's `SIGHUP` reload, S12.1 (memory and latency report in CI), S12.4 (housekeeping), S12.5 (runbook), plus the `ops-reload` items the roadmap carries from the `ops-live` backup and review.

**Goal:**

- A `SIGHUP` validates the new config. A bad one keeps the bot running on the old config and alerts the admins (BR-O05). A good one restarts the process cleanly, so the new config is in force.
- At every start, open gaps are re-checked. A fixed gap re-enqueues its stuck invoices and tells their senders (BR-F12). The gap is closed, with its GitHub issue, once one of them gets past the failing step.
- Housekeeping keeps the disk bounded: stale temp folders and dead jobs go, and the padrón refreshes itself weekly.
- CI fails when 10 queued invoices push the peak memory over 800 MB or miss the latency targets.
- The JSON log carries `extra=` fields, a good backup logs one line, and neither helper can read `/data` beyond what it is handed.
- The owner has a runbook.

**Needs from the owner first:** nothing. Step 21 runs on the live host; it does not need Meta.

**Not in this plan:** the `ops-live` "Later" items (wrong-mailbox `TokenProvider`, the restore drill's key list) and the other "Later" rows stay in the roadmap. No `gap.closed` event: the events contract is v1.1 (E13).

## Design choices

Step 1 writes these into spec 05. They are proposals for the owner's review of this plan.

- **D155 · A good config reload restarts the process; a bad one is refused.**
  - Startup already builds every adapter from the config, so swapping the config inside a running process would mean rebuilding the whole runtime, including the webhook router. A restart does the same with code that already exists.
  - On `SIGHUP` the app loads the candidate config with the same function startup uses: `load_config`, `load_directory`, `load_rules`, `compile_rules`, then `run_checks`, refusing any blocking problem. Startup gains that `run_checks` call too, so the two can never disagree.
  - If the candidate fails, the app logs `config reload rejected` with a reason code, queues BR-O05 to the admins and keeps running. If it passes, the app logs `config reload accepted` and sends itself `SIGTERM`. Uvicorn shuts down gracefully, and the container's `restart: always` (live) or `unless-stopped` (dev) starts it again on the new config. Jobs in flight keep their leases and are re-claimed after `JOB_LEASE_S`, as after any restart. While the app is down, Meta retries the webhook.
  - This reads R-9.6's "swap it in" as "restart into it". Secrets are re-read on the same restart, which also makes a secret rotation a `SIGHUP`.
- **D156 · Gaps are re-checked at every start, not only after a `SIGHUP`.**
  - Under D155 every reload is a start. An image deploy is a start too, and it can fix a gap just as well. So `_runtime` runs `reevaluate()` once, after the wiring and before the background tasks.
- **D157 · A gap counts as fixed only when the config it failed on has changed.**
  - Without that, the first restart after BR-F03 would "fix" a broken portal whose entry still says `active`, and a TEMPLATE_INVALID would be retried on every restart.
  - A new column, `gaps.raised_with`, holds a digest of the config entry the fix must change: the portal entry for PORTAL_BROKEN and the flow's sink entry for TEMPLATE_INVALID. It is null for PORTAL_MISSING, whose fix is the new store entry itself. Each sighting stores the current digest.
  - Fixed when: PORTAL_MISSING, the RUC has a store whose portal is `active`. PORTAL_BROKEN, the portal is `active` and its digest differs from `raised_with`. TEMPLATE_INVALID, the sink's digest differs from `raised_with`. PARSER_MISS and UNROUTED_BUYER never auto-resume (spec 06 §3).
  - A fixed gap's linked invoices in `MANUAL_REQUIRED` are re-enqueued: the portal kinds to `FETCHING_DOCS`, TEMPLATE_INVALID to `DELIVERING`. Each sender gets BR-F12. With none left to re-enqueue, the gap is closed at once.
  - When a re-enqueued invoice gets past the failing step, the gap is `closed` and its issue gets a comment and is closed. If it fails at the same step again, the sighting moves the gap back to `reported` (or `open` without an issue). `raised_at` stays set, so there is no second issue or alert.
- **D158 · The extract helper reads its input from stdin and sees none of `/data`.**
  - The `ops-live` review assumed the helper already reads stdin. It does not: it opens the photo by path (`extract/__main__.py`). Binding a single file back, the way D150 binds the browser's folder, needs a staging mount the image wrapper does not have.
  - So the pipeline sends the file's bytes on stdin (capped by the media limit it already checks), and the image wrapper masks the whole data root read-only, like the browser wrapper with no `HELPER_OUT_DIR`.
- **D159 · Housekeeping is one daily task, and the padrón refresh is weekly.**
  - The `housekeeping` task runs at the retention time. It removes `<data_dir>/tmp/<invoice_id>` folders whose invoice is terminal or unknown, and deletes jobs whose invoice is terminal and whose lease has expired.
  - The padrón refresh is a daily task that acts only on the weekday of `padron.refresh_cron`. The cron is restricted to the form `M H * * D` and checked when the config loads. The refresh downloads `padron.url` (default `https://www2.sunat.gob.pe/padron_reducido_ruc.zip`, checked in step 21) into `<data_dir>/padron/`, capped at `padron.max_mb` (default 600). It runs `import_padron` and deletes the ZIP whatever the outcome. `scripts/import_padron.py --download` runs the same refresh once, by hand.
- **D160 · The memory and latency report is an in-process load run in CI.**
  - `scripts/memprofile.py` pushes 10 synthetic invoices through the real pipeline (fake channel, real Postgres, real helpers). It reports peak memory as the process's own max RSS plus the largest helper's, and, per invoice, the time to the acknowledgement and to the summary.
  - It exits 1 when the peak is over 800 MB (R-9.1), an acknowledgement takes 5 s or more, or a summary takes 60 s or more (R-9.2). CI runners have more than one vCPU, so the 1-vCPU number for R-9.2 comes from the live host (step 21).

## Step 1: Record D155–D160, BR-O05 and BR-F12 `[inline: spec text and user-facing copy the owner reviews with this plan]`

Files: `specs/05-open-questions.md`, `specs/06-business-rules.md` (the spec 06 file), `docs/plan-mvp.md`, `src/invoicebot/rules/catalog.py`, `src/invoicebot/rules/messages_es.py`, `src/invoicebot/channels/whatsapp/transport.py`.

- Spec 05: rows D155–D160 with the text of "Design choices" above, each marked "(9e plan, proposed)".
- Spec 06:
  - BR-O05: "A `SIGHUP` reload found an invalid config" | "Alert, admins only; the bot keeps the old config" | `⚙️ La nueva configuración no es válida; sigo con la anterior ({reason}).`
  - BR-F12: "A fixed gap re-enqueued an invoice (R-11.9)" | "`MANUAL_REQUIRED` → `FETCHING_DOCS` or `DELIVERING`" | `🔄 Ya puedo continuar con tu comprobante #{id}; lo estoy procesando de nuevo.`
  - §3, the PORTAL_BROKEN "Auto-resume when" cell: "The entry is `active` and has changed since the last failure (D157)".
- Catalog and messages: both rules, in the shape of their neighbors. `BR-O05` is an admin rule, so it maps to the `admin_alert` template like the other `*_admin` rules. `BR-F12` maps to the `resume_notice` template in `transport.py`, next to `"BR-F07": "manual_required"`.
- Roadmap row 9e: plan `docs/plan-mvp-ops-reload.md`, D155–D160.

Test: `uv run pytest -q tests/unit/test_rules_catalog.py` (the parity test; use the file that holds it) and `npx prettier --check docs/plan-mvp.md specs/05-open-questions.md`.

## Step 2: The JSON log renders `extra=` fields `[worker]`

Files: `src/invoicebot/logging.py`, `tests/unit/test_logging.py`.

- `JsonFormatter.format`: every record attribute that is not a standard `logging.LogRecord` attribute (compare against the attribute names of a blank `LogRecord`, computed once into a module constant) and not already one of the formatter's own fields is added to the JSON object under its own key. A value that is not `str`, `int`, `float`, `bool` or `None` is rendered with `str()`. String values go through `_redact_value` like the message.
- An `extra` key that collides with a formatter field never overwrites it.

Tests (docstrings start with `R-9.5`):

1. `logger.warning("backup failed", extra={"reason": "dump"})` gives a line whose JSON has `"reason": "dump"`.
2. An `extra` value holding a configured secret value comes out redacted.
3. `extra={"count": 3}` keeps `3` as a number.
4. An `extra` key equal to a formatter field (e.g. `"message"`) does not replace it.

Test: `uv run pytest -q tests/unit/test_logging.py`.

## Step 3: A good backup logs `backup uploaded` `[worker]`

Files: `src/invoicebot/ops/backup.py`, `src/invoicebot/app.py`, `tests/unit/test_ops_backup.py` (the unit test file that covers `run_backup`; create it if none exists).

- `run_backup` returns a frozen dataclass `BackupSizes(dump_bytes: int, config_bytes: int)`: the sizes of the two encrypted artifacts it uploaded. Update its docstring.
- `app._run_backup` logs `logger.info("backup uploaded", extra={"dump_bytes": ..., "config_bytes": ...})` after a successful run.

Tests (docstrings start with `R-9.11`): `run_backup` with a fake drive returns the sizes of the two uploaded payloads. Adjust any existing test that ignores the return value only where the type changes.

Test: `uv run pytest -q tests/unit/test_ops_backup.py tests/integration/test_app_backup_alert.py`.

## Step 4: The browser wrapper refuses `..` in `HELPER_OUT_DIR` `[worker]`

Files: `src/invoicebot/sandbox.py`, `src/invoicebot/fetchers/browser/session.py` only if it builds `HELPER_OUT_DIR`, `tests/unit/test_sandbox.py`, `tests/deploy/sandbox_checks.py`.

- `BROWSER_SCRIPT`: before the `case` on the data root, refuse an out dir that contains a `..` path component: `case "/$out/" in */../*) echo "out dir has .." >&2; exit 1 ;; esac`.
- `browser_prefix` (and whatever Python sets `HELPER_OUT_DIR`): raise `ValueError` when the data root or the out dir has a `..` part (`".." in path.parts`).
- `sandbox_checks.py`: one more check, `HELPER_OUT_DIR=$data/../x` makes the wrapper exit non-zero with `out dir has ..`.

Tests (docstrings start with `D150`): `browser_prefix(..., data_dir=Path("/data/../etc"))` raises `ValueError`; an out dir with `..` is refused by the Python side.

Test: `uv run pytest -q tests/unit/test_sandbox.py`, then `bash tests/deploy/test_helper_sandbox.sh` if Podman is available (say so if it is not).

## Step 5: The extract helper reads its input from stdin `[worker]`

Files: `src/invoicebot/extract/__main__.py`, `src/invoicebot/pipeline/extract.py`, `tests/unit/test_extract_main.py` (the unit file for the helper's `main`; use the existing one).

- The helper's argv drops the input path. `main` reads at most `max_bytes + 1` bytes from `sys.stdin.buffer`. More than `max_bytes` gives the same "unreadable" result as the size check today. The image and PDF readers take the bytes: `cv2.imdecode` for images, `pypdfium2.PdfDocument(bytes)` for PDFs. Keep every other argument in its position minus the path.
- `pipeline/extract._run_helper` reads the file in `asyncio.to_thread` (it already knows the size from the media limit) and passes the bytes as `run_helper`'s `stdin`.
- Docstrings say the helper never opens a path under the data root (D158).

Tests (docstrings start with `D158`):

1. `main` with a synthetic QR photo on stdin prints the same payload as before for that photo.
2. Input of `max_bytes + 1` bytes prints the unreadable payload with exit 0.
3. A synthetic PDF on stdin is read.

Test: `uv run pytest -q tests/unit/test_extract_main.py tests/integration/test_pipeline_extract.py` (use the files that cover the helper and the extract handler).

## Step 6: The image wrapper masks the data root `[worker]`

Files: `src/invoicebot/sandbox.py`, `src/invoicebot/app.py`, `tests/unit/test_sandbox.py`, `tests/deploy/sandbox_checks.py`.

- `IMAGE_SCRIPT` takes the data root as its first argument and, before the hidden dirs, mounts `tmpfs -o size=4k,mode=0555` over it, then `mount -o remount,bind,ro` on it, as `BROWSER_SCRIPT` does without an out dir.
- `image_prefix(hidden_dirs, data_dir)` passes it; `helper_prefixes` passes `data_dir` to both prefixes. Docstrings cite D158.
- `sandbox_checks.py`: inside the image prefix, listing the data root shows nothing, and the helper still extracts a synthetic photo given on stdin.

Tests (docstrings start with `D158`): `image_prefix` puts the data root right after the script; `helper_prefixes(enabled=True, ...)` builds both prefixes with it.

Test: `uv run pytest -q tests/unit/test_sandbox.py`, then `bash tests/deploy/test_helper_sandbox.sh` if Podman is available.

## Step 7: Gaps remember the config they failed on `[worker]`

Files: `migrations/versions/0012_gap_raised_with.py`, `src/invoicebot/store/gaps.py`, `tests/integration/test_store_gaps.py`.

- Migration (expand only, plain SQL through `op.execute`): `ALTER TABLE gaps ADD COLUMN raised_with TEXT`. Downgrade drops it.
- `upsert_gap` gains a keyword `raised_with: str | None`, written on insert and on every update.
- `upsert_gap` on a `fixed` gap moves it to `reported` when it has an `issue_url`, else to `open`. `raised_at` is left as it is, so the gap is not raised a second time.
- New store functions, none of which commits:
  - `fixable_gaps(conn) -> list[GapRow]`: gaps in `open` or `reported` of kinds `PORTAL_MISSING`, `PORTAL_BROKEN` and `TEMPLATE_INVALID`, locked `FOR UPDATE`. `GapRow` gains `raised_with`.
  - `mark_fixed(conn, fingerprint)` and `mark_closed(conn, fingerprint)`: set the state.
  - `stuck_invoices(conn, fingerprint) -> list[int]`: the ids in `invoice_gaps` for that gap whose invoice is in `MANUAL_REQUIRED`, locked `FOR UPDATE OF invoices`.

Tests (integration; docstrings start with `D157` or `R-11.9`):

1. `raised_with` is stored on insert and replaced by the next sighting.
2. A sighting on a `fixed` gap with an issue moves it to `reported`, without an issue, to `open`; `raised_now` is false in both.
3. `fixable_gaps` returns only the three kinds and only `open`/`reported`.
4. `stuck_invoices` returns the linked `MANUAL_REQUIRED` invoice and skips a linked `DONE` one.

Test: `uv run pytest -q tests/integration/test_store_gaps.py`.

## Step 8: Sightings carry the config digest `[worker]`

Files: `src/invoicebot/config/digest.py` (new), `src/invoicebot/pipeline/gaps.py`, `src/invoicebot/pipeline/fetch.py`, `src/invoicebot/pipeline/deliver.py`, `tests/unit/test_config_digest.py`, `tests/integration/test_pipeline_gaps.py`.

- `config/digest.py`: `config_digest(model: BaseModel) -> str`, the SHA-256 hex of `model.model_dump_json()` with sorted keys (dump to a dict, then `json.dumps(..., sort_keys=True, separators=(",", ":"))`). Module docstring: "The digest a gap stores to tell whether its config entry changed (D157)."
- `GapSighting` gains `raised_with: str | None = None`; `raise_gap` passes it to `upsert_gap`.
- PORTAL_BROKEN sightings (`fetch.py`) carry the digest of the portal entry; TEMPLATE_INVALID sightings (`deliver.py`) carry the digest of the failing sink entry. PORTAL_MISSING passes `None`.

Tests:

1. Unit (`D157`): the digest is stable for equal models, differs when one field changes, and ignores dict key order.
2. Integration (`D157`): raising a PORTAL_BROKEN gap stores the portal's digest in `gaps.raised_with`.

Test: `uv run pytest -q tests/unit/test_config_digest.py tests/integration/test_pipeline_gaps.py`.

## Step 9: One home for re-enqueuing a stuck invoice `[worker]`

Files: `src/invoicebot/pipeline/resume.py` (new), `src/invoicebot/pipeline/commands.py`, `tests/integration/test_commands.py`.

- Move `_retry_documents` and `_retry_delivery` from `commands.py` into `pipeline/resume.py` as `retry_documents` and `retry_delivery`, unchanged. Add `resume_delivery(conn, invoice_id) -> None`: `set_state(conn, invoice_id, DELIVERING, expected=MANUAL_REQUIRED)` and `enqueue(conn, invoice_id, DELIVER_STEP)`.
- `commands.py` imports them. Behavior of `/reprocesar` does not change.
- Module docstring: "Send a stuck invoice back to the step that failed (S4.9, R-11.9)."

Tests: the existing `/reprocesar` tests pass unchanged. Add one (`R-11.9`): `resume_delivery` moves a `MANUAL_REQUIRED` invoice to `DELIVERING` with one `deliver` job.

Test: `uv run pytest -q tests/integration/test_commands.py`.

## Step 10: `reevaluate()` fixes gaps and resumes their invoices `[worker:high: reasoning: per-kind fix rules against the loaded directory and config, row locks and the close-when-none-left case in one transaction]`

Files: `src/invoicebot/pipeline/reevaluate.py` (new), `src/invoicebot/pipeline/gaps.py`, `tests/integration/test_pipeline_reevaluate.py` (new).

- `pipeline/gaps.close_gap(conn, gap)`: `mark_closed`, and when the gap has an `issue_url`, queue a GitHub outbox row `{"op": "close_issue", "issue_url": ...}` the way `_queue_alert` queues `create_issue`.

- `async def reevaluate(pool, cfg, directory) -> int`, one transaction; returns the number of gaps it fixed and logs `gaps reevaluated` with `fixed` and `resumed` counts once.
- For each row of `fixable_gaps`, fixed per D157:
  - PORTAL_MISSING `portal_missing:<ruc>`: `directory` has a store with that RUC and its portal's `status == "active"`.
  - PORTAL_BROKEN `portal_broken:<key>`: the portal exists, is `active`, and `config_digest(portal) != gap.raised_with`.
  - TEMPLATE_INVALID `template_invalid:<flow>:<sink>`: the flow and sink still exist and `config_digest(sink) != gap.raised_with`. A sink that no longer exists is not fixed.
- For a fixed gap: `mark_fixed`, then for each id in `stuck_invoices`: the portal kinds call `retry_documents(conn, id, MANUAL_REQUIRED)`, TEMPLATE_INVALID calls `resume_delivery`; then queue BR-F12 to the invoice's sender through the outbox, the way BR-F07 is queued in `fetch.py`, with `{id}`.
- A fixed gap with no stuck invoice is closed at once through `close_gap`.
- Fingerprint parts are split on `:` with the prefixes from `gaps/kinds.py`; no new string literals for kinds.

Tests (integration, real Postgres; docstrings start with `R-11.9` or `D157`):

1. PORTAL_MISSING gap, invoice in `MANUAL_REQUIRED`; the directory has the RUC with an `active` portal: gap `fixed`, invoice `FETCHING_DOCS` with one `fetch` job, one BR-F12 outbox row to its sender.
2. Same, but the portal is `todo`: nothing changes.
3. PORTAL_BROKEN with `raised_with` equal to the current digest: not fixed. With a different digest and `active`: fixed and the invoice resumed.
4. TEMPLATE_INVALID with a changed sink: invoice `DELIVERING` with one `deliver` job, BR-F12 queued.
5. A fixed gap with an issue whose only linked invoice is `DONE`: the gap is `closed` and one `close_issue` row is queued.
6. PARSER_MISS and UNROUTED_BUYER gaps are untouched.
7. An `ignored` gap is untouched.

Test: `uv run pytest -q tests/integration/test_pipeline_reevaluate.py`.

## Step 11: A fixed gap closes once an invoice gets past the step `[worker]`

Files: `src/invoicebot/pipeline/gaps.py`, `src/invoicebot/pipeline/fetch.py`, `src/invoicebot/pipeline/deliver.py`, `src/invoicebot/pipeline/deliver_google.py`, `tests/integration/test_pipeline_gaps.py`.

- `pipeline/gaps.close_if_fixed(conn, invoice_id, kind)`: when `linked_gap(conn, invoice_id, kind)` is `fixed`, call `close_gap` (step 10).
- Call it in the same transaction as the success: after a successful fetch with kinds PORTAL_MISSING and PORTAL_BROKEN, and after a successful delivery with TEMPLATE_INVALID.

Tests (integration; docstrings start with `R-11.9`):

1. A `fixed` PORTAL_MISSING gap with an issue: the invoice's fetch succeeds, the gap is `closed` and one `close_issue` row is queued.
2. A `fixed` gap without an issue closes with no GitHub row.
3. A `reported` gap is not closed by a success.

Test: `uv run pytest -q tests/integration/test_pipeline_gaps.py`.

## Step 12: The GitHub transport closes issues `[worker]`

Files: `src/invoicebot/github/transport.py`, `src/invoicebot/github/issue_text.py`, `tests/unit/test_github_transport.py` (the existing transport test file).

- New op `close_issue`: take the issue number from `issue_url` (the last path segment, digits only; anything else is a permanent send error), `POST` a comment with the constant `CLOSE_COMMENT` from `issue_text.py` ("Fixed and verified by the bot: an affected invoice got past the failing step."), then `PATCH` the issue with `{"state": "closed"}`. Each request counts against the daily write budget and gets the same `SendLater` and error handling as `comment`.
- The op check that today accepts `create_issue` and `comment` accepts `close_issue` too.

Tests with `httpx.MockTransport` (docstrings start with `R-11.9`):

1. A `close_issue` row sends the comment and then the `PATCH` with `state: closed` to the right number.
2. An `issue_url` whose last segment is not digits raises the permanent error and sends nothing.
3. A 5xx on the `PATCH` reschedules the row like the other ops.

Test: `uv run pytest -q tests/unit/test_github_transport.py`.

## Step 13: One loader for startup and reload `[worker]`

Files: `src/invoicebot/runtime_config.py` (new), `src/invoicebot/app.py`, `tests/unit/test_runtime_config.py` (new), `.importlinter` if a contract needs the new module named.

- `RuntimeConfig`: frozen dataclass with `cfg`, `directory`, `rules` (the compiled routing rules).
- `ConfigRejected(Exception)` with a `reason: str`, a short code and never a value: `yaml`, `validation`, `parsing_rules`, `routing`, or the first blocking `Problem.code`.
- `load_runtime_config(settings) -> RuntimeConfig`: `load_config`, `load_directory`, `load_rules`, `compile_rules`, then `run_checks(cfg, directory, changed_portals=frozenset())`; any blocking problem raises `ConfigRejected`. Each loader's own exceptions are wrapped with the matching reason code (`raise ConfigRejected("yaml") from err`).
- `_runtime` uses it in place of the four loader calls and logs a startup rejection's reason before re-raising.

Tests (unit, `R-9.6`): a valid example config loads; a YAML syntax error gives `yaml`; a pydantic error gives `validation`; a blocking `run_checks` problem gives its code; non-blocking problems pass. Use temp copies of `config/example.yaml` and the store directory fixtures the config tests already use.

Test: `uv run pytest -q tests/unit/test_runtime_config.py`, then `uv run lint-imports`.

## Step 14: `SIGHUP` reloads or alerts `[worker]`

Files: `src/invoicebot/reload.py` (new), `src/invoicebot/app.py`, `tests/unit/test_reload.py` (new), `tests/integration/test_app_reload.py` (new).

- `reload.py`: `async def reload_config(load, on_rejected, restart) -> bool`. It runs `load()` in `asyncio.to_thread`. On `ConfigRejected` it logs `config reload rejected` with `extra={"reason": ...}`, awaits `on_rejected(reason)` and returns False. Otherwise it logs `config reload accepted`, calls `restart()` and returns True. Module docstring cites R-9.6 and D155.
- `app.py`:
  - Remove `_log_config_reload_request` and its install in `lifespan`.
  - In `_runtime`, after the wiring and before `_background`, install the `SIGHUP` handler (same try/except for platforms without signals; removed on exit). The handler starts one `reload_config` task, kept in a set with a done-callback, and ignores a `SIGHUP` while one is running.
  - `load` is `partial(load_runtime_config, settings)`. `on_rejected` queues BR-O05 to every admin through the outbox, rate-limited like BR-O04 (`_alert_backup_failure`), with `{reason}`. `restart` is `partial(os.kill, os.getpid(), signal.SIGTERM)`.
- Still in `_runtime`, right after the wiring: `await reevaluate(pool, cfg, directory)` (D156).
- `deploy/README.md`: the reload paragraph says a good config restarts the app and a bad one is refused with BR-O05 (D155).

Tests:

1. Unit (`R-9.6`): `reload_config` with a loader that raises `ConfigRejected("yaml")` calls `on_rejected("yaml")`, not `restart`. A loader that returns calls `restart` once, not `on_rejected`.
2. Integration (`R-9.6`, `BR-O05`): `on_rejected` queues one BR-O05 row per admin; a second rejection within the rate limit queues none.

Test: `uv run pytest -q tests/unit/test_reload.py tests/integration/test_app_reload.py`.

## Step 15: A deploy that adds a portal resumes the invoice `[worker]`

Files: `tests/integration/test_gap_resume_flow.py` (new).

The S8.7 end-to-end test, with the fake channel and real Postgres (docstring starts with `R-11.9`):

1. An invoice reaches `MANUAL_REQUIRED` with a PORTAL_MISSING gap reported with an issue URL.
2. The store directory gains the RUC with an `active` portal (temp copy of the fixture directory). `reevaluate` runs: the invoice is `FETCHING_DOCS` and the sender has a BR-F12 row.
3. The fetch job runs against an `httpx.MockTransport` portal that returns the XML: the invoice moves past `FETCHING_DOCS`, the gap is `closed`, and a `close_issue` row is queued.

Test: `uv run pytest -q tests/integration/test_gap_resume_flow.py`.

## Step 16: Daily housekeeping `[worker]`

Files: `src/invoicebot/ops/housekeeping.py` (new), `src/invoicebot/store/retention.py`, `src/invoicebot/app.py`, `tests/integration/test_ops_housekeeping.py` (new).

- `store/retention.delete_stale_jobs(conn) -> int`: `DELETE FROM jobs USING invoices WHERE jobs.invoice_id = invoices.id AND invoices.state = ANY(%s) AND (jobs.lease_until IS NULL OR jobs.lease_until < now())` with `TERMINAL`.
- `ops/housekeeping.py`: `async def housekeep(pool, data_dir) -> None`. It deletes stale jobs in one transaction, then lists `<data_dir>/tmp/` (`TMP_DIRECTORY`): each entry whose name is an invoice id that is terminal or not in `invoices` is removed with `shutil.rmtree` in `asyncio.to_thread`. A name that is not digits is left alone. It logs `housekeeping done` once, with `jobs` and `tmp_dirs` counts.
- `app._daily_tasks` gains `DailyTask(key="housekeeping", at=retention_at, run=...)`.

Tests (integration; docstrings start with `R-9.7` or `S12.4`):

1. A `tmp/<id>` folder of a `DONE` invoice is removed; one of a `FETCHING_DOCS` invoice stays; one of an unknown id is removed; a non-digit name stays.
2. A job of a `CANCELLED` invoice with an expired lease is deleted; one with a live lease and one of an active invoice stay.

Test: `uv run pytest -q tests/integration/test_ops_housekeeping.py`.

## Step 17: The padrón refresh settings `[worker]`

Files: `src/invoicebot/config/models.py`, `config/example.yaml`, `tests/unit/test_config_models.py` (the existing config model tests).

- `Padron` gains `url: HttpUrl = "https://www2.sunat.gob.pe/padron_reducido_ruc.zip"` and `max_mb: int = Field(600, gt=0)`.
- A validator on `refresh_cron` accepts only `M H * * D` (minute 0–59, hour 0–23, weekday 0–6, Sunday 0) and exposes the parsed values as properties `at: time` and `weekday: int` (Python's Monday-0 weekday, converted).
- `example.yaml` keeps `refresh_cron` and comments the two new keys with their defaults.

Tests (unit, `S12.4`): `"0 4 * * 0"` parses to 04:00 and Sunday; `"*/5 * * * *"`, `"0 4 1 * *"` and `"0 25 * * 0"` are rejected; the defaults load.

Test: `uv run pytest -q tests/unit/test_config_models.py`.

## Step 18: The weekly padrón refresh `[worker]`

Files: `src/invoicebot/ops/padron_refresh.py` (new), `src/invoicebot/app.py`, `scripts/import_padron.py`, `tests/integration/test_ops_padron_refresh.py` (new).

- `async def refresh_padron(pool, http, padron_cfg, data_dir) -> ImportResult`: stream `padron_cfg.url` with the guarded HTTP client into `<data_dir>/padron/padron_reducido_ruc.zip`, aborting past `max_mb` MiB; run `import_padron` on a pool connection; delete the ZIP in `finally`. Log `padron refreshed` with the row and skipped counts. A non-200 answer or the size cap raises `PadronRefreshError(reason)`.
- `app._daily_tasks` gains `DailyTask(key="padron", at=cfg.padron.at, run=...)` whose callable returns at once unless today's owner-local weekday (from the database clock, like `_owner_today`) is `cfg.padron.weekday`. A `PadronRefreshError` is logged as a warning with its reason, never raised.
- `scripts/import_padron.py --download` runs `refresh_padron` once with the config, settings and a guarded client, and prints the counts. The local-ZIP form is unchanged.

Tests (integration, `httpx.MockTransport`; docstrings start with `S12.4`):

1. A mini padrón ZIP (built in the test, as the S3.4 tests do) is downloaded and imported, and the ZIP is gone afterwards.
2. A body over `max_mb` (use a tiny cap) raises `PadronRefreshError("size")` and leaves no file.
3. A 404 raises `PadronRefreshError("status")` and the old `padron` rows remain.

Test: `uv run pytest -q tests/integration/test_ops_padron_refresh.py`.

## Step 19: The memory and latency report `[worker]`

Files: `scripts/memprofile.py` (new), `tests/integration/test_memprofile.py` (new), `.github/workflows/ci.yml`.

- `memprofile.py` (D160) builds the pipeline in-process the way the integration tests do: fake channel, real Postgres from `DATABASE_URL`, real handlers and helpers. It posts 10 synthetic invoices (the synthetic photos and PDFs under `tests/`, never the private samples) through the webhook route, then runs the worker until all 10 have their summary in the outbox or 180 s pass.
- It measures, per invoice, the seconds from the webhook post to the acknowledgement's outbox row and to the summary's outbox row (the rule ids the pipeline already uses for both). Peak memory is `getrusage(RUSAGE_SELF).ru_maxrss + getrusage(RUSAGE_CHILDREN).ru_maxrss`, in MB.
- It prints one JSON object (`peak_mb`, `ack_s_max`, `summary_s_max`, `invoices`) and, when `GITHUB_STEP_SUMMARY` is set, appends a Markdown table to it. Exit 1 when `peak_mb > 800`, `ack_s_max >= 5`, `summary_s_max >= 60`, or an invoice has no summary.
- `ci.yml`: a `memprofile` step in the `ci` job after the tests, `uv run python scripts/memprofile.py`.
- Test (integration, `R-9.1`): run the script's `main` with 2 invoices against the test database; it returns 0 and the JSON has both invoices.

Test: `uv run pytest -q tests/integration/test_memprofile.py`.

## Step 20: The runbook `[inline: owner-facing text]`

Files: `docs/runbook.md` (new), `deploy/README.md` (a link to it).

One section per S12.5 task, each a short numbered procedure with the exact commands for the live host (`deploy/deploy-live.sh`, `podman`, `scripts/*`), linking to `deploy/README.md` instead of repeating it: re-authorize Google and Microsoft tokens (`invoicebot auth-google`, `auth-microsoft`); add a portal and store (files, `check_config.py`, deploy, what D155–D157 then do); set a portal to `broken`; rotate secrets and the GitHub App key (`scripts/secrets_edit.py`, then `SIGHUP`); handle a gap issue and `/gap ignorar`; restore a backup (`scripts/restore_drill.sh`); Postgres down (webhook 503, Meta retries, R-9.15); refresh the padrón by hand (`import_padron.py --download`).

Test: `npx prettier --check docs/runbook.md deploy/README.md`.

## Step 21: Live check on this host `[inline: runs on the live host]`

No code. On the live stack (`invoicebot-live`), after deploying this branch's image with `deploy/deploy-live.sh`:

1. A config with a YAML error through `deploy-live.sh --config`: the script's own check refuses it. Then send `SIGHUP` with a bad file put in place by hand: the log shows `config reload rejected`, the app keeps serving `/health`, and an outbox row for BR-O05 exists (it cannot reach WhatsApp until Meta approves).
2. A good config through `deploy-live.sh --config`: the log shows `config reload accepted`, the container restarts (`podman ps` shows a fresh start), and the startup log has `gaps reevaluated`.
3. `scripts/import_padron.py --download` inside the container: record the URL that worked, the ZIP size, the time and the peak memory. If the default URL fails, record the working one and fix the default in a follow-up commit.
4. Run `scripts/memprofile.py` once on this host against a scratch database, and record the numbers for R-9.1 and R-9.2.
5. The owner reviews `docs/runbook.md` (S12.5's acceptance).

Record the results in the roadmap row 9e.
