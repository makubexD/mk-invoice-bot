# Plan: MVP phase 4 · fetch (M2)

Roadmap: `docs/plan-mvp.md` row 4 and its carried-over items. Backlog: `specs/04-tasks.md` S7.1, S7.6, S10.1, S10.2, S10.5, S10.8, S10.9, S0.9. Requirements: R-5.1, R-5.6, R-5.7, R-5.8, R-5.10, R-9.1, R-9.10, R-9.16. Rules: BR-F06, BR-F07, BR-F08, BR-F09. Design: `specs/03-design.md` §4, §6, §8.

**Goal (M2):** a La Leña invoice in `FETCHING_DOCS` gets its portal from the store directory, the bot downloads the PDF and the XML, checks the XML matches the invoice (R-5.6) and moves it to `DELIVERING`. A portal's "not found yet" is retried on the portal's schedule (D65). When every fetcher has failed, the invoice goes to `MANUAL_REQUIRED` with BR-F07.

**Needs from the owner first:**

- The private samples folder: `2026-10-Support_1_LaLena_Sent` (its PDF and XML) for steps 3 and 19.
- Network access to `comprobantes.facturactiva.com` from this machine.
- **A decision to confirm in review (step 4 records it as D82):** the browser runs in a helper process per fetch, like the image helper (D78), and not as a pool that lives in the bot process. See "Design choices".

## Design choices

These settle places where spec 03 is ambiguous or ahead of what this phase needs. Step 4 writes them into the specs.

- **D82, the browser lives in a helper process.** Spec 03 §8 names Playwright among the heavy libraries kept out of the bot process, and also describes a `BrowserPool(max=1, idle_close=60s)`. A helper per fetch (`python -m invoicebot.fetchers.browser`) meets S10.1's acceptance ("Chromium is never resident while idle") with no pool, idle timer or lifecycle code: Chromium and the Playwright driver exit with the helper. The cost is a cold start per fetch (measured in step 2). The captcha wait of S10.4 (`widen-b`) runs inside the helper, which then lives up to `ask_user_timeout_s`; that design is `widen-b`'s.
- **Chain position comes from `fetch_attempts`.** No new column. For each fetcher in the flow's order, its attempts on this invoice decide: an attempt with `ok` ends the chain; a final failure moves on; `not_found_yet` attempts up to the length of the portal's `retry_schedule` mean "wait"; one more means the fetcher is finished. A technical error is a final failure for this invoice (spec 03 §6), not a queue retry.
- **Fetchers not built yet are skipped.** The flow lists `qr_url`, `portal_registry`, `sunat_sol`, `email_inbox`, `manual`. Only `portal_registry` is registered in this phase. The chain skips a name that is not in the registry, with no attempt row (`shortcut:` until `widen-b` registers the rest). `manual` is not a fetcher: it is the end of the chain.
- **Moved out of this phase (step 4 records it):**
  - S7.6's `httpx` transport moves to `widen-b` with S7.2 `qr_url`, its first caller. This phase builds the guard's checks and the Chromium side.
  - S10.8's brand-by-serie-prefix and its one-tap question move to `file-b`, where `comercio` is first used (the folder name). `SIGHUP` reload moves to `ops`.
  - The `document_url` host allowlist moves to `widen-b` with `qr_url`.
  - The helper sandbox (separate user, no network) and the cgroup memory cap move to `ops`: they are host settings, and the host is not shared before `ops`.
- **Portal file additions:** `not_found` (the selector or text the site shows for an unknown invoice), `allow_http` (D76, default `false`) and `retry_schedule` (default `["1h", "6h", "24h", "72h"]`, D65). The key of a portal is its file name.

Conventions: everything in `docs/plan-mvp-read.md` "Conventions" still applies. In addition:

