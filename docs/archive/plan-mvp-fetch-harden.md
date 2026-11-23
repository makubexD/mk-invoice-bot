# Plan: MVP phase 9o · fetch-harden

Roadmap: `docs/plan-mvp.md`, "Carried over from the `refetch` review". Three problems are left over from 9n:

1. **A helper failure is invisible.** The browser helper catches every error at its top level, prints `technical`
   and exits 0. `run_helper` sends its stderr to `DEVNULL`, so the one thing it logs (the exception class, R-9.16)
   never reaches the bot's log. That is how D168 stayed hidden. The same path serves SOL.
2. **D167 and BR-F03 work against each other.** Each invoice can now add two technical attempts to a portal's
   streak, so `broken_after: 3` trips on two invoices, or on one invoice plus a `/reprocesar`. Also, while a
   `PORTAL_BROKEN` gap is open, every new invoice still waits `schedule[0]` (1 h) for a retry that will fail too.
3. **`/reprocesar` has no brake.** Each `/reprocesar` runs the whole chain again, a SOL login included, and nothing
   stops a sender from repeating it (security review, Low).

Fixes:

- The helper puts the exception class in its one JSON line (`error_class`, an identifier only), and
  `run_browser_job` logs it. The message of our own `RuntimeError` is logged too.
- BR-F03 counts invoices, not attempts.
- An open `PORTAL_BROKEN` gap ends the D167 retry for the portal fetcher. So does the failure that raises the gap.
- `/reprocesar` refuses a new fetch round that starts within `limits.reprocess_min_interval_s` (default 600 s) of the
  last one.

A QR answer of 4xx stays `technical`. BR-F03 lists HTTP 4xx as technical, and the QR fetcher has no `portal_key`, so
it never feeds a streak. The only cost is one D167 retry.

**Needs from the owner first:** nothing for steps 1 to 8. Step 9 uses the live host.

## Step 1: D169–D171, BR-F03 wording, roadmap row 9o `[inline: design decisions and spec wording]`

1. `specs/05-open-questions.md`, after D168, add three rows (status "9o plan, proposed"):
   - **D169** "The helper names its failure": the browser helpers (portal and SOL) add `error_class` to their one
     JSON line when their top-level boundary catches an exception. It holds only the class name: a Python
     identifier of at most 64 characters, checked by the bot's model. `run_browser_job` logs it at `WARNING`. The
     helper's stderr stays discarded (R-9.16). The bot also logs its own `RuntimeError` message (`helper exited
     with code N`, `helper output exceeds the cap`), which holds no page text.
   - **D170** "BR-F03 counts invoices, and an open gap ends the retry": the streak takes each invoice's newest
     attempt on the portal and needs the newest `broken_after` invoices to have all ended technical. While the
     portal's `PORTAL_BROKEN` gap is `open` or `reported`, a technical failure of the portal fetcher gets no D167
     retry. Neither does the failure that raises the gap. A QR 4xx stays technical (BR-F03 lists HTTP 4xx).
   - **D171** "`/reprocesar` waits between rounds": a `/reprocesar` that would start a fetch round (D166) within
     `limits.reprocess_min_interval_s` (default 600) of the invoice's last round is refused with
     `REPROCESS_TOO_SOON`. A gap resume (D157) and an edit are not limited: the bot or a fix starts those, not a
     repeated command.
2. `specs/06-business-rules.md`, the BR-F03 row's condition: "A portal adapter fails `broken_after` (default 3)
   times in a row" becomes "The newest `broken_after` (default 3) invoices that tried a portal adapter all ended on
   a technical failure there", keeping the parenthesis. Its action: after "the chain continues" add "; while the
   gap is open, the portal gets no D167 retry (D170)".
3. `specs/03-design.md`: find where the helper's output line is described (`grep -n "one JSON line\|stdout" specs/03-design.md`)
   and add `error_class` (optional, D169). If no such line exists, add nothing there.
4. `docs/plan-mvp.md`: add row 9o `fetch-harden` (plan: `docs/plan-mvp-fetch-harden.md`, D169–D171) after 9n, before
   9d2. In "Carried over from the `refetch` review", strike through the first three bullets and the "Small" bullet
   (`~~…~~`), ending each with "Moved to 9o (D169–D171)", as other moved items do.

Test: `grep -n "D169\|D170\|D171" specs/05-open-questions.md specs/06-business-rules.md && grep -n "9o" docs/plan-mvp.md`
shows each line.

## Step 2: The portal helper reports its error class `[worker]`

Files: `src/invoicebot/fetchers/browser/protocol.py`, `src/invoicebot/fetchers/browser/__main__.py`,
`tests/unit/test_browser_protocol.py` (new), `tests/unit/test_browser_main.py` (new).

1. `protocol.py`: a constant `ERROR_CLASS_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"` with a comment (an identifier
   only, never page text, R-9.16, D169). `HelperOutput` gains
   `error_class: str | None = Field(default=None, pattern=ERROR_CLASS_PATTERN)`. Update its docstring (D169).
