# Plan: MVP phase 9d · ops-live

Roadmap: `docs/plan-mvp.md` row 9d (D136, D141). The bot runs for real on this machine: flow A (Google) first, then flow B (Microsoft). Before that, the four `ops-harden` review findings and the `ops-host` browser-helper finding are fixed. The live runs then close the checks every earlier phase deferred.

**Goal:**

- `/gap ignorar` stays ignored after a queued issue opens. A taught link never leaves a comment row waiting forever. A crafted XML cannot put text into `/gaps`. The restore drill passes the same URL keys as the backup.
- A browser helper sees only its own fetch folder under `/data`, not the filed photos.
- Google is signed in and the bot's Drive folder and both sheets exist. The chat-free checks are done: backup and restore drill, padrón import, SOL (S0.4) and its host allowlist.
- Once Meta verification is approved: flow A (M6), flow B (M3, M4) and the read checks (M5) run through WhatsApp, and the results are recorded.

**Needs from the owner first:**

- Steps 1–7: nothing.
- Steps 8–10: the Google OAuth client. The owner set it up on 2026-11-18, and `google.client_id` is in the private config. They also need the owner at the keyboard for the browser sign-ins and the SOL login.
- Steps 12–16: Meta business verification approved. On 2026-11-18 it was still in review, so the WABA cannot send. The Entra app exists (`microsoft.client_id` is set). Step 14 also needs S0.11 and the bot's Outlook.com mailbox. Step 15 needs S0.10. Step 16 needs S0.1 (≥ 30 pairs) and S0.5, and records what is missing if they are not ready.

**Moved out by the owner (2026-11-18):** S12.7 (repo private) is "not yet". S16.2 waits for it (D52), so both leave this phase. Step 1 records that in the roadmap.

**Not in this plan:** the `flows-config` leftover on the `spreadsheets` scope applies only once a sink is `existing: true`. None is, so it stays in the roadmap. The `ops-host` `/proc` leftover and the VPS digest pin stay "Later".

## Design choices

Step 1 writes these into spec 05. They are proposals for the owner's review of this plan.

- **D150 · The browser helper sees only its own fetch folder under the data root.**
  - Each fetch already gets its own folder, `<data_dir>/tmp/<invoice>/<job>-<uuid>` (`pipeline/fetch.py`), and the files of a committed run stay there.
  - In the image, the browser wrapper first binds that folder aside. It then covers the data root with a read-only 4 KiB tmpfs, recreates the folder's path and moves the bind back. The helper can write its downloads but cannot list or read anything else in `/data`: no filed photos, no other fetch.
  - The folder reaches the wrapper as `HELPER_OUT_DIR` in the helper's environment. The wrapper refuses a folder outside the data root. Without `HELPER_OUT_DIR` (the startup check), the whole data root is masked.
  - The extract helper is unchanged: it must read the photo, and it has no network.
- **D151 · Live runs start with the chat-free checks while Meta verification is pending.**
  - The WABA cannot send until verification. So Google setup, the backup and restore drill, the padrón import and SOL (S0.4) run first: none of them needs a chat reply.
  - Every check that needs a reply waits for the Meta step (step 12). That covers BR-D16, buttons, `/reprocesar` and `/pagado`.
- **D152 · The SOL session allows only the SUNAT hosts the live login used.**
  - Step 10 records the hosts the live SOL login and query reach. Step 11 gives `GuardProxy` an allowlist of exactly those hosts for the SOL helper (`CONNECT` and `GET`), and installs the page route before the first `goto`.
  - Portal fetches keep today's guard. Websockets and redirect hops to any other host are refused at the proxy.

## Step 1: Record D150–D152 and the S12.7 deferral `[inline: spec and roadmap text the owner reviews with this plan]`

Files: `specs/05-open-questions.md`, `docs/plan-mvp.md`.

- Spec 05: add rows D150, D151, D152 with the text of "Design choices" above, each marked "(9d plan, proposed)".
- Roadmap row 9d:
  - Backlog: "live runs: flow A first, then flow B; the 9a and 9c review findings; SOL host allowlist (D150–D152)". Plan: `docs/plan-mvp-ops-live.md`.
  - Needs from the owner: "Meta verification; the Google and Entra apps".
- A new row 9g `private` after 9f: "S12.7 (repo private), then S16.2 (D52)", needs "the owner's word for S12.7 (not yet on 2026-11-18)".

Test: `npx prettier --check docs/plan-mvp.md specs/05-open-questions.md`.

## Step 2: An ignored gap stays ignored when its issue opens `[worker]`

