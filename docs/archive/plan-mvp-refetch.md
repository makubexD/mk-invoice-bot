# Plan: MVP phase 9n · refetch

Roadmap: `docs/plan-mvp.md` row 9n (D166, D167). The live trial found three fetch problems:

1. **A re-fetch is a no-op.** `list_attempts` returns every attempt the invoice ever had, so after `/reprocesar` (D96)
   or a fixed gap's resume (D157, R-11.9) `chain.decide` sees every fetcher used up and returns `Exhausted` at once.
   The sender gets BR-F12 and then the same BR-F07, and the gap never closes. Live invoice 12 is stuck this way.
2. **Technical failures are silent.** The email, QR and portal fetchers turn exceptions into `technical` or `timeout`
   and log nothing, so a Graph timeout (live invoice 9) can only be inferred from its duration.
3. **One technical failure ends a fetcher for good** (D65). A portal hiccup that clears minutes later still sends the
   invoice to `MANUAL_REQUIRED`.

Fixes: a fetch round (`invoices.fetch_round_at`) that each retry starts, so the chain reads only this round's attempts
while the full log stays for the per-portal stats (R-5.7); a `WARNING` with the exception class on each technical
branch; and one retry for a technical or timeout failure, after the fetcher's first scheduled delay.

**Needs from the owner first:** nothing for steps 1 to 7. Step 8 uses the live host and invoice 12.

## Step 1: D166, D167, roadmap row 9n, 9d2 outbox note `[inline: design decisions and spec wording]`

1. `specs/05-open-questions.md`, after D165, add two rows:
   - **D166** "A retry starts a new fetch round": `/reprocesar` (D96) and a fixed gap's resume (D157) stamp
     `invoices.fetch_round_at` with the database clock; the chain decision, the retry counts, the first-wait notice and
     BR-F07's list read only the attempts at or after it. Every attempt stays in `fetch_attempts` for R-5.7 and
     BR-F03's streak, which is per portal and ignores rounds. A new round may send the first-wait notice again.
   - **D167** "A technical failure gets one retry": a `technical` or `timeout` result waits the fetcher's first
     scheduled delay (`schedule[0]`, 1 h by default) and runs once more, with no message to the sender; a second one in
     the round is final and the chain moves on (D65). This bounds BR-F07's delay for a broken portal at one delay.
2. `specs/06-business-rules.md`, BR-F03: no change (the chain still continues after the final failure); confirm the
   wording does not say "first technical failure is final". If it does, point it at D167.
3. `docs/plan-mvp.md`: add row 9n `refetch` (plan: `docs/plan-mvp-refetch.md`, D166–D167) after 9m, before 9d2.
4. `docs/plan-mvp-live-chat.md`, step 1: add a first item: "Retire the stale WhatsApp outbox rows before Meta goes
   live: the `pending` and `dead` admin alerts queued while the token was restricted would otherwise all go out at
   once. Mark them `dead` with one `UPDATE outbox SET state = 'dead' WHERE channel = 'whatsapp' AND state =
'pending'`, after checking the count."

Test: `grep -n "D166\|D167" specs/05-open-questions.md && grep -n "9n" docs/plan-mvp.md && grep -n "stale WhatsApp"
docs/plan-mvp-live-chat.md` shows each line.

## Step 2: The fetch round in the schema and the store `[worker]`

Files: `migrations/versions/0014_fetch_round.py`, `src/invoicebot/store/invoices.py`,
`src/invoicebot/store/fetch_attempts.py`, `tests/integration/test_fetch_attempts_store.py`.

1. Migration `0014` (revises `0013`, same layout and docstring style as `0013_console_channel.py`):
   `ALTER TABLE invoices ADD COLUMN fetch_round_at TIMESTAMPTZ NULL`. Docstring: expand-only, the previous image
   never reads it (D166). `downgrade` drops the column.
2. `store/invoices.py`: `async def start_fetch_round(conn, invoice_id: int) -> None` sets `fetch_round_at = now()`
   for that invoice. Docstring: starts a new fetch round (D166); does not commit.
3. `store/fetch_attempts.py`, `list_attempts`: join `invoices` and keep only the rows with `fetch_attempts.at >=
invoices.fetch_round_at`, or every row when `fetch_round_at IS NULL`; same order and return type. Docstring: the
   current round's attempts (R-5.7, D166). `record_attempt` and `technical_streak` do not change.
