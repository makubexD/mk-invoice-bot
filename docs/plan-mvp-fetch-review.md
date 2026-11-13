# Plan: MVP phase 4 · fetch, review fixes

Roadmap: `docs/plan-mvp.md` row 4. This plan fixes the findings from the `fetch` phase's finish review (`py-code-reviewer` and `py-security-auditor` on `origin/main...HEAD`, 2026-11-13) on the same branch, before the phase's PR. Requirements: R-5.1, R-5.6, R-5.8, R-5.10, R-9.1, R-9.2. Rules: BR-F07, BR-F09. Decisions: D65, D82.

**Goal:** every hop the browser makes is checked against the guard, a duplicate fetch job does no second fetch, and the review's other findings are fixed or carried to `ops` by name.

## Owner decisions (2026-11-13)

- **Redirect bypass (High, confirmed with a local probe):** `context.route` sees only the first request; Chromium follows a 3xx natively, and a fulfilled 3xx is followed without routing too. So a portal, or anything it loads, can redirect to loopback, Postgres or 169.254.169.254. **Fix: a guard proxy inside the helper** (steps 3 and 4). Chromium sends every connection through it, so redirects, WebSockets and DNS rebinding are all checked, and the proxy connects to the address it checked.
- **`--no-sandbox` (Medium):** an explicit `ops` gate, no code change here (step 14).

## Step 1: A duplicate fetch job does not fetch twice (R-5.1, D65) `[worker]`

Files: `src/invoicebot/pipeline/fetch.py`, `src/invoicebot/store/fetch_attempts.py`, `tests/integration/test_pipeline_fetch.py`.

- The worker deletes a job after the handler commits, so a crash in between delivers it again. In the handler's write transaction, lock the invoice's attempts (`SELECT … FOR UPDATE` on the invoice row through a store function) and compare the attempt count with the count the decision was made on; when it changed, write nothing and return.
- Test (R-5.1): running the same `fetch` job twice records one attempt and leaves one queued `fetch` job.

Test: `uv run pytest -q tests/integration/test_pipeline_fetch.py`

## Step 2: `guard_blocked` only for a blocked navigation (R-5.10, BR-F09) `[worker]`

Files: `src/invoicebot/fetchers/browser/routing.py`, `src/invoicebot/fetchers/browser/__main__.py`, `tests/integration/test_browser_helper.py`.

- `RouteGuard` keeps `first_block` only for a request where `route.request.is_navigation_request()` is true; a blocked sub-resource (a dead CDN host) is still aborted but recorded nowhere.
- `__main__.py`: promote to `GUARD_BLOCKED` only a `TECHNICAL` or `TIMEOUT` result, never `NOT_FOUND_YET`.
- Tests (R-5.10): a blocked sub-resource on a not-found page gives `not_found_yet`; a blocked navigation with a technical result gives `guard_blocked`.

Test: `uv run pytest -q tests/integration/test_browser_helper.py`

## Step 3: The guard proxy (R-5.10, D82) `[worker:high: reasoning: a security-critical proxy whose hop and address-pinning rules the spec cannot fully hold]`

Files: `src/invoicebot/fetchers/browser/proxy.py` (new), `tests/integration/test_browser_proxy.py` (new).

- `GuardProxy(http_hosts, resolver, *, check_addresses=check_addresses)` is an asyncio server on `127.0.0.1:0` with `start()`/`close()` (async context manager) and a `port` and `first_block` attribute.
- `CONNECT host:port` (HTTPS and `wss`): only port 443; `resolve_and_check(host, resolver)`; on a reason, answer `403` and keep the first reason; else open the TCP connection to the **checked address** (not the name) and pipe both ways until either side closes.
- Plain `GET http://host/...` (absolute-form): allowed only through `check_url(url, http_hosts)`, then the same resolve-and-check and pinned connection; forward the request and pipe the response.
- Caps: request head 16 KiB, at most 32 open tunnels; an idle tunnel closes after the helper's page timeout. Any other method or malformed head gets `400`.
- `check_addresses` is injectable so the tests can allow their loopback server; production uses `net.guard.check_addresses`.
- Tests (R-5.10): a CONNECT to a host resolving to 127.0.0.1 is refused with `403` and `first_block` set; an allowed CONNECT tunnels bytes both ways; a plain-http request to a host outside `http_hosts` is refused; a malformed head gets `400`; the 33rd tunnel is refused.

Test: `uv run pytest -q tests/integration/test_browser_proxy.py`

## Step 4: Chromium goes through the proxy (R-5.10) `[worker]`