Files: `src/invoicebot/store/gaps.py`, `src/invoicebot/pipeline/gaps.py`, `tests/integration/test_pipeline_gaps.py`.

- `store/gaps.mark_reported`: the SQL becomes `UPDATE gaps SET state = CASE WHEN state = 'ignored' THEN state ELSE 'reported' END, issue_url = %s WHERE fingerprint = %s`. Docstring: "Store the issue URL and move the gap to reported unless it is ignored (D116, D139); does not commit."
- `pipeline/gaps.issue_opened`: return before the alert also when `gap.state == "ignored"`. Add that to the docstring: "An ignored gap gets its URL but no alert (D139)."

Tests (integration, real Postgres; docstrings start with `D139`):

1. A gap with a pending `create_issue` row is ignored, then `issue_opened` runs: the gap's state is still `ignored`, its `issue_url` is stored, and no `GAP_admin` outbox row was added.
2. A gap that is not ignored still becomes `reported` and gets one `GAP_admin` row (existing behavior; keep or extend the existing test).

Test: `uv run pytest -q tests/integration/test_pipeline_gaps.py`.

## Step 3: A taught link never queues a comment that waits forever `[worker]`

Files: `src/invoicebot/pipeline/gaps.py`, `src/invoicebot/pipeline/teach.py`, `tests/integration/test_pipeline_teach.py`.

Background: the outbox has only the states `pending`, `sent` and `dead`. "Sending" means a `pending` row locked by the sender. While that row is locked, `set_pending_issue_hint`'s `UPDATE` waits. When the lock is released, the row is `sent` and the issue callback (`issue_opened`) has already committed the URL.

- `pipeline/gaps._issue_queued`: add `AND payload_json->>'op' = 'create_issue'` to the `WHERE` clause. A pending comment row no longer blocks a new `create_issue`.
- `pipeline/gaps._queue_alert`: when it queues a `create_issue`, the hint goes into the payload's fields. If `sighting.fields.hint_url` is `None` and `gap.hint_url` is set, use `dataclasses.replace(sighting.fields, hint_url=gap.hint_url)`. That way a link taught before GitHub was enabled reaches the issue body.
- `pipeline/teach._record_hint`, after `set_hint`:
  - `gap is None` → return (as now).
  - `gap.issue_url` set → queue the `comment` row with `issue_url` (as now).
  - `set_pending_issue_hint(...) > 0` → return (as now).
  - Otherwise, read the gap again with `get_gap`. If `issue_url` is now set (the in-flight create finished), queue the `comment` row with that `issue_url`. If not, queue nothing: no issue exists and none is queued, and the hint stays on the gap for the next `create_issue`.
  - The `gap.raised_at is None` branch goes away, since it is covered by "queue nothing". Update the docstring to the three outcomes.
- Every `comment` row queued from here now carries `issue_url`. Leave the transport's wait path (`_comment_issue_url`) as it is.

Tests (integration; docstrings start with `D118`):

1. GitHub enabled, the gap raised, no outbox row for it, `issue_url` unset: `_record_hint` (through the public teach entry the existing tests use) queues no `github` row, and `gaps.hint_url` is the link.
2. Same, but with a `pending` `create_issue` row: the row's `fields.hint_url` is the link and no comment row is added (existing behavior).
3. The gap has an `issue_url`: one `comment` row with that `issue_url`.
4. `_issue_queued`: with only a pending `comment` row for the fingerprint, a new sighting of the gap queues a `create_issue` row.
5. A gap with a `hint_url` and no issue, GitHub enabled, raised again: the new `create_issue` row's `fields.hint_url` is the stored link.

Test: `uv run pytest -q tests/integration/test_pipeline_teach.py tests/integration/test_pipeline_gaps.py`.

## Step 4: The UBL path checks `num_doc_adq` `[worker]`

Files: `src/invoicebot/extract/ubl.py`, `tests/unit/test_extract_ubl.py`.

- `extract/ubl.py` imports `NUM_DOC_ADQ_PATTERN` from `invoicebot.extract.qr_payload` (one home). At line ~223, the customer id is added as `num_doc_adq` only when `NUM_DOC_ADQ_PATTERN.fullmatch(text)` matches; otherwise the field is left out, as for a missing id.

Tests (unit; docstrings start with `R-2.1` or the rule the existing UBL tests cite):

1. A customer id `20123456789` (synthetic RUC from `config/example.yaml` style) → `num_doc_adq` is set.
2. A customer id with a newline, a URL (`https://x.example`) or 16 characters → no `num_doc_adq` field.

Test: `uv run pytest -q tests/unit/test_extract_ubl.py`.