4. Tests in `test_fetch_attempts_store.py`, each docstring starting with `D166`, with the file's existing helpers:
   - No round started: `list_attempts` returns every attempt, in order (today's behavior).
   - Two attempts, then `start_fetch_round` in a later transaction, then one more attempt: `list_attempts` returns
     only the last one; a `SELECT count(*) FROM fetch_attempts WHERE invoice_id = %s` still returns 3.
   - `start_fetch_round` with no attempts after it: `list_attempts` returns `[]`.
   - `technical_streak` counts attempts from before the round: three `technical` attempts for a portal, then a new
     round, still give `True` for `n=3`.

Each new attempt and the round must commit in separate transactions (the `at` default is the transaction's `now()`).

Test: `uv run pytest -q tests/integration/test_fetch_attempts_store.py tests/integration/test_schema.py` (needs the
compose Postgres); ruff and mypy on the changed files.

## Step 3: A retry starts a new round `[worker]`

Files: `src/invoicebot/pipeline/resume.py`, `tests/integration/test_commands.py`,
`tests/integration/test_gap_resume_flow.py`.

1. `pipeline/resume.py`, `retry_documents`: after `set_state(... FETCHING_DOCS ...)` and before `enqueue`, call
   `start_fetch_round(conn, invoice_id)`. Docstring gains "in a new fetch round (D166)".
2. `test_commands.py`: next to `test_reprocesar_on_manual_required_refetches`, add
   `test_reprocesar_after_used_up_fetchers_runs_the_chain_again` (docstring `D96, D166: ...`). Arrange as the existing
   test, plus, before `/reprocesar`, one committed `record_attempt` per fetcher of the invoice's flow with reason
   `technical` (final today and after D167's one retry is spent: record two per fetcher). Assert after `/reprocesar`:
   the invoice is `FETCHING_DOCS`, one fetch job is queued, `list_attempts` returns `[]`, and
   `decide(flow.fetchers, <those fetchers>, [], <any schedule>)` returns `Run(<first fetcher>)`.
3. `test_gap_resume_flow.py`: add `test_an_active_portal_resumes_an_invoice_whose_fetchers_were_used_up` (docstring
   `R-11.9, D157, D166: ...`): the same arrange and asserts as
   `test_an_active_portal_resumes_the_invoice_and_its_fetch_closes_the_gap`, plus, before the portal is activated,
   committed attempts that used up every fetcher of the flow (as the invoice would have after a real exhaustion,
   e.g. `portal_missing` for the portal fetcher). Factor the shared arrange into a helper in that file only if both
   tests use it unchanged.

Both new tests fail on `main` before change 1 (the first sees the old attempts, the second ends `MANUAL_REQUIRED`
again); check that by running them with `resume.py` reverted.

Test: `uv run pytest -q tests/integration/test_commands.py tests/integration/test_gap_resume_flow.py`.

## Step 4: Log the email and QR fetchers' technical failures `[worker]`

Files: `src/invoicebot/fetchers/email_inbox.py`, `src/invoicebot/fetchers/qr_url.py`,
`tests/unit/test_fetchers_email_inbox.py`, `tests/unit/test_fetchers_qr_url.py`.

1. Both modules get `logger = logging.getLogger(__name__)`.
2. `email_inbox.py`, `fetch`: bind the `(MailboxError, httpx.HTTPError)` exception and, before returning `TECHNICAL`,
   `logger.warning("email inbox fetch failed", extra={"error": type(err).__name__})`. Only the class name: the
   message can hold a mailbox address, a URL or a query.
3. `qr_url.py`: the same in the three `except` branches at lines 119–124: messages `"qr url fetch blocked"`,
   `"qr url fetch timed out"`, `"qr url fetch failed"`, each with `extra={"error": type(err).__name__}`.
4. Tests, each docstring starting with the ID of the test it sits beside plus `R-5.7`: for the email mailbox-error and
   HTTP-error cases, and for the QR timeout and server-error cases, `caplog.at_level(logging.WARNING,
logger=<module name>)` shows exactly one record with that message and `record.error` equal to the raised class's
   name. A successful fetch logs no warning (one email case, one QR case).

Test: `uv run pytest -q tests/unit/test_fetchers_email_inbox.py tests/unit/test_fetchers_qr_url.py`.

## Step 5: Log the portal helper's failures `[worker]`

Files: `src/invoicebot/fetchers/portal.py`, `tests/integration/test_fetchers_portal.py`.

1. `portal.py` gets `logger = logging.getLogger(__name__)`. In `run_browser_job`, before each failure return:
   `TimeoutError` → `logger.warning("portal helper timed out", extra={"portal_key": fetcher_name})`; `RuntimeError` →
   `"portal helper failed"`; `ValidationError` → `"portal helper output invalid"`; the last two also carry
   `"error": type(err).__name__`. Never the exception message or the helper's output (they can hold the invoice's
   data).
2. Tests in `test_fetchers_portal.py` (docstring `R-5.7, R-9.2: ...`), with the file's existing fake-helper setup:
   a helper that exits non-zero logs `"portal helper failed"` with the portal key; a helper that prints invalid JSON
   logs `"portal helper output invalid"`; if the file already has a timeout case, it also asserts the
   `"portal helper timed out"` record. A helper reporting `ok` logs no warning.

Test: `uv run pytest -q tests/integration/test_fetchers_portal.py`.

## Step 6: The chain gives a technical failure one retry `[worker]`

Files: `src/invoicebot/fetchers/base.py`, `src/invoicebot/pipeline/manual_message.py`,
`src/invoicebot/fetchers/chain.py`, `tests/unit/test_fetchers_chain.py`.

1. Move `TECHNICAL_REASONS` from `pipeline/manual_message.py` to `fetchers/base.py` beside `FetchReason` (one home;
   the chain may not import `pipeline`). `manual_message.py` imports it from there, so `pipeline/fetch.py`'s import
   from `manual_message` keeps working.
2. `chain.py`: add `def technical_retry_delay(fetcher, attempts, schedule) -> timedelta | None`: `schedule[0]` when
   `schedule` is not empty and none of `fetcher`'s `attempts` has a reason in `TECHNICAL_REASONS`; else `None`.
   Docstring cites D167 and says `attempts` excludes the run being judged. In `_decide_one`, when the latest reason is
   in `TECHNICAL_REASONS`, return `Wait(fetcher)` if `technical_retry_delay(fetcher, history[:-1], schedule)` is not
   `None`, else `None`. Update `decide`'s and `WAIT_REASONS`'s docstrings for D167. `retry_delay` does not change
   (it counts only `WAIT_REASONS`).
3. `test_fetchers_chain.py` (docstrings start `D167`), with the file's `attempts`/`FETCHER` helpers:
   - The `technical-is-final` case becomes `[TECHNICAL]` → `Wait(FETCHER)`; add `[TIMEOUT]` → `Wait(FETCHER)`,
     `[TECHNICAL, TECHNICAL]` → `Exhausted()` (id `second-technical-is-final`), `[TECHNICAL, TIMEOUT]` →
     `Exhausted()`, `[NOT_FOUND_YET, TECHNICAL]` → `Wait(FETCHER)`.
   - `test_technical_failure_passes_to_the_next_fetcher`: two technical attempts for the first fetcher → `Run` of the
     second.
   - A technical attempt with an empty schedule → the next fetcher runs (one fetcher: `Exhausted()`).
   - `technical_retry_delay`: no earlier attempt → `schedule[0]`; an earlier `timeout` → `None`; an earlier
     `not_found_yet` only → `schedule[0]`; empty schedule → `None`.

List in the summary each existing test whose expectation changed and why (D167); no other test may change.

Test: `uv run pytest -q tests/unit/test_fetchers_chain.py tests/unit/test_manual_message.py` (the second only if it
exists), and `uv run lint-imports`.

## Step 7: The fetch step schedules the technical retry `[worker]`

Files: `src/invoicebot/pipeline/fetch.py`, `tests/integration/test_pipeline_fetch.py`.

1. `_run`: the delay is `technical_retry_delay(...)` when `result.reason in TECHNICAL_REASONS`, else `retry_delay(...)`
   as today (same arguments).
2. `_record_result`: a new branch before the final `else`: `result.reason in TECHNICAL_REASONS and delay is not None`
   → `enqueue(conn, id, FETCH_STEP, await _run_after(conn, delay))`, with no notice and no admin alert (D167). The
   final `else` (immediate next job) stays for every other failure and for a technical failure with no retry left.
   Docstring cites D167.
3. Tests in `test_pipeline_fetch.py` (docstrings start `D167`), with the file's fake fetchers and schedule helpers:
   - A first `technical` result: one attempt recorded, the invoice stays `FETCHING_DOCS`, one fetch job whose
     `run_after` is about `now + schedule[0]` (within the file's existing tolerance), no outbox row.
   - A first `timeout` result: the same.
   - A second `technical` for the same fetcher in the round, with a second fetcher in the flow: the next fetch job
     has no delay; running it runs the second fetcher.
   - A second `technical` with only one fetcher: the next run moves the invoice to `MANUAL_REQUIRED` with BR-F07.
   - BR-F03 still raises `PORTAL_BROKEN` after `broken_after` technical attempts for the portal (the existing test
     must still pass; extend it only if it relied on technical being final at once).

List in the summary each existing test whose expectation changed and why (D167); no other test may change.

Test: `uv run pytest -q tests/integration/test_pipeline_fetch.py tests/integration/test_flow_a.py
tests/integration/test_flow_dni.py tests/integration/test_gap_resume_flow.py`.

## Step 8: D168, Chromium's sandbox keeps its zygote `[inline: design decision and spec wording]`

Found in the first live run of step 10 (2026-11-23): every sandboxed browser fetch fails at launch. Chromium exits
with "Zygote cannot be disabled if sandbox is enabled": spec 03 §8's `--no-zygote` contradicts D147's
`chromium_sandbox=True`, so the helper reports `technical` in about 1 s. The portal has never fetched live. A probe in
the live container loaded the portal with the sandbox on and `--no-zygote` dropped; the RSS sum rose about 56 MiB
(806 vs 750, shared pages counted twice), inside the 450 MB helper budget (spike S0.9: 315 MB peak).

1. `specs/05-open-questions.md`, after D167: **D168** "Chromium's sandbox keeps its zygote": with the sandbox on
   (D147), the helper drops `--no-zygote`, since Chromium refuses to start with both; the zygote costs about 50 MB,
   inside the helper budget. With the sandbox off, the flag stays.
2. `specs/03-design.md` §8, the launch flags line: after `--no-zygote` add "(only with Chromium's sandbox off, D168)".

Test: `grep -n "D168" specs/05-open-questions.md specs/03-design.md` shows both lines.

## Step 9: Drop `--no-zygote` when Chromium's sandbox is on `[worker]`

Files: `src/invoicebot/fetchers/browser/session.py`, `tests/unit/test_browser_session.py`.

1. `session.py`: move `"--no-zygote"` out of `LAUNCH_ARGS` into its own constant `NO_ZYGOTE` (comment: only without
   Chromium's sandbox, D168). `_launch_args(portal, chromium_sandbox)` appends `NO_ZYGOTE` only when `chromium_sandbox`
   is false; `launch_options` passes its flag through. Docstrings cite D168.
2. Tests in `test_browser_session.py` (docstrings start `D168`): with `chromium_sandbox=True`, `options["args"]` has no
   `--no-zygote`; with `False`, it has it once. Every other flag of `LAUNCH_ARGS` is present in both cases.

Test: `uv run pytest -q tests/unit/test_browser_session.py`.

## Step 10: Live: re-fetch invoice 12 `[inline: the owner's live host and real invoices]`

1. Deploy the image (`./deploy/deploy-live.sh <tag>`), check `/health` and that the migration reached `0014`.
2. If invoice 12 is still `FETCHING_DOCS` with its D167 retry queued from the first live run, let that job run (or
   move its `run_after` to `now()`) instead of `/reprocesar`. Otherwise `invoicebot rehearse send --text "/reprocesar 12"`, then `rehearse replies`: expect BR-F12 or the reprocess
   acknowledgement, then no BR-F07; invoice 12 moves to `DELIVERING` and on through flow B (dry-run mail, BR-D07).
3. Check the OneDrive folder with `rclone lsl "onedrive:Reembolsos/<folder>"` for invoice 12's PDF, XML and Excel.
4. `podman logs` for the fetch step shows `portal_key` `facturactiva-la-lena` with reason `ok`; no new warning.

Test: invoice 12 leaves `MANUAL_REQUIRED` through the portal, and its files are in OneDrive.
