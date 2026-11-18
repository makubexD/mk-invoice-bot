# Plan: MVP phase 9h · harden-live

Roadmap: `docs/plan-mvp.md` row 9h. It runs while Meta verification is pending, before 9d2 `live-chat`. It fixes the items the roadmap carries from the `ops-reload` review: two that matter once chats are live, and the cheap "Later" ones.

**Goal:**

- A `SIGHUP` with a config that needs a secret the store lacks is refused with BR-O05, like any other bad config, instead of restarting into a crash loop.
- Closing a gap's GitHub issue is idempotent, and only acts on an issue in the configured repo.
- The padrón download has a total deadline.
- The JSON log never writes a value under a key that names personal data.
- A photo that runs the extract helper out of memory answers unreadable (BR-E01), as a PDF already does.

**Out of scope:** the housekeeping race (a FAILED invoice retried between the state read and the folder removal). The window is narrow; it stays in the roadmap as "Later".

**Needs from the owner:** nothing.

## Step 1: A reload checks the secrets the candidate config needs `[worker]`

Files: `src/invoicebot/app.py`, `tests/unit/test_app_wiring.py`.

Today `_start_config_reload` passes `partial(load_runtime_config, settings)` as the reload's `load`. That checks the config files only. A candidate that turns on a feature whose secret is missing is accepted, and the restart then fails at startup (py-security-auditor, D155).

1. Add `_secret_problem(cfg: Config, directory: StoreDirectory, store: SecretStore) -> str | None` to `app.py`. It returns the first problem code, or `None`, in this order:
   - `"secrets:whatsapp"` when `_whatsapp_channel(cfg)` is `None`, or `store.scoped(WHATSAPP_ADAPTER, set(WHATSAPP_SECRETS))` is not `enabled`;
   - `"secrets:sunat_validez"` when `_validation_secrets(cfg, store)` is not `None` and not `enabled`;
   - `"secrets:google"` when `cfg.google` is not `None` and `_google_secrets(store)` is not `enabled`;
   - `"secrets:github"` when `cfg.gaps.github.enabled` and the GitHub App key's view is not `enabled` (added in the finish review);
   - `"secrets:laptop"` when a flow lists `laptop_outlook` and the agent key's view is not `enabled` (added in the finish review);
   - `"secrets:sunat_sol"` when the SOL site is active (the same condition `_sol_fetcher` uses: `cfg.sunat.sol.enabled`, `directory.sunat_sol` set and `status == "active"`) and either the scoped SOL view is not `enabled` (startup fails on missing keys too, `SunatSolFetcher.__init__`) or `sol_ruc` does not match `cfg.owner.ruc` (`hmac.compare_digest`, as in `_sol_fetcher`).

   Its docstring cites R-9.6, R-9.9, R-9.10 and D155, and says that startup keeps its own fail-closed checks. Reuse the existing constants and helpers; repeat no secret key names.

2. Add `_check_reload_candidate(settings: Settings) -> RuntimeConfig`. It calls `load_runtime_config(settings)`, then `_load_secrets(settings)`. If that raises `OSError`, `ValueError` or `pyrage.DecryptError`, it raises `ConfigRejected("secrets")` from the error. If `_secret_problem(...)` returns a code, it raises `ConfigRejected(code)`. Otherwise it returns the runtime config.
3. In `_start_config_reload`, pass `partial(_check_reload_candidate, settings)` instead of `partial(load_runtime_config, settings)`.

Tests in `tests/unit/test_app_wiring.py`. Build each store with `SecretStore({...})` holding synthetic values, and each config from the example config the file already loads. Cases:

- every WhatsApp key present, validation off, no Google, SOL not active → `None`;
- `whatsapp_token` missing → `"secrets:whatsapp"`;
- validation on, without `sunat.SECRETS` → `"secrets:sunat_validez"`;
- `google` set, without `google_client_secret` → `"secrets:google"`;
- active SOL site whose `sol_ruc` differs from `owner.ruc` → `"secrets:sunat_sol"`;
- `_check_reload_candidate` with a `secrets_path` that does not exist → `ConfigRejected` with reason `"secrets"`.

Every docstring starts with `R-9.6`.

Test: `uv run pytest -q tests/unit/test_app_wiring.py tests/unit/test_reload.py tests/integration/test_app_reload.py`

## Step 2: Closing an issue is idempotent and stays in the configured repo `[worker]`

Files: `src/invoicebot/github/transport.py`, `tests/unit/test_github_transport.py`.

Today `_close_issue` posts `CLOSE_COMMENT` and then patches the issue closed. A PATCH that fails with a 5xx is retried, and the retry posts the comment again. It also takes only the issue number from the URL, so after `gaps.github.repo` changes it would close issue #N in the new repo (py-code-reviewer).