- Playwright and Chromium load only in the browser helper. `.importlinter`'s `heavy-in-helper-only` contract gains `playwright`.
- Browser tests serve synthetic HTML through `page.route(...).fulfill`, never a real site, and live in `tests/integration/` (they need Chromium and fail, not skip, without it).
- No page text, field value or URL query is logged; a fetch logs the fetcher, the portal key, the reason code and the duration (R-9.16).
- Lint for every step: `uv run ruff check src tests scripts migrations && uv run ruff format --check src tests scripts migrations && uv run mypy src && uv run lint-imports`.

Out of scope here: `qr_url`, `email_inbox`, `sunat_sol`, captchas, the full BR-F07 list and accepting sender files (S7.4, S7.5), gaps and BR-F02/BR-F03, and delivery.

## Step 1: Add Playwright and its Chromium `[inline: dependency vetting through py-add-package]`

Files: `pyproject.toml`, `uv.lock`, `deploy/Dockerfile`, `.github/workflows/ci.yml`, `deploy/README.md`, `.importlinter`.

- Add `playwright` (pinned) with the `py-add-package` skill.
- Install the headless shell only: `uv run playwright install --only-shell --with-deps chromium` in the runtime image and in the CI job that runs pytest. The README names the command and its check.
- `.importlinter`: add `playwright` to `heavy-in-helper-only`, allowed only in `invoicebot.fetchers.browser`.

Test: `uv run python -c "import playwright.async_api" && uv run playwright install --only-shell chromium && uv run lint-imports`

## Step 2: Memory spike, Chromium with Postgres under 2 GB (S0.9) `[inline: measurement on this machine]`

Files: `specs/spikes/browser-memory.md` only. The script is throwaway and stays outside the repo.

- In a podman container limited to 2 GB, with Postgres running beside it, launch the headless shell through Playwright with spec 03 §8's flags and resource blocking, and open the La Leña lookup page.
- Record: peak RSS of the helper plus Chromium, the cold start to "page loaded", and the same with images on (captcha pages, later).
- **Gate:** if the helper's peak is over 450 MB (R-9.1 leaves 800 MB for the app, minus the bot's 350 MB steady state), stop the plan and report.

Done when `specs/spikes/browser-memory.md` is committed with the numbers, or the gate stops the plan.

## Step 3: La Leña's portal file (S10.2 seed) `[inline: live portal with a private invoice]`

Files: `config/portals/facturactiva-la-lena.yaml`, `specs/spikes/portals.md`.

- With the Oct 2026 La Leña invoice from the private folder, use the site by hand through Playwright's inspector and write the file: `fields` (selector → expression), `submit`, `downloads` (`pdf`, `xml`), `not_found`, `retry_schedule`, and `status: active`.
- Try a made-up número too, to record what "not found" looks like.
- `specs/spikes/portals.md`: the selectors and how "not found" shows, with no invoice values.

Done when the file holds every key step 9 reads, and `uv run python scripts/check_config.py` passes once step 6 lands (until then, the YAML loads with `yaml.safe_load`).

## Step 4: Record the design choices in the specs `[inline: decision text across specs]`

Files: `specs/05-open-questions.md`, `specs/03-design.md`, `specs/04-tasks.md`, `docs/plan-mvp.md`.

- Spec 05: D82 as in "Design choices", plus the S0.9 numbers.
- Spec 03 §6 and §8: the browser helper instead of `BrowserPool`; chain position from `fetch_attempts`; the three portal keys added.
- Spec 04: S10.1's text follows D82; S7.6's `httpx` part and the `document_url` allowlist go with S7.2 in `widen-b`; S10.8's brand choice goes to `file-b` and its reload to `ops`.
- Roadmap: move the carried-over items as listed in "Design choices", and mark the `ocr` and `read` review items this plan takes (steps 12, 17 and 18).

Done when `grep -n "BrowserPool" specs/03-design.md` names it only as replaced by D82.

## Step 5: Store directory models and loader (S10.8) `[worker]`

Files: `src/invoicebot/config/models.py`, `src/invoicebot/config/loader.py`, `tests/unit/test_config_models.py`.