Files: `src/invoicebot/fetchers/browser/__main__.py`, `src/invoicebot/fetchers/browser/routing.py`, `tests/integration/test_browser_helper.py`.

- Start `GuardProxy` before Chromium and launch with `proxy={"server": f"http://127.0.0.1:{port}"}` and `--proxy-bypass-list=<-loopback>` so loopback goes through the proxy too. `new_context(service_workers="block")`.
- `RouteGuard` keeps only resource blocking (fonts, media, analytics, images); its URL and address checks move to the proxy. The result's `guard_blocked` comes from the proxy's `first_block` under step 2's rule.
- Remove the DNS-rebinding `shortcut:` comment: the proxy pins the checked address.
- Tests (R-5.10): a page whose request 302s to a loopback URL never reaches the loopback server (it counts hits) and the fetch is `guard_blocked`; a WebSocket to loopback is refused.

Test: `uv run pytest -q tests/integration/test_browser_helper.py tests/integration/test_browser_form.py`

## Step 5: `portal-test` tests a portal that is not live yet (S10.5) `[worker:low]`

Files: `src/invoicebot/cli.py`, `tests/unit/test_cli.py`.

- In the same `model_copy`, set the tested portal's `status` to `active`, so a `todo` portal can be tried before it goes live.
- Test (R-5.7): a `todo` portal with the fake helper exits 0.

Test: `uv run pytest -q tests/unit/test_cli.py`

## Step 6: `run_helper` moves out of `pipeline` `[worker:low]`

Files: `src/invoicebot/pipeline/helper.py` → `src/invoicebot/helper.py`, its callers (`pipeline/extract.py`, `fetchers/portal.py`, tests importing it), `.importlinter`.

- `git mv` and update imports; no logic change.
- `.importlinter`: add `invoicebot.fetchers` to the core-modules contract's sources, and a contract forbidding `invoicebot.fetchers` from importing `invoicebot.pipeline`, `invoicebot.queue` and `invoicebot.store`.

Test: `uv run lint-imports && uv run pytest -q tests/integration/test_fetchers_portal.py tests/integration -k extract`

## Step 7: The OCR helper is single-threaded before it forks Tesseract (R-9.2) `[worker:low]`

Files: `src/invoicebot/extract/__main__.py`, `src/invoicebot/extract/ocr.py`.

- Call `cv2.setNumThreads(0)` at the start of `main`, before any cv2 work, so `preexec_fn` cannot deadlock on a thread holding a lock; the comment at `ocr.py` names this as why it is safe.

Test: `uv run pytest -q tests/unit/test_extract_ocr.py tests/unit/test_extract_qr.py`

## Step 8: The bot checks the downloaded PDF itself (R-5.6) `[worker:low]`

Files: `src/invoicebot/fetchers/portal.py`, `tests/integration/test_fetchers_portal.py`.

- Re-stat the PDF the helper reports and refuse it above `max_bytes.pdf` (`TECHNICAL`, files removed), as the XML already is; the size in the result is the bot's own stat.
- Tests (R-5.6): a helper that reports a smaller size than the PDF on disk gets the real size; an oversized PDF is technical and leaves no files.

Test: `uv run pytest -q tests/integration/test_fetchers_portal.py`

## Step 9: A failed fetch leaves no files in `tmp/` (R-9.2) `[worker]`

Files: `src/invoicebot/pipeline/fetch.py`, `tests/integration/test_pipeline_fetch.py`.

- Create the fetch folder with the media `DIRECTORY_MODE`; on any result but `ok`, remove the whole folder (`shutil.rmtree`), so a helper killed on timeout leaves nothing. Use `handlers.TMP_DIRECTORY` instead of a second constant, and `PORTAL_FETCHER` wherever `"portal_registry"` is spelled out (`app.py` too).
- Mark with `shortcut:` that a fetched file's `local_path` stays under `tmp/` until `deliver` moves it.
- Tests (R-9.2): a timeout result leaves no fetch folder; an `ok` result keeps its files.

Test: `uv run pytest -q tests/integration/test_pipeline_fetch.py`

## Step 10: Tests for the untested fetch branches (R-5.1, R-5.6) `[worker]`

Files: `tests/integration/test_pipeline_fetch.py`, `tests/integration/test_fetchers_portal.py`, `src/invoicebot/pipeline/fetch.py`.

- `ok` without a document the flow requires → `missing_docs`, recorded and chained to the next fetcher.
- The helper reports only a PDF → `missing_docs`, files removed.
- `_done` is unreachable (the `ok` attempt and `DELIVERING` commit together): remove it and its call.