## Step 5: The restore drill maps URL keys like the backup `[worker]`

Files: `scripts/restore_drill.sh`, `tests/integration/test_restore_drill.py`.

- Replace the inline `python3 -c` URL split with `uv run --project "$repo_root" python -I -` reading the URL on stdin. That code imports `pg_environment` from `invoicebot.ops.backup` and writes each `KEY=VALUE` (all keys except `PATH`) NUL-separated to stdout. On `ValueError` it prints the error (key name only, never a value) to stderr and exits 1.
- The script exports each pair. This keeps the host's case and passes `sslmode` and the other `PG_ENVIRONMENT_BY_QUERY_KEY` keys. An unknown key fails the drill with "restore drill: cannot parse RESTORE_DATABASE_URL" plus the key name.
- `repo_root` must be set before this block (move its two lines up). The password never appears in argv (R-9.15).

Tests (integration; docstrings start with `R-9.11`):

1. A URL with `?sslmode=disable` restores as before (the test database needs no SSL).
2. A URL with `?foo=1` exits non-zero, prints `foo` on stderr, and never prints the password.

Test: `uv run pytest -q tests/integration/test_restore_drill.py`.

## Step 6: The browser wrapper masks the data root (D150) `[worker:high: reasoning: mount-namespace shell ordering (bind aside, mask, recreate, move back) that must keep the existing sandbox guarantees]`

Files: `src/invoicebot/sandbox.py`, `tests/unit/test_sandbox.py`.

- `BROWSER_SCRIPT` takes the data root as its second argument, after the tmp size:

  ```sh
  set -e
  echo 1000 > /proc/self/oom_score_adj
  mount -t tmpfs -o "size=$1m,mode=0700" none "$TMPDIR"
  data=$2
  shift 2
  out=${HELPER_OUT_DIR:-}
  if [ -n "$out" ]; then
    case "$out" in "$data"/*) ;; *) echo "out dir outside data root" >&2; exit 1 ;; esac
    mkdir "$TMPDIR/.out"
    mount --bind "$out" "$TMPDIR/.out"
  fi
  mount -t tmpfs -o size=4k,mode=0755 none "$data"
  if [ -n "$out" ]; then
    mkdir -p "$out"
    mount --move "$TMPDIR/.out" "$out"
  fi
  mount -o remount,ro "$data"
  while [ "$1" != "--" ]; do mount -t tmpfs -o size=4k,mode=0555 none "$1"; shift; done
  shift
  unshare --user --map-user=1000 --map-group=1000 "$@" <&0 &
  wait $!
  ```

  Keep the existing comment block and add one line: "The data root is masked read-only; only `HELPER_OUT_DIR` is bound back (D150)."

- `browser_prefix(hidden_dirs, tmp_mb, data_dir: Path)` puts `str(data_dir)` right after the tmp size argument. `helper_prefixes(enabled, hidden, tmp_mb, data_dir: Path)` passes it on. `data_dir` must be absolute and not `/`; otherwise raise `ValueError`.
- `check_sandbox` is unchanged: without `HELPER_OUT_DIR` the whole data root is masked.

Tests (unit; docstrings start with `D150`):

1. `browser_prefix([...], 64, Path("/data"))` puts `"64", "/data"` right after the script name, before the hidden dirs and `--`.
2. `browser_prefix(..., Path("/"))` and `Path("data")` raise `ValueError`.
3. `helper_prefixes(True, ..., data_dir)` passes `data_dir` to the browser prefix only; the image prefix is unchanged.
4. Update the existing prefix tests to the new signature without weakening them.

Test: `uv run pytest -q tests/unit/test_sandbox.py`.

## Step 7: Wire the data root and the out dir (D150) `[worker]`

Files: `src/invoicebot/app.py`, `src/invoicebot/fetchers/portal.py`, `src/invoicebot/fetchers/sunat_sol.py`, `tests/unit/test_fetchers_portal.py`, `tests/deploy/sandbox_checks.py`.

This is a little over the usual three files. The two fetchers change one call each, and the container check proves the step.