- Replace `Portal` and `PortalMatch` with the directory shape (spec 03 §6):
  - `Store`: `ruc` (11 digits), `razon_social`, `tipo`, `brands` (each `comercio` and an optional `serie_prefix` list), `portal: str | None` (a key, `manual`, or `null`), `notes`.
  - `Portal`: `key` (set from the file name), `adapter`, `url`, `status` (`active | todo | broken`), `fields`, `captcha`, `submit`, `downloads`, `not_found: str | None`, `allow_http: bool = False`, `retry_schedule: list[str]` with the D65 default. Each `retry_schedule` entry must match `^\d+[mhd]$`.
  - `StoreDirectory`: frozen; `stores: dict[str, Store]` keyed by RUC, `portals: dict[str, Portal]` keyed by key.
- `load_directory(config_dir: Path) -> StoreDirectory` reads `config_dir/stores/*.yaml` and `config_dir/portals/*.yaml` with `yaml.safe_load`. It raises `ValueError` naming the file when a store's `ruc` differs from its file name or a file's root is not a mapping. Remove `load_portals`.
- Tests, each citing R-5.8:
  - the repo's own `config/` loads, and the La Leña store resolves to the `facturactiva-la-lena` portal;
  - a store file whose `ruc` differs from its name → `ValueError`;
  - a portal with no `retry_schedule` gets `["1h", "6h", "24h", "72h"]`;
  - a `retry_schedule` entry `"2w"` → validation error;
  - a list at a file's root → `ValueError`.

Test: `uv run pytest -q tests/unit/test_config_models.py`

## Step 6: Directory checks and `check_config.py` (S10.8) `[worker]`

Files: `src/invoicebot/config/checks.py`, `scripts/check_config.py`, `tests/unit/test_config_checks.py`.

- `run_checks(cfg, directory, *, pr_mode)` takes the `StoreDirectory`. New problems:
  - a store's `portal` names no portal file and is not `manual` or `null`;
  - two `serie_prefix` lists in one store overlap;
  - a portal with `adapter: generic_form` and `status: active` lacks `url`, `submit`, `downloads` or `not_found`.
- The existing todo/broken (`--pr`) and adapter-package checks keep working on the new `Portal`.
- `scripts/check_config.py` loads the directory from the config file's folder and no longer reads `portals.yaml`.
- Tests (R-5.8): one per new problem; the repo's `config/` passes; a todo portal is a problem only with `pr_mode=True`.

Test: `uv run pytest -q tests/unit/test_config_checks.py && uv run python scripts/check_config.py`

## Step 7: Request guard checks (S7.6, R-5.10) `[worker]`

Files: `src/invoicebot/net/__init__.py`, `src/invoicebot/net/guard.py`, `tests/unit/test_net_guard.py`, `.importlinter`.

- `GuardReason(StrEnum)`: `scheme_blocked`, `http_not_allowed`, `private_address`, `unresolvable`, `bad_url`. Its values are the codes BR-F08 records.
- `check_url(url: str, http_hosts: frozenset[str]) -> GuardReason | None`: `https` passes; `http` passes only when the host is in `http_hosts` (D76); `file:`, `data:`, `javascript:` and any other scheme are `scheme_blocked`; a URL with credentials or no host is `bad_url`.
- `check_addresses(addresses: Iterable[str]) -> GuardReason | None`: `private_address` when any address is private, loopback, link-local, multicast, reserved, unspecified or in CGNAT `100.64.0.0/10`, for IPv4 and IPv6 (including IPv4-mapped IPv6).
- `async resolve_and_check(host: str, resolver: Resolver) -> tuple[str, GuardReason | None]`: resolves once through the injected `Resolver` port and returns the first address to pin, or the reason. An empty answer is `unresolvable`.
- `.importlinter`: `invoicebot.net` is a leaf (imports nothing else from the app), added to `zero-consumer`.
- Table-driven tests (R-5.10, BR-F08): each scheme; `http` with and without the host listed; `127.0.0.1`, `10.0.0.1`, `169.254.169.254`, `100.64.0.1`, `::1`, `::ffff:127.0.0.1`, `fe80::1` → `private_address`; `93.184.216.34` passes; a resolver answering one public and one private address → `private_address` (DNS rebinding); an empty answer → `unresolvable`; `https://user:pw@host/` → `bad_url`.

