# Plan: MVP phase 6e · widen-b-gaps-ops

Roadmap: `docs/plan-mvp.md` row 6e. The gap service (E8: the gap registry, the GitHub issue in the ops repo, the owner alert, the taught portal link, PORTAL_BROKEN, `{gap_line}`), the operations baseline (E15: nightly encrypted backups, retention, self-alerts), the gap leftovers (`shortcut:` logs, BR-E06, BR-R06, BR-F03), and the review leftovers carried from `widen-b-deliver`. M4 is reached when this phase ends (D105).

**Goal:** ten invoices from the same unknown store make one `gaps` row, one issue in the ops repo and one owner alert; the sender is asked once for the store's link, and a link the owner approves lands in the issue. A portal that fails three times in a row raises PORTAL_BROKEN. Every night the database and the config are dumped, encrypted to an `age` key and uploaded to the personal OneDrive, and old rows are pruned. The owner hears about a stuck outbox, a full disk, a rejected channel token and a failed backup.

**Needs from the owner first:** none of the prerequisites are done (owner, 2026-11-16), and the owner said to draft anyway. Nothing in this plan needs them for code or tests:

- S0.10 (the ops repo and the GitHub App): GitHub is tested with `httpx.MockTransport`; `gaps.github.enabled` stays `false` in `config/example.yaml`.
- The `age` recipient: tests generate a throwaway `pyrage.x25519.Identity`.
- The backup target: tests use a fake `Drive`.
- New for development: `pg_dump` and `pg_restore` (PostgreSQL client 16 or later) on the dev host, e.g. `sudo apt install postgresql-client`. The backup integration tests fail without them, as integration tests do without Postgres. Step 29 adds them to CI and the image.

The live checks are in "Deferred: live check" at the end.

## Design choices

Step 1 writes these into the specs (spec 05, D111 to D118). They are proposals for the owner's review of this plan.

- **D111 · Which E8 stories 6e takes.** The roadmap row names S8.1 to S8.3, but spec 04 also counts S8.5 and S8.7 to S8.11 as MVP, and none of them has a phase. M4 needs "an issue and an owner alert", so 6e takes S8.1, S8.2, S8.3 with S8.11 (the issue text rules, which S8.3 cannot ship without), S8.5 (the alert and the digest), S8.8 (BR-F03, already in D105) and S8.10 (`{gap_line}` in BR-F07). S8.7 (the `SIGHUP` reload and `reevaluate()`, which also closes issues) and S8.9 (`/gaps`, `/gap ignorar`, the daily App check) go to `ops`, next to the `SIGHUP` reload already moved there by the `fetch` plan. Until then the owner closes a fixed gap's issue by hand and `/reprocesar` resumes its invoices. `printed_urls` stay dropped (D101), so BR-F02 offers no suggestion.
- **D112 · One home for admins.** Admin alerts go to the profiles with `admin: true`, through `queue_admin_alert`, as today. `gaps.admins` is removed from the config model and `config/example.yaml`; nothing reads it.
- **D113 · Daily tasks at a time of day, not a cron string.** `ops.backup.cron` becomes `ops.backup.at: "03:00"` and `gaps.digest_cron` becomes `gaps.digest_at: "08:00"`, local times in `owner.timezone`. One loop in `ops/daily.py` wakes every 5 minutes and runs each task whose time has passed today and that has not run today; the last run is a row in a new `ops_state(key, last_at)` table, read and written with the database clock. A missed night runs at the next wake-up. No cron parser is added. The same table rate-limits the self-alerts (BR-O01 to BR-O04, BR-D05): at most one per key per `ops.self_alerts.realert_hours` (new, default 6).
- **D114 · Gap thresholds use the gap's own count.** `raise_gap` always upserts the row (count, `last_seen`, the linked invoice). A kind has a threshold and a window: PORTAL_MISSING, PORTAL_BROKEN and TEMPLATE_INVALID 1; PARSER_MISS 2 (BR-E06); UNROUTED_BUYER 3 within 30 days (BR-R06). When `last_seen` is older than the window, the count starts again at 1. The gap is _raised_ (issue, alert) once, when the count first reaches the threshold; a new column `gaps.raised_at` records it. No extra query over invoices is needed.
- **D115 · The alert follows the issue.** With `gaps.github.enabled`, raising a gap queues only a `github` outbox row. When the issue is created, the same transaction stores `issue_url`, sets the gap `reported` and queues the owner alert with the link. With GitHub disabled, the alert (without a link) is queued when the gap is raised. R-11.11: the daily digest task also alerts, without a link, every raised gap with no `issue_url` and no `last_alert_at` after 24 hours. A raise on a gap whose `last_alert_at` is older than `realert_days` alerts again. Digest kinds (`gaps.digest_kinds`: PARSER_MISS, UNROUTED_BUYER) are never alerted one by one; the digest lists those raised or re-alert-due since the last digest in one message.
- **D116 · GitHub writes.** `invoicebot/github/` holds the App token, the issue text and the outbox transport, and imports neither `pipeline` nor `queue` workers; what happens after an issue opens is a callback from `app.py`. The installation token is cached in memory until 5 minutes before it expires (spec 03 said `kv`; a restart only costs one more exchange). The issue text is a module constant, not `gaps/templates/gap_issue.md`. Idempotency is the hidden marker: before creating, the transport lists the ops repo's `gap` issues (all states, at most 5 pages of 100) and reuses one whose body holds `<!-- gap:<fingerprint> -->`. A write over the daily budget, a token failure (BR-D05) or a 5xx reschedules the row with a new outbox `SendLater` that does not count an attempt, so gap rows never go dead from an outage. Two operations exist: `create_issue` and `comment` (the approved taught link, D117).
- **D117 · The taught link.** BR-F02 is sent once per issuer, when the PORTAL_MISSING gap is first raised and `gaps.teach_via_chat` is on. A text from that sender is a BR-F02 answer when it is one `https` URL or "no sé" and the sender's newest outbox question is BR-F02 (the `outbox.last_rule` check D109 uses); any other text goes the usual way. `/portal <ruc> <url>` does the same from any sender. A valid link is stored in `taught_links` as `proposed` and BR-S06 asks the owner with buttons `t<ruc>:ok` and `t<ruc>:no`, handled outside `resolve_reply` like the payment buttons. [Aprobar] sets the row `approved` and the gap's `hint_url`; if the issue already exists, a `comment` row adds the link to it, else the link goes into the new issue. "Used" means the link reaches the owner's issue: the fetchers still read only the store directory.
- **D118 · The backup.** `ops/backup.py` runs `pg_dump --format=custom --exclude-table-data=kv --exclude-table-data=padron`, tars the config directory without `secrets*` and `*.age` files, encrypts each with `pyrage` to `ops.backup.age_recipient`, and uploads `db-<date>.dump.age` and `config-<date>.tar.age` to `ops.backup.target.path` on the personal OneDrive through the existing `GraphDrive`. The dump is read into memory and refused above `ops.backup.max_mb` (new, default 200) with BR-O04, since `Drive.upload` takes bytes; streaming waits until a dump nears the cap. The `pg_dump` command is an argument, so tests point it at the dev host's client. `scripts/restore_drill.sh` decrypts with the `age` identity kept off the host and restores into an empty database.