2. `__main__.py`, `main()`'s `except Exception as error` branch: emit
   `HelperOutput(reason=FetchReason.TECHNICAL, error_class=type(error).__name__)`. The `BadRequest` branch stays
   as it is. Add D169 to the module docstring's last sentence.
3. `tests/unit/test_browser_protocol.py`, docstrings start with `D169`:
   - `HelperOutput.model_validate_json('{"reason": "technical", "error_class": "TargetClosedError"}')` keeps the
     class.
   - The field is optional: `{"reason": "technical"}` gives `error_class is None`.
   - Parametrized, each rejected with `ValidationError`: `"a b"`, `"1Error"`, `"Error: page text"`, `""`, and
     `"E" * 65`.
4. `tests/unit/test_browser_main.py`, docstrings start with `D169`. Monkeypatch `__main__`'s `parse_helper_args`
   (return `False`), `_read_request` (return a sentinel `object()`), `limit_resources` (no-op) and `asyncio.run`
   (raise `ValueError("secret page text")`, closing the coroutine it gets so no warning is left):
   - `main()` returns `0`, and stdout (from `capsys`) is one JSON line that parses to
     `{"reason": "technical", "files": [], "error_class": "ValueError"}`.
   - The printed line does not contain `secret page text`.
   - With `_read_request` raising `BadRequest`, the line has `"error_class": null`.

Test: `uv run pytest -q tests/unit/test_browser_protocol.py tests/unit/test_browser_main.py`; ruff and mypy on the
changed files.

## Step 3: The SOL helper reports its error class `[worker:low]`

Files: `src/invoicebot/fetchers/browser/sol.py`, `tests/unit/test_browser_sol.py`.

1. `sol.py`, `main()`'s `except Exception as error` branch: emit
   `HelperOutput(reason=FetchReason.TECHNICAL, error_class=type(error).__name__)`, as step 2 did for the portal
   helper. Cite D169 in the comment.
2. `test_browser_sol.py`: one test, docstring starting with `D169`, in the style of step 2's main test: monkeypatch
   `sol`'s `parse_helper_args`, `read_request`, `limit_resources` and `asyncio.run` (raising `ValueError`). Stdout is
   one JSON line with `"error_class": "ValueError"` and `"reason": "technical"`.

Test: `uv run pytest -q tests/unit/test_browser_sol.py`.

## Step 4: `run_browser_job` logs the helper's error class `[worker]`

Files: `src/invoicebot/fetchers/portal.py`, `tests/integration/test_fetchers_portal.py`.

1. `run_browser_job`, after `HelperOutput.model_validate_json` succeeds: when `output.error_class` is not `None`,
   log `logger.warning("portal helper failed inside", extra={"fetcher": fetcher_name, "error": output.error_class})`,
   then return the output as before.
2. The `except RuntimeError as err` branch adds `"detail": str(err)` to its `extra`. Comment: our own message, no
   page text (D169).
3. Docstring: one sentence on the two log lines (D169).
4. Tests in `test_fetchers_portal.py`, docstrings start with `D169`, using the file's existing fake-helper pattern
   (see `test_fetch_helper_runtime_error_logs_portal_helper_failed`):
   - A helper that prints `{"reason": "technical", "files": [], "error_class": "TargetClosedError"}`: the fetch result
     is `technical`, and one `WARNING` record "portal helper failed inside" has `error == "TargetClosedError"` and
     the portal key as `fetcher`.
   - A helper that prints `{"reason": "not_found_yet", "files": []}`: no "portal helper failed inside" record.
   - A helper that exits with code 3: the "portal helper failed" record has `detail == "helper exited with code 3"`.
   - A helper that prints `{"reason": "technical", "files": [], "error_class": "bad class"}`: the result is
     `technical` and the "portal helper output invalid" record is logged (the pattern rejects it).

Test: `uv run pytest -q tests/integration/test_fetchers_portal.py` (needs the compose Postgres if the file uses it).

## Step 5: BR-F03 counts invoices `[worker]`

Files: `src/invoicebot/store/fetch_attempts.py`, `tests/integration/test_fetch_attempts_store.py`,
`tests/integration/test_pipeline_fetch.py`.

1. `technical_streak(conn, portal_key, n)`: same signature and return. Query each invoice's newest attempt on the
   portal, then the newest `n` of those:

   ```sql
   SELECT reason
   FROM (
       SELECT DISTINCT ON (invoice_id) reason, at, id
       FROM fetch_attempts
       WHERE portal_key = %s
       ORDER BY invoice_id, at DESC, id DESC
   ) AS latest
   ORDER BY at DESC, id DESC
   LIMIT %s
   ```

   Keep the `n < 1` guard and the "exactly `n` rows, all technical" check. Docstring: the newest `n` invoices that
   tried the portal all ended technical there (BR-F03, D170); an invoice counts once however many attempts it had.