Test: `uv run pytest -q tests/unit/test_net_guard.py && uv run lint-imports`

## Step 8: Fetch types and the chain decision (S7.1, S10.9) `[worker]`

Files: `src/invoicebot/fetchers/__init__.py`, `src/invoicebot/fetchers/base.py`, `src/invoicebot/fetchers/chain.py`, `tests/unit/test_fetchers_chain.py`.

- `base.py`:
  - `FetchReason(StrEnum)`: `ok`, `not_found_yet`, `no_portal`, `portal_inactive`, `technical`, `timeout`, `guard_blocked`, `xml_invalid`, `xml_mismatch`, `missing_docs`.
  - `FetchedFile` (frozen: `kind` `pdf | xml`, `path`, `size`, `sha256`) and `FetchResult` (frozen: `reason`, `files: tuple[FetchedFile, ...]`, `portal_key: str | None`). `ok` is a property: `reason is FetchReason.OK`.
  - `Fetcher` Protocol: `async def fetch(self, invoice: FetchInvoice, out_dir: Path) -> FetchResult`, with `FetchInvoice` a frozen dataclass of the fields a portal needs (`ruc_emisor`, `tipo`, `serie`, `numero`, `fecha`, `total`). The docstring states the invariant: a fetcher never raises for an expected outcome, and returns a reason code, never page text.
- `chain.py`, pure:
  - `Attempt` (frozen: `fetcher`, `reason`).
  - `Decision`: one of `Run(fetcher)`, `Wait(delay: timedelta, fetcher)`, `Done(fetcher)` and `Exhausted()`.
  - `decide(order: Sequence[str], registered: Collection[str], attempts: Sequence[Attempt], schedule_for: Callable[[str], Sequence[timedelta]]) -> Decision`. It holds no clock: `Wait` only says how long to wait after the latest `not_found_yet`, and the queue's `run_after` holds the time.
  - `parse_delay("1h") -> timedelta` for `m`, `h` and `d`.
- Tests (R-5.1, D65), with order `[qr_url, portal_registry, email_inbox]` and only `portal_registry` registered:
  - no attempts → `Run(portal_registry)`;
  - one `ok` attempt → `Done(portal_registry)`;
  - one `not_found_yet` with schedule `[1h, 6h]` → `Wait(1h)`; two → `Wait(6h)`; three → `Exhausted()`;
  - one `technical` attempt → `Exhausted()` (a technical error is final, not retried);
  - one `xml_mismatch` → `Exhausted()`;
  - with `email_inbox` also registered, one `technical` on `portal_registry` → `Run(email_inbox)`;
  - an empty order → `Exhausted()`;
  - `parse_delay("90m") == timedelta(minutes=90)`; `parse_delay("2w")` raises `ValueError`.

Test: `uv run pytest -q tests/unit/test_fetchers_chain.py`

## Step 9: Fill a portal form in a page (S10.2) `[worker]`

Files: `src/invoicebot/fetchers/browser/__init__.py`, `src/invoicebot/fetchers/browser/form.py`, `tests/integration/test_browser_form.py`.

- `render_fields(fields, invoice: FetchInvoice) -> list[FieldAction]`: each value is a template over `ruc_emisor`, `tipo`, `serie`, `numero`, `fecha`, `total` with a format spec (`{fecha:%d/%m/%Y}`, `{total:.2f}`, `{numero}`), or `{select: "<template>"}` for a `<select>`. Use `string.Formatter` with only those names; an unknown name or attribute access (`{x.y}`, `{x[0]}`) raises `ValueError`.
- `async fill_and_download(page, portal, invoice, out_dir, max_bytes) -> FetchResult`:
  - goes to `portal.url`, fills each field, clicks `submit`;
  - waits for either a download control or `not_found`, up to the page timeout; `not_found` → `not_found_yet`;
  - clicks each `downloads` control under `page.expect_download()` and saves it to `out_dir/<kind>`, refusing a file over its `max_bytes` kind limit (`technical`);
  - a missing control or a Playwright timeout → `technical` / `timeout`; never raises for these.