- `app.py`: pass `settings.data_dir` to `helper_prefixes`.
- `fetchers/portal.run_browser_job(command, payload, timeout_s, fetcher_name, out_dir: Path)`: the helper's env is `helper_env(Path(scratch)) | {"HELPER_OUT_DIR": str(out_dir)}`. `PortalFetcher._run` and `SunatSolFetcher` pass their `out_dir`. Update the docstring.
- `tests/deploy/sandbox_checks.py`: add `check_browser_data_masked`. Run it with a data root that holds `photos/x.jpg` and an out dir `tmp/1/a` holding `in.txt`, using the browser prefix and `HELPER_OUT_DIR=<root>/tmp/1/a`. Inside:
  - `cat <out>/in.txt` works, and `echo y > <out>/new` works and is visible on the host afterwards;
  - `ls <root>/photos` and `cat <root>/photos/x.jpg` fail, and `touch <root>/z` fails;
  - a run with `HELPER_OUT_DIR=/etc` exits non-zero.

  Register it with the other checks, and pass the data root to the module's `PREFIXES`.

Tests:

1. Unit (`R-9.2`): `run_browser_job` with a fake `run_helper` (the existing test seam) gets an env holding `HELPER_OUT_DIR` equal to the `out_dir` it was given.
2. The container check above.

Test: `uv run pytest -q tests/unit/test_fetchers_portal.py tests/unit/test_fetchers_sunat_sol.py tests/unit/test_sandbox.py && bash tests/deploy/test_helper_sandbox.sh`.

## Step 8: Google sign-in and the bot's Drive and sheets `[inline: uses the owner's Google account and private config]`

Main session with the owner. The worker never sees the config or the secrets.

1. Confirm in Google Cloud that the OAuth consent screen is "In production" (a "Testing" app's refresh token dies in 7 days) and that the client is a Desktop client.
2. `google_client_secret` in the age store (`scripts/secrets_edit.py`). The owner types it, and it is never pasted into chat.
3. `invoicebot auth-google --account owner`, then `--account invoices_a` (same mailbox today; D138 checks it).
4. `invoicebot google-setup --flow personal_google` and `--flow personal_dni`. Put the Drive `root_folder_id` and both `spreadsheet_id`s in the private config. `scripts/check_config.py` exits 0.
5. Copy the config and the token files to `$LIVE_ROOT/config` as step 13 of 9c did, run `./deploy/deploy-live.sh <tag>`, and check that `/health` is 200.
6. Record in `specs/spikes/host.md`: the date, publishing status, scopes granted. No ids, no mailbox.

Test: `scripts/check_config.py` exits 0 on the live config; `/health` 200.

## Step 9: Chat-free live checks `[inline: changes this host and the owner's OneDrive]`

1. **Backup and restore drill (R-9.11):** with the `age` key pair and the OneDrive backup folder (S0.10, D119) ready, set `ops.backup.at` a few minutes ahead and confirm two `.age` files land. Then run `scripts/restore_drill.sh` with the private key into an empty Postgres, and record the drill in `deploy/README.md`. If S0.10 is not ready, record that and skip.
2. **Padrón (S3.4):** download `padron_reducido_ruc.zip` and run `scripts/import_padron.py` under `/usr/bin/time -v`. Record peak RSS (target < 50 MB) and peak disk in `specs/spikes/accuracy.md`.
3. **GitHub gaps (6e):** only if S0.10 is ready. Fill `gaps.github` on the host and put `github_app_private_key` in the age store. Run the daily GitHub App check by hand and confirm it passes. The issue part runs in step 15.

Test: the checks above, by hand.

## Step 10: S0.4, SOL received invoices `[inline: the owner's SOL login in a real browser]`

1. The owner logs in to SOL "Consulta de comprobantes" by hand. Does it give the XML of a **received** invoice? Record yes or no with screenshots (kept private) in `specs/spikes/portals.md`.
2. Record every host the login and the query reach (devtools network tab: redirects, websockets, static hosts). Store the list in `specs/spikes/portals.md` for step 11.
3. **Yes:** fill `config/sunat_sol.yaml`'s selectors (watch for frames in SOL's menu), keep it `todo` until step 11 lands, then `active`. **No:** record it, keep the file `todo`, and drop `sunat_sol` from flow A's default chain in `config/example.yaml` and the private config.

Test: the record exists; `scripts/check_config.py` exits 0.

## Step 11: SOL host allowlist in the guard proxy (D152) `[worker]`

Only if step 10 found that SOL gives the XML. Otherwise skip it and say so in the step's commit (`Route: inline: skipped, S0.4 no`).

Files: `src/invoicebot/fetchers/browser/proxy.py`, `src/invoicebot/fetchers/browser/sol.py`, `tests/unit/test_browser_proxy.py`.

- `GuardProxy` takes `allowed_hosts: frozenset[str] | None = None`. When it is set, a `CONNECT` or `GET` to a host not in the set (exact match, lower-cased) gets 403, before any DNS lookup. `None` keeps today's behavior for portals.
- `sol.py` builds its proxy with `allowed_hosts=SOL_HOSTS`. That is a `frozenset` constant in `invoicebot/sunat_origins.py` (the one home of the SUNAT origins), filled with the step 10 list. It installs the page route before the first `page.goto`.