Test: `uv run pytest -q tests/integration/test_pipeline_fetch.py tests/integration/test_fetchers_portal.py`

## Step 11: Portal config defaults (R-5.8) `[worker:low]`

Files: `src/invoicebot/config/models.py`, `src/invoicebot/config/checks.py`, `tests/unit/test_config_checks.py`.

- `Portal.status` defaults to `"todo"`, so a half-written file is never live.
- `checks.py`: an `active` portal whose `url` contains `{` is refused at startup (`shortcut:` URL templates such as `{ruc_emisor}` come with the first portal that needs one).
- Tests (R-5.8): a portal without `status` is `todo`; an active portal with `{` in its URL is refused.

Test: `uv run pytest -q tests/unit/test_config_models.py tests/unit/test_config_checks.py`

## Step 12: The portal fetch has its own timeout (R-5.8) `[worker:low]`

Files: `src/invoicebot/config/models.py`, `config/example.yaml`, `src/invoicebot/app.py`, `src/invoicebot/cli.py`.

- New key `retrieval.portal_timeout_s` (`Field(gt=0)`; `example.yaml`: 120), used by `app.py` and `cli.py` instead of `extraction.helper_timeout_s`, which is sized for OCR.
- Test (R-5.8): add to `tests/unit/test_config_models.py` that the key is required and positive.

Test: `uv run pytest -q tests/unit/test_config_models.py tests/unit/test_cli.py`

## Step 13: One home for retry delays (D65) `[worker]`

Files: `src/invoicebot/config/models.py`, `src/invoicebot/fetchers/chain.py`, `src/invoicebot/pipeline/fetch.py`.

- `Portal.retry_schedule` is parsed to `timedelta` once in its validator through `chain.parse_delay` (or a delay parser moved to `config/`, whichever keeps `config` a leaf); remove the second format definition.
- The chain decision returns the next delay in `Wait.delay`, and `fetch.py` uses it; remove `_next_delay` and `_not_found_count`.
- No `FETCH_WAITING` message when the schedule is empty.
- Existing tests in `tests/unit/test_fetchers_chain.py` and `tests/integration/test_pipeline_fetch.py` must pass unchanged, apart from the delay type.

Test: `uv run pytest -q tests/unit/test_fetchers_chain.py tests/integration/test_pipeline_fetch.py tests/unit/test_config_models.py`

## Step 14: Guard and template hardening from the audit (R-5.10) `[worker:low]`

Files: `src/invoicebot/net/guard.py`, `src/invoicebot/fetchers/browser/form.py`, `tests/unit/test_net_guard.py`.

- `_is_blocked`: decode the IPv4 embedded in NAT64 (`64:ff9b::/96`) and 6to4 (`2002::/16`) addresses and check it, as `ipv4_mapped` is.
- `form.py`: `pick` and `calendar` selector templates may name only `tipo_label` and `fecha` (values from config or a date), never OCR-derived fields.
- Tests (R-5.10): `64:ff9b::7f00:1` and `2002:7f00:1::` are blocked; a public NAT64 address is allowed. (The selector rule's test goes with the existing render tests in `tests/integration/test_browser_form.py` if a unit home is missing.)

Test: `uv run pytest -q tests/unit/test_net_guard.py tests/integration/test_browser_form.py`

## Step 15: Carry the audit's ops items to the roadmap `[inline: roadmap wording for the owner]`

Files: `docs/plan-mvp.md`.

- `ops` gate: Chromium runs with `--no-sandbox` (Playwright's default); before live fetching on a shared host, enable `chromium_sandbox=True` with user namespaces, or run the helper as its own uid or container.
- `ops`: a test that kills the browser helper mid-fetch and finds no `chrome-headless-shell` left (Playwright starts it detached).
- `ops`: a hostile page can start many downloads into Playwright's artifacts folder before the timeout; cap the folder or the helper's disk.

Test: `npx prettier --check docs/plan-mvp.md`

## Step 16: Live run on La Leña through the proxy (M2) `[inline: live portal with a private invoice]`

Files: none in the repo, unless a fix is needed (then that fix and its test).

- `invoicebot portal-test facturactiva-la-lena --sample <private sample>`: `ok`, both files, the XML matches; a made-up número: `not_found_yet`. Add the proxy's peak memory to `specs/spikes/browser-memory.md` if it moved.

Done when both runs give the expected reasons and the fetch phase's finish runs again on the branch.