- Integration tests serve synthetic HTML (a form, a results page with PDF and XML links, a "no encontrado" page) through `page.route(...).fulfill` in a launched headless shell (R-5.8):
  - the fields render as the portal file says (`fecha` as `dd/mm/yyyy`, `total` with two decimals, a `select`);
  - a match downloads both files with their sizes and SHA-256;
  - the not-found page → `not_found_yet`;
  - a page with no XML control → `technical`;
  - a 3 MB XML with a 2 MB limit → `technical`, no file left in `out_dir`;
  - `{fecha.year}` in a portal field → `ValueError` from `render_fields`.

Test: `uv run pytest -q tests/integration/test_browser_form.py`

## Step 10: The browser helper process (S10.1, S7.6 Chromium side, D82) `[worker]`

Files: `src/invoicebot/fetchers/browser/__main__.py`, `src/invoicebot/fetchers/browser/routing.py`, `tests/integration/test_browser_helper.py`.

- `routing.py`: `async on_route(route, http_hosts, resolver)` for `context.route("**/*", ...)`:
  - aborts fonts, media and known analytics hosts (spec 03 §8), and images unless the portal has a captcha;
  - runs `check_url` and `resolve_and_check` (step 7) on every request, redirects included, and aborts with `blockedbyclient` on a reason, remembering the first reason.
  - `shortcut:` Chromium resolves the host again after the check, so DNS rebinding is closed by the container's egress rule (spec 03 §6), not here; noted for `ops`.