Tests (unit; docstrings start with `D152`):

1. Allowlisted proxy: `CONNECT` to a listed host passes on to the existing guard; `CONNECT` and `GET` to `evil.example` get 403 and no resolver call.
2. Proxy without the allowlist: unchanged (existing tests pass untouched).

Test: `uv run pytest -q tests/unit/test_browser_proxy.py tests/unit/test_fetchers_sunat_sol.py`.

## Step 12: Meta goes live `[inline: Meta business settings and secrets]`

Waits for Meta business verification. Until then, stop here and push-notify the owner that steps 1–11 are done.

The steps follow the `live-trial-meta-setup` memory. Do not create a second app or portfolio.

1. A permanent System User token with `whatsapp_business_messaging` and `whatsapp_business_management`, stored in the age store as `whatsapp_token` (replaces the 24 h one). Reset the app secret (it was pasted in chat on 2026-11-16) and store the new one.
2. Register the bot number with the Cloud API (it leaves the WhatsApp Business app), and put its `phone_number_id` in the private config.
3. Submit the utility templates (S0.7) and wait for approval.
4. Webhook callback `https://<machine>.<tailnet>.ts.net/webhook/whatsapp` with the verify token, subscribed to `messages`. Redeploy.
5. The owner sends "Hola" and gets the bot's answer.

Test: one inbound and one outbound message in the live log; no 131031.

## Step 13: Flow A live (M6) `[inline: the owner's real invoices and Google account]`

1. **M6:** a personal invoice photo leads to files in `{root}/{YYYY}/{MM}/`, one Sheet row, and BR-D16 with working links. Send it again after `/reprocesar`: the row is updated, not duplicated.
2. **PDF only (D142):** an invoice from a store that gives only the PDF completes.
3. **DNI boleta (D143):** a real boleta to the owner's DNI routes to `personal_dni` and writes one row with BR-D18. Record how many boleta photos carry a QR with the DNI.
4. **Gmail:** forward a store's invoice mail to the dedicated Gmail; `email_inbox` finds its XML.
5. **WhatsApp PDF (read-more):** a store PDF sent as a document is read from its text layer.
6. **SOL:** if step 10 said yes, one invoice goes through `sunat_sol`.
7. **BR-D01:** remove the app's access in the Google account, send a photo, and confirm `PENDING_AUTH` plus the admin alert. Sign in again and confirm the invoice finishes.
8. Record M6 in `specs/04-tasks.md`.

Test: the checks above, by hand.

## Step 14: Flow B live (M3, 6d) `[inline: the owner's work invoices, OneDrive and mailboxes]`

Follow `docs/plan-mvp-file-b.md` § "Deferred: live run (M3)" and then `docs/plan-mvp-widen-b-deliver.md` § "Deferred: live check": `inbox_rule`, the laptop agent, `/pagado` and the go-live import dry run. The import's real run waits for the owner's go. Set `mail.send_enabled` back to `false` after each run, and record M3 and the 6d part of M4 in `specs/04-tasks.md`.

Test: the checks in those two sections, by hand.

## Step 15: Gaps and self-alerts live (6e) `[inline: GitHub App on the ops repo and the owner's chat]`

Follow `docs/plan-mvp-widen-b-gaps-ops.md` § "Deferred: live check" (GitHub and self-alerts; the backup part was step 9). Also check this plan's fixes:

- `/gap ignorar` on a gap whose issue is queued: the gap stays ignored and gives no `GAP_admin`.
- `/portal` for a gap with no issue yet: no comment row waits.

Record M4 in `specs/04-tasks.md`.

Test: the checks above, by hand.

## Step 16: Read checks and the M7 record (M5, M7) `[inline: the owner's private samples and SUNAT credentials]`

1. **S0.5:** with the SUNAT API credentials in the age store and `validez_api.enabled: true`, send a work invoice. Confirm the token URL and request fields against the real API (fix the config defaults if they differ) and that `sunat_status_json` holds `estado_cp` 1.
2. **S0.1:** with ≥ 30 private pairs, run `scripts/accuracy_report.py --set <private dir> --write` and record the numbers. Check `_fix_serie` against any serie with D, O, Q, I or L in its last three characters.
3. If S0.1 or S0.5 is not ready, record what is missing. v1 cannot go live without S0.1 (D56).
4. Record M5 and the 9d part of M7 in `specs/04-tasks.md`.

Test: the records exist.