1. In `_transport`'s `close_issue` branch, before the number is parsed: if the payload's `issue_url` does not start with `f"https://github.com/{client.repo}/issues/"`, raise `PermanentSendError("github issue url is not in the configured repo")`, with no request made.
2. `_close_issue` first GETs `{_api_url(client)}/issues/{number}` through `_send`, and reads its body with `_json`. If the body is a dict whose `state` is `"closed"`, it returns without a write. Otherwise it PATCHes `{"state": "closed"}` and then POSTs `CLOSE_COMMENT`. A retry after the PATCH therefore finds the issue closed and writes nothing. Update the docstring to say this (R-11.9). Note: a comment POST that fails after a successful PATCH is not retried; the issue is closed, which is what matters.

Tests, in the same file (update `test_close_issue_posts_the_close_comment_then_closes_the_issue` for the new order). Cases:

- open issue → the requests are GET, PATCH `{"state": "closed"}`, then POST `{"body": CLOSE_COMMENT}`;
- the GET answers `state: closed` → only the GET is made, and the row succeeds;
- an `issue_url` under `https://github.com/other/repo/issues/42` → `PermanentSendError`, with no request;
- the PATCH answers 502 → `SendLater`, and no comment POST was made;
- the existing non-numeric last segment case still raises `PermanentSendError`.

Docstrings start with `R-11.9`.

Test: `uv run pytest -q tests/unit/test_github_transport.py`

## Step 3: The padrón download has a total deadline `[worker]`

Files: `src/invoicebot/config/models.py`, `src/invoicebot/ops/padron_refresh.py`, `tests/integration/test_ops_padron_refresh.py`.

The client's 30 s timeout covers each read, not the whole download. A slow sender could keep the weekly task busy for hours while staying under the size cap (py-security-auditor).

1. `Padron` gets `download_timeout_s: float = Field(1800, gt=0)`, with a field docstring or comment: the whole download's deadline in seconds (S12.4).
2. In `refresh_padron`, wrap the `_download(...)` call in `async with asyncio.timeout(padron_cfg.download_timeout_s):`. Turn a `TimeoutError` from it into `PadronRefreshError("timeout")`. Add `"timeout"` to `PadronReason`, and mention it in the module docstring. The `finally` that deletes the ZIP is unchanged.

Test (new, same file): a `MockTransport` whose response streams an async iterator that yields one chunk, then sleeps 1 s, then yields another. With `_cfg(download_timeout_s=0.05)`, `refresh_padron` raises `PadronRefreshError` with reason `"timeout"`, no ZIP is left in `tmp_path / "padron"`, and an earlier imported row is still found. The docstring starts with `S12.4`.

Test: `uv run pytest -q tests/integration/test_ops_padron_refresh.py tests/unit/test_config_models.py`

## Step 4: The JSON log drops values under personal-data keys `[worker]`

Files: `src/invoicebot/logging.py`, `tests/unit/test_logging.py`.

Since 9e, the formatter writes every `extra=` key. Secret redaction still applies, but sender-id hashing is only a convention, so a future `extra={"sender": ...}` would be logged as it is (py-security-auditor).

1. Add `_PERSONAL_KEYS = frozenset({"sender", "sender_id", "phone", "wa_id", "ruc", "razon_social", "email", "name"})`, with a comment citing R-9.5 and R-9.12: sender ids are logged only as a hash, and no personal data is logged.
2. In the formatter's loop over `record.__dict__`, a key in `_PERSONAL_KEYS` is written with the value `"[redacted]"` instead of its value.
3. Before finishing, run `grep -rn "extra=" src` and confirm that no current call uses one of these keys. Report the result in the summary.

Tests (same file):

- `logger.info("x", extra={"sender": "51999999999"})` writes `"sender": "[redacted]"`, and the line does not contain `51999999999`;
- `extra={"reason": "status"}` is still written as is.

Docstrings start with `R-9.12`.

Test: `uv run pytest -q tests/unit/test_logging.py`

## Step 5: A photo that runs out of memory answers unreadable `[worker]`

Files: `src/invoicebot/extract/__main__.py`, `tests/unit/test_extract_qr.py`.

`_read_pdf` turns a `MemoryError` into `_unreadable(ocr=False)`. `_read_photo` does not, so the helper exits non-zero and the job retries until it is parked, instead of answering BR-E01 (py-security-auditor).

1. In `_read_photo`, add `MemoryError` to the `except` around `load_gray` / `read_invoice_qr` / `preprocess`.
2. Wrap `read_text(...)` and `parse_ocr(...)` so that a `MemoryError` there also returns `_unreadable(ocr=False)`. Keep the function within the lint complexity cap; a small private helper is fine.

Tests (same file, following the existing `monkeypatch.setattr(helper, "load_gray", explode)` pattern):

- `load_gray` raising `MemoryError` → the result is the unreadable payload;
- `read_text` raising `MemoryError` → the result is the unreadable payload.

Docstrings start with `R-2.1` and `BR-E01`.

Test: `uv run pytest -q tests/unit/test_extract_qr.py tests/unit/test_extract_pdf.py`