- `__main__.py`: reads one JSON request from stdin (portal, invoice fields, `out_dir`, size limits, page timeout), sets rlimits like the image helper (`RLIMIT_AS`, `RLIMIT_CPU`, `RLIMIT_FSIZE` from the request's largest file limit), launches the headless shell with spec 03 §8's flags, one context, runs `fill_and_download`, and prints one JSON result (`reason`, `files`). A guard block returns `guard_blocked`. Any unexpected error prints `technical` and exits 0; nothing from the page is printed.
- Integration tests (R-5.10, R-9.1):
  - `on_route` with a request to `http://127.0.0.1/` → aborted, reason `private_address`; to a `file:` URL → `scheme_blocked`; to a font → aborted with no reason recorded;
  - the helper run as a subprocess against a portal whose `url` is `http://127.0.0.1:1/` → prints `guard_blocked`, exit 0;
  - malformed stdin → `technical`, exit 0.

Test: `uv run pytest -q tests/integration/test_browser_helper.py`

## Step 11: Share the helper runner `[worker:low]`

Files: `src/invoicebot/pipeline/helper.py`, `src/invoicebot/pipeline/extract.py`, `tests/integration/test_pipeline_extract.py`.

- Move `_run_helper`'s process handling from `pipeline/extract.py` into `pipeline/helper.py` as `run_helper(argv, stdin: bytes, env, timeout_s, max_output_bytes) -> bytes`: new session, kill and reap the process group on every exit, cap the output, raise `TimeoutError` or `ValueError` as today. `extract.py` keeps its argv, env and result parsing and calls it.
- No behavior change: the existing extract tests pass unchanged.

Test: `uv run pytest -q tests/integration/test_pipeline_extract.py`

## Step 12: The `portal_registry` fetcher (S10.2, R-5.6, BR-F06, BR-F09) `[worker]`

Files: `src/invoicebot/fetchers/portal.py`, `src/invoicebot/extract/ubl.py`, `tests/integration/test_fetchers_portal.py`.

- `PortalFetcher(directory, limits, helper_timeout_s)` implements `Fetcher`:
  - looks up the store by `ruc_emisor`; no store or `portal: null` → `no_portal`; `portal: manual` → `no_portal`; a portal not `active` → `portal_inactive`;
  - runs the browser helper through `run_helper` with the portal's `allow_http` host; a `TimeoutError` → `timeout`;
  - parses the XML with `parse_invoice_xml` (`ValueError` → `xml_invalid`, BR-F09) and checks it with `matches_invoice` (`False` → `xml_mismatch`, BR-F06); either deletes the downloaded files.
- `ubl.py` (carried from the `ocr` review, now that a fetched XML is a caller): `parse_invoice_xml` refuses input that does not start with `<` after an optional UTF-8 BOM and whitespace.
- Integration tests point the helper command at a fake script (no browser) that prints a chosen result and writes chosen files (R-5.6):
  - a matching XML and a PDF → `ok` with both files;
  - an XML for another número → `xml_mismatch`, files removed;
  - an XML with a DOCTYPE → `xml_invalid`;
  - a UTF-16 XML without a BOM → `xml_invalid`;
  - a RUC with no store → `no_portal`; a store with `portal: manual` → `no_portal`; a `todo` portal → `portal_inactive`;
  - a helper that sleeps past the timeout → `timeout`.

Test: `uv run pytest -q tests/integration/test_fetchers_portal.py tests/unit/test_extract_ubl.py`

## Step 13: Record fetch attempts and fetched files (S7.1, R-5.7) `[worker]`

Files: `src/invoicebot/store/fetch_attempts.py`, `tests/integration/test_fetch_attempts_store.py`, `.importlinter` (only if `store` may not yet import `invoicebot.fetchers.chain`; otherwise return plain tuples and let the handler build `Attempt`).

- `record_attempt(conn, invoice_id, fetcher, portal_key, reason, ms)`; `ok` is `reason == 'ok'`.
- `list_attempts(conn, invoice_id)` in insertion order, as `(fetcher, reason)` pairs.
- Fetched files reuse `store/invoices.py`'s existing `add_file`.
- Neither commits (the caller owns the transaction).
- Tests (R-5.7): two attempts come back in order with their reasons; an attempt for another invoice is not listed; a rollback leaves no row.

Test: `uv run pytest -q tests/integration/test_fetch_attempts_store.py`

## Step 14: The fetch job (S7.1, S10.9, BR-F07) `[worker]`

Files: `src/invoicebot/pipeline/fetch.py`, `src/invoicebot/rules/messages_es.py`, `tests/integration/test_pipeline_fetch.py`.

- `make_fetch_handler(pool, cfg, directory, registry: Mapping[str, Fetcher], data_dir) -> Handler` for step `FETCH_STEP = "fetch"`:
  - acts only on an invoice in `FETCHING_DOCS` (R-9.4); loads its reading and flow;
  - asks `decide` (step 8) with the flow's `fetchers`, the registry and the attempts; the schedule of `portal_registry` is the store's portal `retry_schedule`, or the D65 default when there is no portal;
  - `Run`: runs the fetcher into `data_dir/tmp/<invoice_id>/`; an `ok` result lacking a kind in the flow's `required_docs` becomes `missing_docs`. Then in one transaction it records the attempt, and on `ok` adds the files and moves to `DELIVERING` with no job (`file-b` adds it); on `not_found_yet` enqueues `fetch` again with `run_after = now() + delay` and, on the first one only, queues `FETCHING_DOCS`'s status line ("esperando al portal", added to `messages_es.py`'s status texts); on any other reason enqueues `fetch` again at once, so the chain moves on;
  - `Exhausted`: moves to `MANUAL_REQUIRED` with rule `BR-F07` and its message. `shortcut:` the F07 list holds one `•` line per recorded attempt that has a BR-F text (F06, F08, F09); the full list is S7.4 in `widen-b`.
  - logs `step done` with the fetcher, portal key, reason and duration only.
- Tests with a fake `Fetcher` (R-5.1, R-5.7, D65, BR-F07):
  - `ok` → `DELIVERING`, two `files` rows, one attempt, no new job;
  - `not_found_yet` → still `FETCHING_DOCS`, a `fetch` job with `run_after` about 1 h ahead, one status message; a second `not_found_yet` → no second message;
  - `not_found_yet` past the last scheduled try → `MANUAL_REQUIRED` with `BR-F07`;
  - `xml_mismatch` → next run ends `MANUAL_REQUIRED`, and the message holds the BR-F06 line;
  - an invoice in `ROUTING` → nothing changes.

Test: `uv run pytest -q tests/integration/test_pipeline_fetch.py`

## Step 15: Wire the fetch step `[worker]`

Files: `src/invoicebot/pipeline/route.py`, `src/invoicebot/app.py`, `tests/integration/test_pipeline_route.py`.

- `route.py`: a routed invoice enqueues `fetch` in the same transaction that moves it to `FETCHING_DOCS`; update the module docstring and `ROUTED_STATE`'s comment.
- `app.py`: load the store directory at startup (`load_directory` on the config folder), build the registry `{"portal_registry": PortalFetcher(...)}` and register `FETCH_STEP`.
- Tests (S4.2, R-5.1): a routed invoice has one `fetch` job; a refused one has none.

Test: `uv run pytest -q tests/integration/test_pipeline_route.py`

## Step 16: `invoicebot portal-test` (S10.5) `[worker]`

Files: `pyproject.toml` (`[project.scripts]` only), `src/invoicebot/cli.py`, `tests/unit/test_cli.py`.

- `invoicebot portal-test <key> --sample <path>`: the sample is a YAML file outside the repo with `ruc_emisor`, `tipo`, `serie`, `numero`, `fecha` and `total`. It runs the portal's helper into a temporary folder and prints the reason, each file's kind and size, and whether the XML matches. It never prints field values. Exit 0 on `ok`, 1 otherwise.
- `argparse`; `cli.py` only parses and delegates to the fetcher.
- Tests (R-5.7) with the fake helper of step 12: `ok` → exit 0 and two file lines; an unknown key → exit 2 and a message naming the key; the output holds no sample value.

Test: `uv run pytest -q tests/unit/test_cli.py`

## Step 17: Image helper hardening from the `ocr` review `[worker:low]`

Files: `src/invoicebot/extract/__main__.py`, `src/invoicebot/extract/names.py`, `tests/unit/test_extract_qr.py`.

- Move the QR variant and decoder names to `extract/names.py` (no `cv2` import) as `Literal` types, and type the helper's result fields with them (R-9.16).
- Catch `cv2.error` around image reading in the helper and report the photo as unreadable (the BR-E01 path), not a non-zero exit.
- Tests: an image that makes OpenCV raise → the unreadable result, exit 0; the result's variant and decoder accept only the listed names.

Test: `uv run pytest -q tests/unit/test_extract_qr.py`

## Step 18: Tesseract limits from the `ocr` review `[worker:low]`

Files: `src/invoicebot/extract/ocr.py`, `src/invoicebot/extract/ocr_parse.py`, `tests/unit/test_extract_ocr_parse.py`.

- Start Tesseract with `RLIMIT_FSIZE=0` (it writes only to stdout); the docstring says why.
- Cap the OCR text at a named constant (64 KB) before parsing, and the matches per parsing rule at a named constant (50).
- Tests (R-9.2): a 100 KB text is cut to the cap before parsing; a rule with 60 matches stops at 50.

Test: `uv run pytest -q tests/unit/test_extract_ocr_parse.py tests/unit/test_extract_ocr.py`

## Step 19: Live run on La Leña (M2) `[inline: live portal with a private invoice]`

Files: none in the repo, unless a fix is needed (then that fix and its test).

- `invoicebot portal-test facturactiva-la-lena --sample <private sample>` with the Oct 2026 invoice: `ok`, both files, the XML matches.
- The same with a made-up número: `not_found_yet`.
- Record the helper's peak RSS and duration in `specs/spikes/browser-memory.md`.

Done when both runs give the expected reasons. The phase is done when this passes, the step tests pass and the lint, type and import checks pass.