Conventions: lint is `uv run ruff check src tests scripts migrations && uv run ruff format --check src tests scripts migrations && uv run mypy src && uv run lint-imports`. Integration tests need Postgres (`podman compose -f deploy/docker-compose.yml up -d postgres`). Every new user text is a constant in `rules/messages_es.py`, quoted in spec 06 by step 1. No real RUCs, phone numbers or repo names in tests; use the synthetic values of `config/example.yaml` and `owner/mk-invoice-bot-ops`. Times come from the database clock (`now()`).

Out of scope here: S8.7 and S8.9 (to `ops`, D111), S8.4 and S8.6 (post-MVP, D44), the `SIGHUP` reload, an external uptime monitor (configured on the VPS in `ops`), streaming backups.

## Step 1: Record D111–D118 and the new texts `[inline: spec and roadmap text the owner reviews with this plan]`

Files: `specs/05-open-questions.md`, `specs/06-business-rules.md`, `specs/04-tasks.md`, `specs/03-design.md`, `docs/plan-mvp.md`.

- Spec 05: rows D111 to D118 with the text from "Design choices".
- Spec 06 §3: the owner alert texts. `GAP_admin`: `🧩 Falta configurar: {kind_desc} — {subject}.{issue_line}`, where `{issue_line}` is ` Issue: {issue_url}` or empty. `GAP_DIGEST_admin`: `🧩 Pendientes de configuración:` and one `• {kind_desc} — {subject}` line per gap. The `kind_desc` per kind: `portal de descarga desconocido`, `portal con fallas`, `plantilla inválida`, `no puedo leer los comprobantes de un emisor`, `RUC comprador sin flujo`. The subjects: `RUC {ruc_emisor}`, `{portal_key}`, `{flow}/{sink}`, `RUC {ruc_emisor}`, `RUC {num_doc_adq}`. The thresholds and windows of D114 in the table.
- Spec 06: BR-S06's buttons `[Aprobar] [Rechazar]` keep their labels; BR-F02 notes the answer forms of D117. New texts: `BR-F02_dont_know` `Gracias. Ya avisé al administrador para agregar este portal.`; `BR-S06_approved` (owner) `✅ Guardé el portal de RUC {ruc_emisor}; va en el issue.`; `BR-S06_rejected` (owner) `Descarté el portal propuesto para RUC {ruc_emisor}.`
- Spec 04: S8.1 to S8.3, S8.5, S8.8, S8.10, S8.11 cite D111, D114 to D117; S8.7 and S8.9 say "phase `ops` (D111)"; S12.3 cites D113 and D118.
- Spec 03: §13 (in-memory token, issue text constant, `SendLater`, the alert after the issue), §15 (`ops.backup.at`, `ops/daily.py`), §5 (`ops_state`, `gaps.raised_at`, `jobs.created_at`).
- Roadmap: row 6e lists "E8 (S8.1–S8.3, S8.5, S8.8, S8.10, S8.11; D111)"; row 9 `ops` adds S8.7 and S8.9.

Test: none (spec text only); `uv run pytest -q tests/unit/test_rules_parity.py` still passes.

## Step 2: Add `pyjwt[crypto]` as a direct dependency `[inline: dependency vetting through py-add-package]`

Files: `pyproject.toml`, `uv.lock`.

- Run the `py-add-package` skill for `pyjwt[crypto]` (today only transitive through `msal`). Keep the locked version unless vetting says otherwise.

Test: `uv run python -c "import jwt; jwt.encode({}, 'k', algorithm='HS256')"`.

## Step 3: Migration 0010: `ops_state`, `gaps.raised_at`, `jobs.created_at`, an attempts index `[worker:low]`