2. Tests in `test_fetch_attempts_store.py`, docstrings start with `BR-F03, D170`, each attempt committed in its own
   transaction (the `at` default is the transaction's `now()`). Use the file's helpers and a fresh portal key per
   test:
   - Two invoices with two `technical` attempts each: `technical_streak(…, 3)` is `False`.
   - Three invoices, each with one `technical` attempt: `True` for `n=3`.
   - Three invoices; the middle one has `technical` then `not_found_yet`: `False` (its newest attempt is not
     technical).
   - Three invoices, each ending `timeout`: `True`.
   - The existing streak tests keep passing. If one built its streak from several attempts of one invoice, change
     it to use one invoice per attempt and say so in the summary. Do not weaken its assertion.
3. `test_pipeline_fetch.py`: run `test_broken_three_technical_failures_raise_one_portal_broken_gap` and
   `test_broken_third_invoices_br_f07_names_the_broken_portal` unchanged. If `_technical_failures` records several
   attempts for one invoice, fix the helper so each failure is a separate invoice, and say so in the summary.

Test: `uv run pytest -q tests/integration/test_fetch_attempts_store.py tests/integration/test_pipeline_fetch.py -k
"streak or broken"`.

## Step 6: An open `PORTAL_BROKEN` gap ends the retry `[worker:high: reasoning: the chain decision and the fetch step must agree on the retry, or the step re-runs the portal at once]`

Files: `src/invoicebot/fetchers/chain.py`, `src/invoicebot/pipeline/fetch.py`, `tests/unit/test_fetchers_chain.py`,
`tests/integration/test_pipeline_fetch.py`.

Today `decide` and `_run` each call `technical_retry_delay`, and they must agree: if `_run` enqueues the next job
with no delay but `decide` still says `Wait`, the portal runs again at once.

1. `chain.py`:
   - `technical_retry_delay(fetcher, attempts, schedule, no_retry: Collection[str] = frozenset())` returns `None`
     when `fetcher in no_retry`, before the other checks. Docstring: an open broken gap leaves no retry (D170).
   - `decide(order, registered, attempts, schedule_for, no_retry: Collection[str] = frozenset())` passes `no_retry`
     through `_decide_one` to its `technical_retry_delay` call. Docstring cites D170.
2. `pipeline/fetch.py`:
   - `_portal_is_broken(conn, inputs, reading)` becomes `_portal_is_broken(conn, reading, directory)`, since it
     reads only those two. Update its one caller in `_exhausted` (`inputs.directory`).
   - `handle`: inside the first `async with pool.connection() as conn:` block, after `rows = …`, compute
     `broken = reading is not None and await _portal_is_broken(conn, reading, directory)`. Build
     `no_retry = frozenset({PORTAL_FETCHER}) if broken else frozenset()`, store it on `_Inputs` (new field
     `no_retry: frozenset[str]`), and pass it to `decide`.
   - `_run`: pass `inputs.no_retry` to `technical_retry_delay`.
   - `_raise_broken_gap` returns `bool`, `True` when it raised the gap. In `_run`, when it returns `True` and
     `fetcher == PORTAL_FETCHER`, pass `delay=None` to `_record_result`. The next job's `handle` then sees the open
     gap, so `decide` agrees. Comment: D170.
3. Unit tests in `test_fetchers_chain.py`, docstrings start with `D170`:
   - `technical_retry_delay(FETCHER, [], schedule, no_retry={FETCHER})` is `None`, and with
     `no_retry={"other"}` it is `schedule[0]`.
   - `decide` with history `[Attempt(FETCHER, TECHNICAL)]` and `no_retry={FETCHER}` moves to the next registered
     fetcher (`Run("email_inbox")` with the file's `ORDER`); without `no_retry` it is `Wait(FETCHER)`.
   - From the refetch review: history `[Attempt(FETCHER, TECHNICAL), Attempt(FETCHER, NOT_FOUND_YET)]` waits, and
     `retry_delay(FETCHER, history, schedule)` is `schedule[1]` (the technical attempt does not use a wait slot).
     If the code gives `schedule[0]` instead, stop and report: do not change `retry_delay`.
4. Integration tests in `test_pipeline_fetch.py`, docstrings start with `D170`, using the file's helpers for
   technical portal failures and gaps:
   - With an open `PORTAL_BROKEN` gap for the issuer's portal, a first technical failure of the portal fetcher
     enqueues the next fetch job with `run_after` at about `now()` (not `schedule[0]` later), and the next run
     moves on to the next fetcher.
   - The failure that raises the gap (the third invoice of a streak) gets no retry: its next job has no delay.
   - Without a gap, a first technical failure still waits `schedule[0]` (the existing
     `test_first_technical_failure_schedules_one_retry_without_a_message` keeps passing).

Test: `uv run pytest -q tests/unit/test_fetchers_chain.py tests/integration/test_pipeline_fetch.py`.

## Step 7: The reprocess interval in config and the store `[worker]`

Files: `src/invoicebot/config/models.py`, `config/example.yaml`, `src/invoicebot/store/invoices.py`,
`tests/integration/test_invoices_store.py`.

1. `models.py`, `Limits`: add `reprocess_min_interval_s: int = Field(default=600, ge=0)` with a comment citing D171.
   The default keeps existing private configs valid.
2. `config/example.yaml`, under `limits:` after `per_domain_min_interval_s`: `reprocess_min_interval_s: 600   # D171
   · /reprocesar waits this long after the last fetch round`.
3. `store/invoices.py`: `async def fetch_round_started_within(conn, invoice_id: int, seconds: int) -> bool` returns
   whether the invoice's `fetch_round_at` is later than `now() - make_interval(secs => %s)`. It is `False` for a
   `NULL` round or a missing invoice. Use the database clock and parameterized SQL. Docstring: D171; does not commit.
4. Tests in `test_invoices_store.py`, docstrings start with `D171`, with the file's invoice helper:
   - No round started: `False`.
   - `start_fetch_round` committed, then `fetch_round_started_within(…, 600)`: `True`.
   - The round set to `now() - interval '20 minutes'` with an `UPDATE`: `False` for 600.
   - `seconds=0`: `False` right after a round starts in an earlier transaction.
   - An unknown invoice id: `False`.

Test: `uv run pytest -q tests/integration/test_invoices_store.py`; `uv run pytest -q tests/unit -k config` still
passes (the example config loads).

## Step 8: `/reprocesar` refuses a round that comes too soon `[worker]`

Files: `src/invoicebot/pipeline/commands.py`, `src/invoicebot/pipeline/intake.py`,
`src/invoicebot/rules/messages_es.py`, `tests/integration/test_commands.py`.

1. `messages_es.py`, next to `REPROCESS_NOT_ALLOWED`: `REPROCESS_TOO_SOON = "Ya reintenté el comprobante #{id} hace
   poco. Vuelve a usar /reprocesar {id} en unos minutos."` with a comment `#: /reprocesar's answer when the last
   fetch round is too recent (S4.9, D171).`
2. `commands.py`:
   - `CommandContext` gains `reprocess_min_interval_s: int = 0` (0 means no wait; the docstring says so).
   - `_reprocesar`: before `_requeue`, when `head.state in (FAILED, MANUAL_REQUIRED)` and
     `await fetch_round_started_within(conn, invoice_id, context.reprocess_min_interval_s)`, reply
     `REPROCESS_TOO_SOON.format(id=invoice_id)` and return. A `BATCHED` invoice is not limited (it starts no fetch
     round). Docstring cites D171.
3. `intake.py`, where `CommandContext(…)` is built: pass
   `reprocess_min_interval_s=cfg.limits.reprocess_min_interval_s`, using the config object already in scope there.
   If none is in scope, stop and report instead of adding a global lookup.
4. Tests in `test_commands.py`, docstrings start with `D171`, beside the existing `/reprocesar` tests:
   - A `MANUAL_REQUIRED` invoice whose round started 1 minute ago (an `UPDATE` setting `fetch_round_at`), with
     `reprocess_min_interval_s=600`: the reply is `REPROCESS_TOO_SOON`, the state stays `MANUAL_REQUIRED`, and no
     fetch job is enqueued.
   - The same invoice with its round 20 minutes ago: the existing "started" reply, and the state is
     `FETCHING_DOCS`.
   - With the default context (`0`), a round 1 minute ago does not block.
   - A `BATCHED` invoice with a failed batch and a recent round is still requeued.

Test: `uv run pytest -q tests/integration/test_commands.py -k reprocesar`.

## Step 9: Live: deploy and read one helper failure `[inline: the owner's live host]`

1. Deploy the image (`./deploy/deploy-live.sh <tag>`), check `/health`.
2. In the live container, run a probe that makes the portal helper fail (for example, a request with a portal URL
   the guard blocks, or `podman exec` with the helper on an unreachable host), and check that `podman logs` shows
   "portal helper failed inside" with an `error` class, and no page text.
3. Check that live invoices 9, 10 and 13 are not left in a retry loop: any with an open `PORTAL_BROKEN` gap moves on
   without a 1 h wait.

Test: one `WARNING` "portal helper failed inside" with an `error` field in the live log.