Files: `migrations/versions/0010_gaps_ops.py`, `tests/integration/test_migrations.py` (or the repo's migration test; find it with `grep -rln "0009" tests`).

- Expand only, plain SQL through `op.execute`: `CREATE TABLE ops_state (key TEXT PRIMARY KEY, last_at TIMESTAMPTZ NOT NULL)`; `ALTER TABLE gaps ADD COLUMN raised_at TIMESTAMPTZ`; `ALTER TABLE jobs ADD COLUMN created_at TIMESTAMPTZ NOT NULL DEFAULT now()`; `CREATE INDEX ix_fetch_attempts_portal_at ON fetch_attempts (portal_key, at DESC) WHERE portal_key IS NOT NULL`. `down_revision = "0009"`. The downgrade drops what the upgrade adds.
- Tests: R-9.13 upgrade to head and downgrade to 0009 both succeed; R-9.12 a job inserted without `created_at` gets `now()`.

Test: `uv run pytest -q tests/integration/test_migrations.py`.

## Step 4: Config: one admin source, daily times, the new ops keys `[worker]`

Files: `src/invoicebot/config/models.py`, `config/example.yaml`, `tests/unit/test_config_models.py` (use the real config model test file).

- `Gaps`: drop `admins` (D112); `digest_cron: str` becomes `digest_at: time`. `Backup`: `cron` becomes `at: time`; add `max_mb: int = Field(200, gt=0)`. `SelfAlerts`: add `realert_hours: int = Field(6, gt=0)`. `Gaps.digest_kinds` becomes `list[Literal["PORTAL_MISSING", "PORTAL_BROKEN", "TEMPLATE_INVALID", "PARSER_MISS", "UNROUTED_BUYER"]]`.
- `example.yaml`: `digest_at: "08:00"`, `at: "03:00"`, `max_mb: 200`, `realert_hours: 6`; remove `admins:` under `gaps`.
- Tests: R-9.6 `example.yaml` loads; R-9.6 `digest_cron` or `gaps.admins` in a config is refused (`extra="forbid"`); R-9.6 `digest_at: "25:00"` is refused; R-11.8 an unknown digest kind is refused.

Test: `uv run pytest -q tests/unit/test_config_models.py && uv run python scripts/check_config.py`.

## Step 5: Outbox `SendLater` `[worker]`

Files: `src/invoicebot/queue/outbox.py`, `tests/integration/test_outbox.py`.

- `class SendLater(Exception)` with a `delay: timedelta` attribute and a short `reason: str`. When a transport raises it, `OutboxSender` sets the row's `run_after = now() + delay` and `last_error = reason`, and does not increment `attempts`. `PermanentSendError` and other errors keep their behavior.
- Tests: R-11.10 a transport raising `SendLater(timedelta(hours=1), "budget")` leaves the row `pending`, `attempts` unchanged, `run_after` about one hour ahead; R-11.10 the row is not picked again before `run_after`; R-11.10 a row raising `SendLater` 20 times never goes `dead`.

Test: `uv run pytest -q tests/integration/test_outbox.py -k send_later`.

## Step 6: Gap kinds `[worker:low]`

Files: `src/invoicebot/gaps/__init__.py`, `src/invoicebot/gaps/kinds.py`, `tests/unit/test_gap_kinds.py`.

- A package `invoicebot/gaps/` with no imports from `pipeline`, `queue`, `store`, `api`, `events` (step 13 adds the contract). `kinds.py`: `GapKind` frozen dataclass `(kind, fingerprint_prefix, threshold, window_days: int | None, desc_key)` and `GAP_KINDS: Mapping[str, GapKind]` with the five kinds of spec 06 §3 and D114 (`portal_missing`, `portal_broken`, `template_invalid`, `parser_miss`, `unrouted` prefixes). `fingerprint(kind: str, *parts: str) -> str` joins the prefix and the parts with `:`, refusing a part that contains `:` or is empty (`ValueError`).
- `KIND_DESC` texts and the subject formats live in `rules/messages_es.py` (one home for user text), keyed by kind; `kinds.py` holds only the keys.
- Tests: BR-F02 `fingerprint("PORTAL_MISSING", "20100000001") == "portal_missing:20100000001"`; R-11.2 a part with `:` raises; BR-R06 UNROUTED_BUYER has threshold 3 and window 30; BR-E06 PARSER_MISS has threshold 2; R-11.2 every kind in spec 06 §3 is in `GAP_KINDS` (read the table like `test_rules_parity.py` reads rules).

Test: `uv run pytest -q tests/unit/test_gap_kinds.py`.

## Step 7: `store/gaps.py`: upsert with threshold and link `[worker]`

Files: `src/invoicebot/store/gaps.py`, `tests/integration/test_store_gaps.py`.

- `async def upsert_gap(conn, kind: GapKind, fingerprint: str, subject: str, invoice_id: int | None) -> GapRow`. One `INSERT … ON CONFLICT (fingerprint) DO UPDATE`: count + 1 and `last_seen = now()`, or count 1 and `first_seen = now()` when the kind has a window and `last_seen < now() - window`. Sets `raised_at = now()` when it is null and the new count reaches the threshold. Links the invoice in `invoice_gaps` (`ON CONFLICT DO NOTHING`). Returns a frozen `GapRow(fingerprint, kind, subject, state, count, raised_at, last_alert_at, issue_url, hint_url, raised_now: bool)`, where `raised_now` says this call set `raised_at`. The caller owns the transaction.
- Also: `mark_reported(conn, fingerprint, issue_url)`, `mark_alerted(conn, fingerprints)` (`last_alert_at = now()`), `set_hint(conn, fingerprint, url)`, `get_gap(conn, fingerprint)`, `linked_gap(conn, invoice_id, kind) -> GapRow | None`, `alert_due(conn, realert_days, kinds) -> list[GapRow]` (raised, not `ignored`, `last_alert_at` null or older than `realert_days`), `unreported_overdue(conn) -> list[GapRow]` (raised over 24 h ago, no `issue_url`, no `last_alert_at`).
- Tests (each from a clean `gaps`): R-11.2 ten upserts for one fingerprint make one row with count 10 and ten links; BR-F02 threshold 1 is `raised_now` on the first call only; BR-E06 threshold 2 is raised on the second; BR-R06 the third upsert within 30 days raises, and an upsert after 31 days (set `last_seen` back in the test) restarts at count 1 and does not raise; R-11.11 `unreported_overdue` returns a gap raised 25 h ago without issue and not one raised 1 h ago; R-11.8 `alert_due` skips an `ignored` gap and one alerted yesterday with `realert_days` 7.

Test: `uv run pytest -q tests/integration/test_store_gaps.py`.

## Step 8: Gap alert texts and the WhatsApp template slots `[worker:low]`

Files: `src/invoicebot/rules/messages_es.py`, `src/invoicebot/channels/whatsapp/transport.py`, `tests/unit/test_rules_parity.py`.

- `messages_es.py`: `GAP_admin`, `GAP_DIGEST_admin`, `KIND_DESC`, the subject formats, `BR-F02_dont_know`, `BR-S06_approved` and `BR-S06_rejected`, exactly as step 1 wrote them in spec 06.
- `TEMPLATE_SLOT_BY_RULE`: `GAP_admin`, `GAP_DIGEST_admin`, `BR-O02`, `BR-O03`, `BR-O04` and `BR-D05` map to `admin_alert`, like `BR-O01`.
- `test_rules_parity.py`: also compares the spec 06 §3 alert texts and `kind_desc` values with the constants, the way it compares rule texts. Existing checks are unchanged.
- Tests: R-11.1 the parity test passes and fails when a §3 text differs (check by editing a constant locally, then revert); R-1.10 the new keys resolve to `admin_alert`.

Test: `uv run pytest -q tests/unit/test_rules_parity.py tests/unit/test_whatsapp_transport.py` (use the real transport test file).

## Step 9: The gap issue text (S8.11) `[worker]`

Files: `src/invoicebot/github/__init__.py`, `src/invoicebot/github/issue_text.py`, `tests/unit/test_github_issue_text.py`.

- `render_issue(gap: IssueFields) -> tuple[str, str]` (title, body). `IssueFields` is a frozen dataclass of typed fields only: `kind`, `fingerprint`, `ruc_emisor: int | None`, `razon_social: str | None` (the store directory name; the padrón comes with `read-more`), `tipo: str | None`, `reasons: tuple[FetchReason, ...]`, `portal_key: str | None`, `hint_url: str | None`.
- R-11.12: the RUC must be 11 digits; the name keeps only letters, digits, spaces and `.,&-`, at most 80 characters; `@` is removed everywhere; the URL is reduced to `https://host/path` and dropped unless it matches `^https://[A-Za-z0-9.-]+(/[A-Za-z0-9._~/-]*)?$`, and is placed in a code fence. The body ends with `<!-- gap:<fingerprint> -->`. No counts, dates, OCR or sender text. The title is `gap: <kind> <subject>`.
- Snapshot tests (R-11.12): one per kind; hostile inputs (`@owner`, markdown links, `javascript:alert(1)`, a URL with `user:pass@`, `‮`, a newline in the name, a 12-digit RUC) render without any of them; the marker is the last line.

Test: `uv run pytest -q tests/unit/test_github_issue_text.py`.

## Step 10: GitHub App installation token `[worker]`

Files: `src/invoicebot/github/app_token.py`, `tests/unit/test_github_app_token.py`.

- `class GitHubApp` (long-lived cache): `__init__(http: httpx.AsyncClient, app_id: int, installation_id: int, private_key_pem: str, timeout_s: float)`. `async def token() -> str` signs an RS256 JWT (`iat` now − 60 s, `exp` now + 540 s, `iss` app id), posts `/app/installations/{id}/access_tokens`, and caches the token until 5 minutes before `expires_at`. Python's clock is acceptable here: the JWT is checked by GitHub's clock, not the database's.
- Failures raise `GitHubTokenError(reason)`, `reason` a `Literal["bad_key", "uninstalled", "no_permission", "unreachable"]`: an unparsable key → `bad_key`; 401 → `bad_key`; 404 → `uninstalled`; a token whose `permissions` lack `issues: write` → `no_permission`; a timeout, transport error or 5xx → `unreachable`. The key never appears in an exception or log.
- Tests with `httpx.MockTransport` and a test RSA key generated in the fixture: R-11.10 a token is fetched once and reused on the second call; R-11.10 a token 4 minutes from expiry is refreshed; BR-D05 401, 404, missing `issues` and a timeout map to their reasons; R-9.16 `repr` and the exception text never contain the PEM.

Test: `uv run pytest -q tests/unit/test_github_app_token.py`.

## Step 11: GitHub outbox transport `[worker]`

Files: `src/invoicebot/github/transport.py`, `tests/unit/test_github_transport.py`.

- `make_github_transport(app: TokenSource, http, repo: str, labels: list[str], budget: int, writes_today: Callable[[], Awaitable[int]], on_issue: Callable[[str, str], Awaitable[None]], on_token_error: Callable[[str], Awaitable[None]]) -> Transport`, where `TokenSource` is a one-method `Protocol` (`token()`) that `GitHubApp` satisfies. If that is more than 6 arguments, group the callbacks in a frozen `GitHubHooks` dataclass.
- The payload is `{"op": "create_issue", "fingerprint", "fields": {…IssueFields}}` or `{"op": "comment", "fingerprint", "issue_url", "hint_url"}`. Before any write: `writes_today() >= budget` → `SendLater(1 h, "budget")`; the row is tried again each hour until the next local day frees the budget.
- `create_issue`: list `GET /repos/{repo}/issues?labels=gap&state=all&per_page=100`, at most 5 pages; a body holding the fingerprint's marker means reuse its `html_url`; else `POST /repos/{repo}/issues` with the rendered title, body and labels `gap`, `gap:<kind>`. Then `await on_issue(fingerprint, html_url)` and return the issue number as the channel message id. `comment`: `POST /repos/{repo}/issues/{n}/comments` with the hint URL in a code fence, reduced as in step 9.
- `GitHubTokenError` → `await on_token_error(reason)` and `SendLater(1 h, "BR-D05:<reason>")`; a 5xx or timeout → `SendLater(15 min)`; a 4xx other than 401/403 → `PermanentSendError`; 401/403 on a write drops the cached token and raises `SendLater(15 min)`.
- Tests with `httpx.MockTransport` and a fake token source: R-11.5 a new gap creates one issue with both labels and calls `on_issue`; R-11.5 an existing issue with the marker is reused and nothing is created (a lost DB row); R-11.10 budget reached → `SendLater`, no request sent; BR-D05 a token error calls `on_token_error` and raises `SendLater`; R-11.10 a 502 raises `SendLater`; R-11.5 `comment` posts to the issue number of `issue_url`.

Test: `uv run pytest -q tests/unit/test_github_transport.py`.

## Step 12: `pipeline/gaps.py`: raise, alert, and the issue callback `[worker]`

Files: `src/invoicebot/pipeline/gaps.py`, `tests/integration/test_pipeline_gaps.py`.

- `GapContext` frozen dataclass: `profiles`, `gaps_cfg: Gaps`, `issue_fields` built by the caller. `async def raise_gap(conn, ctx: GapContext, kind: str, parts: tuple[str, ...], subject: str, invoice_id: int | None, fields: IssueFields) -> GapRow`: calls `upsert_gap`; when `raised_now` (or a re-alert is due, D115) and the gap is not `ignored`: with GitHub enabled and no `issue_url`, adds a `github` outbox row `create_issue`; with GitHub disabled and a non-digest kind, queues `GAP_admin` without a link through `queue_admin_alert` and `mark_alerted`. A digest kind queues nothing here. Never raises for a gap it cannot alert; the caller's transaction is the only one.
- `async def issue_opened(pool, profiles, gaps_cfg, fingerprint, issue_url)`: one transaction: `mark_reported`, then `GAP_admin` with ` Issue: <url>` unless the kind is a digest kind, and `mark_alerted`.
- `async def writes_today(pool, tz) -> int`: `github` outbox rows `sent` since local midnight.
- Tests: R-11.2 raising twice for one fingerprint adds one `github` row; R-11.8 GitHub disabled: one `GAP_admin` row per admin profile, the second raise adds none; R-11.8 a PARSER_MISS gap queues no alert; R-11.5 `issue_opened` sets `reported`, stores the URL and queues `GAP_admin` with the link; R-11.8 a raise 8 days after the last alert, `realert_days` 7, alerts again; R-11.2 an `ignored` gap is counted but queues nothing.

Test: `uv run pytest -q tests/integration/test_pipeline_gaps.py`.

## Step 13: Wire GitHub into `app.py`; import contracts `[worker]`

Files: `src/invoicebot/app.py`, `.importlinter`, `tests/unit/test_app_wiring.py` (or the repo's app wiring test; find it with `grep -rln "make_transport" tests`).

- With `cfg.gaps.github.enabled`, `app.py` scopes the secret `github_app_private_key` (`store.scoped("github", {"github_app_private_key"})`), builds `GitHubApp` on the shared `http` client and registers `"github": make_github_transport(...)` beside `"whatsapp"`, with `on_issue` bound to `pipeline.gaps.issue_opened` and `on_token_error` to the BR-D05 alert (step 14). Disabled: no transport and no secret read.
- `.importlinter`: `invoicebot.gaps` and `invoicebot.github` join `leaf-packages`-style contracts: `gaps` imports nothing but `config`, `fetchers.base` and the standard library; `github` must not import `api`, `events`, `pipeline`, `queue.worker`, `store`. Add both to `zero-consumer`.
- Tests: R-11.10 with GitHub disabled the transports mapping has no `github` and the secret store is not asked; with it enabled and the secret missing, startup fails with the secret's name (not its value).

Test: `uv run pytest -q tests/unit/test_app_wiring.py && uv run lint-imports`.

## Step 14: `ops_state` and the rate-limited admin alert `[worker]`

Files: `src/invoicebot/store/ops_state.py`, `src/invoicebot/pipeline/outgoing.py`, `tests/integration/test_store_ops_state.py`.

- `store/ops_state.py`: `claim(conn, key, every: timedelta) -> bool` atomically sets `last_at = now()` and returns true when the row is missing or older than `every` (`INSERT … ON CONFLICT DO UPDATE … WHERE ops_state.last_at < now() - every RETURNING key`); `last(conn, key) -> datetime | None`.
- `outgoing.py`: `queue_rate_limited_alert(conn, profiles, key, every, rule_id, text) -> bool` = `claim` then `queue_admin_alert`. BR-D05's `on_token_error` uses it with key `BR-D05` and `every` 24 h (BR-D05: at most daily) and the reason's description from `messages_es.py` (add `REASON_DESC` entries for the four reasons).
- Tests: BR-O01 two claims within `every` → the second is false; after `every` → true; two concurrent claims → exactly one true; BR-D05 two token errors in one day queue one alert per admin.

Test: `uv run pytest -q tests/integration/test_store_ops_state.py`.

## Step 15: PORTAL_MISSING at BR-F07 and `{gap_line}` (S8.10) `[worker]`

Files: `src/invoicebot/pipeline/fetch.py`, `src/invoicebot/pipeline/manual_message.py`, `tests/integration/test_pipeline_fetch.py`.

- In `_exhausted` (`pipeline/fetch.py:292`), inside its transaction and before the message is built: when the attempts include `no_portal` and the reading has no `qr_url`, call `raise_gap(…, "PORTAL_MISSING", (ruc_emisor,), …)` with `IssueFields` from the reading, the store directory name and the attempts' reasons.
- `manual_message(…, gap_line: str = "")` loses its `shortcut:`; `_exhausted` passes the spec 06 `{gap_line}` text (a new constant `GAP_LINE_PORTAL`) when a gap is linked to the invoice, else `""`. BR-F07 must not end in a trailing space when empty.
- Tests: BR-F02 an exhausted chain with `no_portal` makes one gap linked to the invoice, and the BR-F07 text ends with the gap line; R-11.3 an exhausted chain with only `not_found` makes no gap and no gap line; R-11.2 two invoices of the same issuer make one gap with two links.

Test: `uv run pytest -q tests/integration/test_pipeline_fetch.py -k "gap or exhausted"`.

## Step 16: BR-F02 asks the sender once per issuer `[worker]`

Files: `src/invoicebot/pipeline/fetch.py`, `tests/integration/test_pipeline_fetch.py`.

- In `_exhausted`, when the PORTAL_MISSING gap is `raised_now` and `gaps.teach_via_chat` is on and no `taught_links` row exists for the RUC, queue BR-F02 to the sender (rule `BR-F02`, with `razon_social` and `ruc_emisor`) after BR-F07, in the same transaction.
- Tests: BR-F02 the first invoice of an unknown issuer gets BR-F07 then BR-F02; the second invoice of that issuer gets BR-F07 only; with `teach_via_chat: false` no BR-F02 is sent.

Test: `uv run pytest -q tests/integration/test_pipeline_fetch.py -k teach`.

## Step 17: Validate a taught link `[worker]`

Files: `src/invoicebot/net/taught_url.py`, `tests/unit/test_taught_url.py`.

- `async def check_taught_url(text: str, resolver: Resolver) -> str` returns the cleaned URL or raises `TaughtUrlError(reason)`: R-11.4 only `https`; at most 300 characters; no userinfo; a hostname, not an IP literal; the host resolves only to public addresses through the request guard's resolver and IP check in `net/guard.py` (reuse its functions, do not copy them); no fragment. `is_dont_know(text) -> bool` accepts "no sé", "no se", "nose" in any case and spacing.
- Tests: R-11.4 `https://portal.example.pe/consulta` passes; `http://…`, a 301-character URL, `https://u:p@host/`, `https://10.0.0.1/`, a host resolving to `127.0.0.1` (fake resolver) and `javascript:` are refused with their reasons; R-11.4 "No sé" and " nose " are "don't know".

Test: `uv run pytest -q tests/unit/test_taught_url.py`.

## Step 18: `taught_links` store and BR-S06 to the owner `[worker]`

Files: `src/invoicebot/store/taught_links.py`, `src/invoicebot/pipeline/teach.py`, `tests/integration/test_pipeline_teach.py`.

- Store: `propose(conn, ruc, url, profile_id) -> bool` (insert `proposed`, or replace a `rejected` row; an existing `proposed` or `approved` row is left and returns false); `decide(conn, ruc, approved: bool) -> TaughtLink | None` (only from `proposed`, locks the row).
- `pipeline/teach.py`: `async def teach(conn, ctx, profile_id, ruc, raw_url, resolver)`: validates (step 17), proposes, answers the sender with BR-S06's sender text, and sends BR-S06's owner text to each admin with buttons `t<ruc>:ok` [Aprobar] and `t<ruc>:no` [Rechazar]. An invalid link answers with the BR-F02 text again (no new text). A link for a RUC already `approved` answers BR-S06's sender text and does nothing else.
- Tests: R-10.2 a valid link stores `proposed`, thanks the sender and asks each admin with both buttons; R-11.4 an invalid link stores nothing and repeats BR-F02; BR-S06 a second proposal while one is `proposed` asks nobody again.

Test: `uv run pytest -q tests/integration/test_pipeline_teach.py -k propose`.

## Step 19: `/portal <ruc> <url>` `[worker:low]`

Files: `src/invoicebot/pipeline/commands.py`, `tests/integration/test_pipeline_commands.py`.

- Register `/portal` in the commands mapping: the argument is an 11-digit RUC and a URL; it calls `teach`. A malformed argument answers the command's usage line in `help_text`. Add `/portal` to `/ayuda`.
- Tests: R-11.4 `/portal 20100000001 https://portal.example.pe/x` proposes the link; `/portal 123 https://…` answers the usage line; `/ayuda` lists `/portal`.

Test: `uv run pytest -q tests/integration/test_pipeline_commands.py -k portal`.

## Step 20: A free-text BR-F02 answer `[worker:high: reasoning: intake text routing has several existing paths (commands, edits, uploads, parked replies); the BR-F02 check must slot in without stealing their texts]`

Files: `src/invoicebot/pipeline/intake.py`, `src/invoicebot/pipeline/teach.py`, `tests/integration/test_pipeline_teach.py`.

- A text message from a sender whose newest outbox question (the `outbox.last_rule` lookup D109 uses; ignore BR-S02 the same way) is BR-F02 for an invoice, and whose text is one `https` URL (step 17's shape, before DNS) or a "don't know", goes to `teach.answer`. A URL calls `teach` with that invoice's RUC; "no sé" answers `BR-F02_dont_know` (step 1) with nothing stored. Any other text, a command, or a sender whose newest question is not BR-F02 goes through the existing paths unchanged. Find where intake dispatches texts and add the check before the edit/`campo=valor` path, not before commands.
- Tests: R-11.4 a URL after BR-F02 proposes it; R-11.4 "no sé" stores nothing; R-11.4 a URL when the newest question is BR-R03 is not taken as a portal; R-11.4 `/estado` after BR-F02 still runs the command; R-3.5 a `serie=F001` edit after BR-F02 still edits.

Test: `uv run pytest -q tests/integration/test_pipeline_teach.py tests/integration/test_pipeline_intake.py`.

## Step 21: [Aprobar] and [Rechazar] `[worker]`

Files: `src/invoicebot/pipeline/teach.py`, `src/invoicebot/pipeline/intake.py`, `src/invoicebot/queue/outbox.py`, `tests/integration/test_pipeline_teach.py`.

- Intake hands a press whose id starts with `t` and matches `^t([0-9]{11}):(ok|no)$` to `teach.handle_press`, like the payment presses. Only an admin profile's press counts; any other is ignored and logged with the hashed sender.
- `ok`: `decide(approved=True)`, `set_hint` on `portal_missing:<ruc>`; if that gap has an `issue_url`, a `github` `comment` row (when GitHub is enabled); else the pending `create_issue` row's `fields.hint_url` is set (`UPDATE outbox SET payload_json = jsonb_set(…) WHERE channel = 'github' AND state = 'pending' AND payload_json->>'fingerprint' = …`, a function in `queue/outbox.py`), so the new issue carries it. `no`: `decide(approved=False)`. They answer the admin with `BR-S06_approved` or `BR-S06_rejected` (step 1).
- Tests: BR-S06 [Aprobar] sets `approved` and the gap's `hint_url`; with an issue it queues one `comment`; a second press changes nothing; a non-admin press changes nothing; [Rechazar] sets `rejected` and a later `/portal` can propose again.

Test: `uv run pytest -q tests/integration/test_pipeline_teach.py -k press`.

## Step 22: TEMPLATE_INVALID gap and the deliver `shortcut:`s `[worker]`

Files: `src/invoicebot/pipeline/deliver.py`, `tests/integration/test_pipeline_deliver.py`.

- `_template_invalid` (`deliver.py:804`): inside its transaction, `raise_gap(…, "TEMPLATE_INVALID", (flow_name, sink_name), …)`, where `sink_name` is the key of the sink whose template failed (find it at the caller, `deliver.py:410`). Remove the `shortcut:` log.
- `_prepare_values` (`deliver.py:486`): an issuer with no store entry is not a gap at delivery (spec 06 §3: PORTAL_MISSING comes only from BR-F07). Replace the `shortcut:` INFO log with a DEBUG line that says the slug stands in for the store name; no behavior change.
- Tests: BR-D02 an invalid template makes one TEMPLATE_INVALID gap linked to the invoice, fingerprint `template_invalid:<flow>:<sink>`; two invoices make one gap; BR-D02 the user text is unchanged.

Test: `uv run pytest -q tests/integration/test_pipeline_deliver.py -k template`.

## Step 23: BR-F03 PORTAL_BROKEN `[worker]`

Files: `src/invoicebot/store/fetch_attempts.py`, `src/invoicebot/pipeline/fetch.py`, `tests/integration/test_pipeline_fetch.py`.

- `store/fetch_attempts.py`: `technical_streak(conn, portal_key, n) -> bool`: the newest `n` attempts for the portal key are all `technical` or `timeout` (fewer than `n` attempts → false).
- `pipeline/fetch.py`: after recording a portal attempt whose reason is `technical` or `timeout`, in the same transaction, if `technical_streak(…, gaps.broken_after)`, `raise_gap(…, "PORTAL_BROKEN", (portal_key,), …)`. The chain continues as before.
- `manual_message`: for a portal whose PORTAL_BROKEN gap is raised, the bullet is BR-F03's line instead of the technical-failure line.
- Tests: BR-F03 three technical failures in a row on one portal (across invoices) raise one gap; `technical, not_found, technical, technical` with `broken_after` 3 raises nothing; the BR-F07 text of the third invoice shows `• Portal <name>: con fallas, ya avisé para arreglarlo`.

Test: `uv run pytest -q tests/integration/test_pipeline_fetch.py -k broken`.

## Step 24: BR-E06 PARSER_MISS `[worker]`

Files: `src/invoicebot/pipeline/extract.py`, `tests/integration/test_pipeline_extract.py`.

- In `_finish` (`extract.py:~209`), when the rule is BR-E01 and the reading has an 11-digit `ruc_emisor`, `raise_gap(…, "PARSER_MISS", (ruc,), …)` in the same transaction. No user text changes (spec 06: no extra message). Without a RUC nothing is raised.
- Tests: BR-E06 two unreadable photos with the same issuer RUC raise the gap on the second and queue no `GAP_admin` (digest kind); one unreadable photo raises nothing; an unreadable photo with no RUC adds no gap row.

Test: `uv run pytest -q tests/integration/test_pipeline_extract.py -k parser_miss`.

## Step 25: BR-R06 UNROUTED_BUYER `[worker]`

Files: `src/invoicebot/pipeline/route.py`, `tests/integration/test_pipeline_route.py`.

- Where `pipeline/route.py` ends in BR-R03 because no routing rule matches the buyer (`_resolve_flow`, `route.py:229`; not `_buyer_check`'s BR-R04 path), and the reading has a buyer document number, `raise_gap(…, "UNROUTED_BUYER", (num_doc_adq,), …)` in the same transaction. BR-R03 continues unchanged.
- Tests: BR-R06 three unrouted invoices with one buyer RUC in 30 days raise one gap and no `GAP_admin`; two do not; the BR-R03 question is sent each time.

Test: `uv run pytest -q tests/integration/test_pipeline_route.py -k unrouted`.

## Step 26: The daily loop `[worker]`

Files: `src/invoicebot/ops/__init__.py`, `src/invoicebot/ops/daily.py`, `tests/integration/test_ops_daily.py`.

- `DailyTask` frozen dataclass `(key: str, at: time, run: Callable[[], Awaitable[None]])`. `async def run_due(pool, tz: str, tasks: Sequence[DailyTask]) -> list[str]`: for each task, in the database clock and the owner's timezone, if the local time is past `at` and `ops_state[key]` is not from today (local date), claim it (`ops_state`), then run it; an exception is logged with `logger.exception` and the next task still runs; the claim stays, so a failing task does not loop every 5 minutes (each task reports its own failure, e.g. BR-O04). Returns the keys run. `DAILY_SWEEP_S = 300` and `async def daily_forever(...)` loop.
- `.importlinter`: `invoicebot.ops` must not import `api` or `events`.
- Tests: R-9.11 at 02:59 local nothing runs, at 03:01 the task runs once, a second call at 03:06 does not run it; a task not run yesterday runs at the next call today; a failing task does not stop the next; the claim is per local date in `America/Lima` (UTC−5).

Test: `uv run pytest -q tests/integration/test_ops_daily.py`.

## Step 27: The digest and the R-11.11 check `[worker]`

Files: `src/invoicebot/pipeline/gaps.py`, `tests/integration/test_pipeline_gaps.py`.

- `async def daily_digest(pool, profiles, gaps_cfg)`: one transaction. (1) `alert_due` over the digest kinds → one `GAP_DIGEST_admin` per admin listing them, then `mark_alerted`; nothing due → no message. (2) R-11.11: `unreported_overdue` gaps of non-digest kinds → `GAP_admin` without a link each, then `mark_alerted`.
- Tests: R-11.8 two PARSER_MISS gaps and one UNROUTED_BUYER gap make one digest message per admin with three lines; a second digest the same day lists nothing; R-11.11 a PORTAL_MISSING gap raised 25 h ago with no issue is alerted without a link, once.

Test: `uv run pytest -q tests/integration/test_pipeline_gaps.py -k digest`.

## Step 28: Retention (S15.1) `[worker]`

Files: `src/invoicebot/store/retention.py`, `tests/integration/test_store_retention.py`.

- `async def prune(conn, days: RetentionDays) -> dict[str, int]`: `UPDATE messages SET payload_json = NULL WHERE received_at < now() - …`; `DELETE FROM outbox WHERE created_at < … AND state <> 'pending'`; `DELETE FROM events WHERE created_at < …`; `DELETE FROM jobs WHERE created_at < …`. Never touches `invoices`. Returns the counts, logged once by the caller.
- Tests: R-9.12 rows older than each window go (payload nulled, row kept for `messages`), younger rows stay; a `pending` outbox row older than 90 days stays; invoices are untouched.

Test: `uv run pytest -q tests/integration/test_store_retention.py`.

## Step 29: The backup (S12.3) `[worker]`

Files: `src/invoicebot/ops/backup.py`, `tests/integration/test_ops_backup.py`, `deploy/Dockerfile`.

- `async def run_backup(cmd: Sequence[str], database_url: str, config_dir: Path, recipient: str, drive: Drive, target: str, max_bytes: int, today: date) -> None` as D118: `pg_dump` through `asyncio.create_subprocess_exec` with an argument list, the URL passed in `PGPASSWORD`/`PGHOST`-style environment of the child only (never on the command line, never logged), with a timeout; the config tarball built in memory with `tarfile`, skipping `secrets*` and `*.age`; `pyrage.encrypt` to the recipient; `drive.upload`. Any failure raises `BackupError(reason)`, `reason` a `Literal["dump", "too_large", "encrypt", "upload"]`.
- `deploy/Dockerfile`: add `postgresql-client` to the runtime `apt-get install` line.
- Tests with a fake `Drive` and a throwaway identity, against the test database: R-9.11 the uploaded dump decrypts with the identity and `pg_restore --list` shows `invoices` and no data for `kv`; the config tarball holds `example.yaml` and no `secrets*`; R-9.11 `max_bytes` 1 raises `too_large` and uploads nothing; a failing upload raises `upload`; R-9.16 the database password is not in any log record.

Test: `uv run pytest -q tests/integration/test_ops_backup.py`.

## Step 30: Daily tasks wired in `app.py`; BR-O04; CI client `[worker]`

Files: `src/invoicebot/app.py`, `.github/workflows/ci.yml`, `tests/unit/test_app_wiring.py`.

- `app.py` adds `"daily": daily_forever(...)` to the background tasks with three tasks: `backup` at `ops.backup.at` (personal `GraphDrive`, the age recipient, `ops.backup.target.path`; a `BackupError` queues BR-O04 with the reason's description through `queue_rate_limited_alert`, key `BR-O04`), `retention` at the same time + 30 minutes, `digest` at `gaps.digest_at`. A target `kind` other than `onedrive` fails at startup.
- `ci.yml`: the integration job installs `postgresql-client` next to Tesseract.
- Tests: R-9.11 the daily mapping has the three keys; BR-O04 a backup that raises `BackupError("upload")` queues one BR-O04 per admin with the upload description.

Test: `uv run pytest -q tests/unit/test_app_wiring.py`.

## Step 31: `scripts/restore_drill.sh` and the runbook `[worker]`

Files: `scripts/restore_drill.sh`, `deploy/README.md`, `tests/integration/test_restore_drill.py`.

- `restore_drill.sh <identity-file> <dump.age> <empty-database-url>`: `set -euo pipefail`; decrypt with `age -d -i` (or `python -m` a 5-line pyrage call if `age` is absent; pick one and document it), `pg_restore --no-owner --dbname`, then smoke queries: `invoices`, `outbox`, `gaps` exist, and `SELECT count(*) FROM invoices` prints. It refuses a target database that already has an `invoices` table.
- `deploy/README.md`: the drill (run before go-live and after schema changes, R-9.11), the uptime check on `/health` (R-9.14), and R-9.15 (the webhook answers 503 while Postgres is down and Meta redelivers).
- Test: R-9.11 the script restores a step-29 backup into a fresh database created in the test and prints the invoice count; a non-empty target is refused.

Test: `uv run pytest -q tests/integration/test_restore_drill.py`.

## Step 32: Self-alerts BR-O01 and BR-O02 (S15.2) `[worker]`

Files: `src/invoicebot/ops/selfcheck.py`, `src/invoicebot/app.py`, `tests/integration/test_ops_selfcheck.py`.

- `async def check(pool, profiles, alerts: SelfAlerts, data_dir: Path, disk_usage: Callable[[Path], float])`: BR-O01 when more than `max_rows` outbox rows are `pending` or the oldest is older than `max_age_min` (text with `{n}` and `{age}`); BR-O02 when `disk_usage(data_dir)` is above `disk_pct`. Each through `queue_rate_limited_alert` with `every = realert_hours`. Runs every 5 minutes as a background task in `app.py` (`shutil.disk_usage` wrapped as the default). Gap `github` rows waiting on `SendLater` count toward BR-O01 too: that is the "GitHub down" signal.
- Tests: BR-O01 21 pending rows alert once per admin and a second check within 6 h does not; BR-O01 one pending row 16 min old alerts; BR-O02 a fake 81 % alerts, 79 % does not.

Test: `uv run pytest -q tests/integration/test_ops_selfcheck.py`.

## Step 33: BR-O03 a rejected channel token `[worker]`

Files: `src/invoicebot/channels/whatsapp/transport.py`, `src/invoicebot/app.py`, `tests/unit/test_whatsapp_transport.py`.

- A WhatsApp 401, or an error with code 190, raises `SendLater(15 min, "BR-O03")` instead of a normal failure, and calls an `on_token_rejected` callback that `app.py` binds to `queue_rate_limited_alert` (key `BR-O03:whatsapp`, text with `{channel}` = `WhatsApp`). Since the alert itself goes through WhatsApp, the callback also logs at ERROR; there is no second channel yet (record that as a `shortcut:`).
- Tests: BR-O03 a 401 reschedules the row without an attempt and calls the callback once; code 190 does the same; a 400 with another code keeps today's behavior.

Test: `uv run pytest -q tests/unit/test_whatsapp_transport.py -k token`.

## Step 34: Health and the replayed delivery `[worker:low]`

Files: `tests/integration/test_db_health.py`.

- If not already covered (read the file first): R-9.15 with the pool closed the webhook answers 503 and stores nothing; after the pool reopens the same delivery is accepted once and its message stored once. R-9.14 `/health` answers 503 with the pool closed and 200 after. No source change unless a test fails; a failure stops the step.

Test: `uv run pytest -q tests/integration/test_db_health.py`.

## Step 35: Payment candidates by invoice date (D107 amended) `[worker]`

Files: `src/invoicebot/store/payments.py`, `src/invoicebot/pipeline/payments.py`, `tests/integration/test_pipeline_payments.py`.

- `candidates` orders by `fecha, id` and returns `fecha` (rename `Candidate.created_at` to `fecha: date`); the 180-day window stays on `created_at`. `_set_line` and `_pending_line` show the invoice date. `payment_match` sorts as it does today on whatever order it gets; check it does not rely on `created_at`.
- Tests: R-7.12 an invoice created today with `fecha` last month is listed before one created last week with `fecha` this week; BR-P02 button labels show `fecha`; R-7.12 an imported invoice created 10 days ago with `fecha` 300 days ago is still a candidate.

Test: `uv run pytest -q tests/integration/test_pipeline_payments.py`.

## Step 36: Payment matching off the event loop `[worker:low]`

Files: `src/invoicebot/pipeline/payment_match.py`, `src/invoicebot/pipeline/payments.py`, `tests/unit/test_payment_match.py`.

- `match_sets` gets a node cap, `MAX_NODES = 200_000`, and stops (returning what it found) when it is reached; `pipeline/payments.py` calls it through `asyncio.to_thread`.
- Tests: R-7.12 20 equal amounts with an impossible target return `[]` in under 1 s; R-7.12 the existing match cases still pass.

Test: `uv run pytest -q tests/unit/test_payment_match.py`.

## Step 37: BR-P03 for the `#id` form `[worker:low]`

Files: `src/invoicebot/pipeline/payments.py`, `tests/integration/test_pipeline_payments.py`.

- When some ids of the `#id` form are not candidates, BR-P03 shows the pending list without an amount (today it prints the sum of only the valid ids).
- Tests: BR-P03 `/pagado 15/12 #12 #99` where #99 is not a candidate: the reply has the pending list and no amount line.

Test: `uv run pytest -q tests/integration/test_pipeline_payments.py -k p03`.

## Step 38: `_resend_laptop` refuses a non-receipt mode `[worker:low]`

Files: `src/invoicebot/pipeline/deliver.py`, `tests/integration/test_pipeline_deliver.py`.

- `_resend_laptop` (`deliver.py:320`): when the resolved mail mode's `completion` is not `receipt` (e.g. `send_enabled: false` maps `laptop_outlook` to `DryRun`), store no `mail_jobs` row and answer as when the flow has no laptop mode.
- Tests: R-7.10 with `mail.send_enabled: false`, [Por la laptop] adds no `mail_jobs` row and no `.eml` path is stored as a job id.

Test: `uv run pytest -q tests/integration/test_pipeline_deliver.py -k resend_laptop`.

## Step 39: Laptop agent: exact key set and job expiry `[worker]`

Files: `scripts/laptop_agent.ps1`, `src/invoicebot/sinks/laptop_job.py`, `tests/laptop/LaptopAgent.Tests.ps1` (use the real Pester file).

- Jobs get a signed `type: "job"` and `created_at` (ISO 8601 UTC); receipts a signed `type: "receipt"`. `Test-JobFile` refuses any key set other than the job's exact set, a `type` other than `job`, and a `created_at` older than 7 days (`$MaxJobAgeDays`). The bot refuses a receipt without `type: "receipt"`.
- Tests: R-7.14 a receipt renamed into `_outbox/` is refused; a job with an extra key is refused; an 8-day-old job is refused and not sent; a fresh job passes; the Python side signs `type` and `created_at` (`uv run pytest -q tests/unit/test_laptop_job.py`).

Test: `pwsh -NoProfile -c "Invoke-Pester -CI tests/laptop" && uv run pytest -q tests/unit/test_laptop_job.py`.

## Deferred: live check

Run once S0.10, the `age` key pair and the OneDrive backup folder exist. Not a step of this plan, so the phase finishes without it; the roadmap tracks it. The private key and the App key stay off the repo.

- **GitHub:** `gaps.github` filled in on the host, `enabled: true`, `github_app_private_key` in the age store. One test invoice from an issuer with no store entry: one issue in the ops repo with labels `gap` and `gap:PORTAL_MISSING`, the marker, no sender text; the owner gets `GAP_admin` with the link. Delete the gap row and send a second invoice: no second issue. `/portal` for that RUC, [Aprobar]: the link appears as a comment. Uninstall the App for a minute: BR-D05 once, the row waits, and the issue opens after reinstalling.
- **Backup:** set `ops.backup.at` a few minutes ahead: two `.age` files in the OneDrive folder; `scripts/restore_drill.sh` on the laptop with the private key restores into an empty Postgres. Record the drill in `deploy/README.md` (R-9.11, before go-live).
- **Self-alerts:** stop the outbox loop briefly (or queue 21 rows on a test number): BR-O01 once.
- Record the result in `specs/04-tasks.md` (M4).

Test: the checks above, by hand.
