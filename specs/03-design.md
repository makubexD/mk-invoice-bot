# 03 · Design

## 1. Architecture overview

```
 WhatsApp Cloud API / Telegram ──HTTPS──► Cloudflare Tunnel ──► app (single Python process, asyncio) ◄── Postgres
                                                      │
   ┌──────────────────────────────────────────────────┼─────────────────────────────────────┐
   │ channels/        ← port: Channel  (whatsapp, telegram)                                  │
   │ api/webhook      → verify signature → dedupe msg.id → persist → enqueue → 200 OK        │
   │ api/             REST /api/v1 (OpenAPI) + MCP server (streamable HTTP), scoped tokens   │
   │ queue/           Postgres job queue (SKIP LOCKED, lease + retry + backoff)              │
   │ worker/          1 async worker loop; HEAVY semaphore = 1 (OCR / browser)               │
   │ pipeline/        state machine per invoice (see §4)                                     │
   │   routing/       ordered rules → flow id (no menu unless unmatched, see §12)            │
   │   rules/         business-rule catalog BR-* → state + es-PE message (spec 06)           │
   │   extract/       qr → ocr → parse → validate (+ padron) → llm_fallback (off by default) │
   │   sunat/         validez API client (token cache)                                       │
   │   fetchers/      chain: qr_url → portal_registry → email_inbox → sunat_sol → paid → manual│
   │   sinks/         google_drive, google_sheets, onedrive, excel_template, mailer          │
   │ flows/           declarative YAML: which fetchers, which sinks, batching, templates     │
   │ scheduler/       tiny cron (asyncio) for scheduled batches + housekeeping               │
   │ store/           Postgres: messages, invoices, jobs, batches, files, events, kv cache   │
   │ events/          signed event webhooks to subscribers (Jarvis, n8n, …) via the outbox   │
   │ gaps/            gap service → outbox → GitHub REST (issue + draft PR) + admin WhatsApp │
   └─────────────────────────────────────────────────────────────────────────────────────────┘
         ▲ on demand only: chromium-headless-shell (Playwright), tesseract (subprocess)
```

**Pattern map:**
- **Hexagonal (ports and adapters):** channels, fetchers and sinks.
- **Chain of Responsibility:** fetchers.
- **State machine:** invoice lifecycle.
- **Strategy:** email trigger modes.
- **Registry:** portals and flows.
- **Rules engine (ordered predicates, first match):** routing.
- **Observer/outbox:** gap events fan out to GitHub and admin alerts without blocking the pipeline.
- **Outbox:** outgoing chat messages, GitHub calls and event webhooks are persisted, then sent, so they're retry-safe.
- **Published contract (P10):** REST, MCP and events are the only way other systems (Jarvis, n8n) reach the bot. The bot never calls into them except to deliver events.

## 2. Tech stack (chosen for a ≤ 2 GB RAM host)
| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12, `uv` | Best OCR, QR, openpyxl and Playwright ecosystem |
| HTTP server | FastAPI (on Starlette) + uvicorn (1 worker, `--loop uvloop`) | Async; OpenAPI for the REST contract (R-12.1) at little extra cost |
| MCP server | Official `mcp` Python SDK, streamable HTTP, mounted in the same app | R-12.2; no extra process |
| Models / config | pydantic v2 + `pydantic-settings`, YAML | Validation of config and extracted data |
| HTTP client | `httpx` (one shared AsyncClient, HTTP/2 off, pooled) | Thin REST to Meta, Google, Graph and SUNAT; avoids the heavy `google-api-python-client` |
| DB / queue / cache | **Postgres 16** via `psycopg` 3 (async, small pool); Alembic with plain-SQL migrations | D40: safe concurrent writes, remote querying for the API, queue via `FOR UPDATE SKIP LOCKED`, `LISTEN/NOTIFY` to wake the worker |
| QR | `zxing-cpp`, `opencv-contrib-python-headless` (wechat_qrcode), `pyzbar` | Cascade |
| OCR | Tesseract 5 + `spa` (apt), invoked as a subprocess | Memory is freed when it exits |
| Images | OpenCV headless; downscale long side to ≤ 2000 px; `cv2.IMREAD_REDUCED_*` when huge | Bounded memory |
| Browser | Playwright + `chromium-headless-shell`, launched per fetch batch, closed after 60 s idle | Never resident |
| Excel | `openpyxl` (keep formatting; `keep_vba` for .xlsm) | Graph Excel API is unsupported on personal OneDrive |
| XML | `lxml` with UBL 2.1 XPaths (ported from Greenter) | Fast, small |
| Auth | Google OAuth refresh token (manual HTTP), `msal` token cache for Graph | |
| Padrón RUC | Postgres table `padron(ruc bigint PRIMARY KEY, razon_social, estado, condicion)`, refreshed weekly by streaming the ZIP into `COPY` on a staging table, then swapping | Indexed lookup, ~0 app RAM |
| LLM fallback | Optional adapter behind an `LLMExtractor` port; thin `httpx` call to an OpenAI-compatible endpoint (DeepSeek by default). The provider must support image input: verify when E3b starts | D24, R-2.11 |
| Deploy | Docker Compose: `app` (mem_limit 900m) + `postgres` (mem_limit 300m, `shared_buffers=64MB`) + `cloudflared`; 1–2 GB swapfile on host. Local dev uses the same Compose file (D31) | |
| n8n | **Optional** event subscriber (R-12.4) | See below |

### Note on n8n (open question Q1)
You chose "code + n8n". On a small host, n8n (250–450 MB resident) **plus** a Chromium fetch (≈300 MB) plus OCR can exceed the budget.

**Design decision:** the Python core implements every adapter natively, through thin REST calls. n8n, like Jarvis, is an **optional event subscriber** (R-12.4): it receives the signed `invoice.delivered` event with the invoice JSON and file URLs, and can call back through the REST API.

Enable it when:
- a. the host has ≥ 2 GB, or
- b. n8n runs on another machine.

You keep visual customization when you want it, and the core never depends on it.

## 3. Data model (Postgres)
```
messages(id TEXT PK /*meta msg id*/, sender, kind, received_at, payload_json)            -- idempotency
invoices(id INTEGER PK, sender, flow_id, state, natural_key TEXT UNIQUE NULL,
         ruc_emisor, tipo, serie, numero, fecha, moneda, base, igv, total,
         ruc_adq, razon_social, fields_json /*value+source+confidence*/,
         sunat_status_json, batch_id NULL, rule_id NULL, error, created_at, updated_at)
files(id PK, invoice_id, kind /*photo|pdf|xml|xlsx|captcha*/, local_path NULL,
      remote_ref NULL, sha256, size, created_at)
jobs(id PK, invoice_id, step, run_after, attempts, lease_until, last_error)              -- queue
batches(id PK, flow_id, state /*open|sending|sent*/, opened_at, sent_at, email_ref)
fetch_attempts(id PK, invoice_id, fetcher, portal_key, ok, reason, ms, at)               -- R-5.7 stats
outbox(id PK, channel /*whatsapp|github*/, to, payload_json, state, attempts, created_at) -- outbox pattern
gaps(fingerprint TEXT PK, kind, subject, state /*open|reported|fixed|closed|ignored*/,
     hint_url NULL, count, first_seen, last_seen, last_alert_at, issue_url NULL, pr_url NULL)
invoice_gaps(invoice_id, fingerprint, PRIMARY KEY(invoice_id, fingerprint))
taught_links(ruc_emisor bigint PK, url, sender, at)                                      -- R-11.4
kv(key PK, value, expires_at)                                         -- UNLOGGED; token cache etc.
padron(ruc bigint PK, razon_social, estado, condicion)
api_tokens(id PK, name, token_hash, scopes text[], created_at, last_used_at, revoked_at)   -- R-12.3
event_subscribers(id PK, name, url, secret_ref, event_types text[], active)              -- R-12.4
events(id PK, type, version, invoice_id NULL, payload_json, created_at)                   -- delivered via outbox
```
Natural key: `"{ruc_emisor}-{tipo}-{serie}-{int(numero)}"`. It is set once confirmed.

## 4. Invoice state machine
```
RECEIVED ─► EXTRACTING ─► ROUTING ──(rule matched & all fields ok, confirm_mode=auto)──► VALIDATING ─► FETCHING_DOCS
                │            │                                                          │              │ ├─► WAITING_CAPTCHA ─┐
                │            └─(BR-R03 unknown buyer / BR-R04 low confidence / review)──► CONFIRMING   │ ◄────────────────────┘
                │                                                     │  ▲ edit   │ timeout          ▼
                ▼                                                     └──┘        ▼             DELIVERING ─► DONE
           EXTRACT_FAILED (BR-E01) → user sends data / better photo      PENDING_USER            │  ▲   (flow B batch modes:
                                                                                                │  │    DONE = added to batch)
   FETCHING_DOCS (all fetchers failed, BR-F07) ─► MANUAL_REQUIRED ─(user sends files | linked gap fixed)─► DELIVERING / FETCHING_DOCS
   DELIVERING (token expired, BR-D01) ─► PENDING_AUTH ─(re-auth)─► DELIVERING
   VALIDATING (estadoCp=2, BR-V02) ─► CONFIRM_INVALID ─(user: continue)─► FETCHING_DOCS
   any ─► CANCELLED (/cancelar, BR-R05, BR-E05) ;  transient error ─► retry w/ exp. backoff (max 5) ─► FAILED
```
- Each transition is a single Postgres transaction (state update + next job insert + outbox message), and it records the **rule ID** that caused it (`invoices.rule_id`). `/estado` can therefore always explain *why* an invoice is where it is.
- With `confirm_mode: auto`, the "Editar" and "Cancelar" buttons from the summary are accepted in any state before `DELIVERING`. An edit re-enters `ROUTING`.

## 5. Extraction pipeline detail
1. **Load:**
   - Decode with `cv2.imdecode`, downscale to ≤ 2000 px on the long side.
   - Keep a grayscale copy and drop the color image as soon as possible.
2. **QR cascade** (stop at the first payload that parses as SUNAT):
   1. zxing-cpp `read_barcodes(gray, formats=QRCode|PDF417)`. Older CPEs used PDF417.
   2. If nothing: crop candidate regions (bottom 40%, and contour-detected squares), apply 2× upscale + unsharp mask, retry zxing.
   3. If nothing: WeChat detector (its models are ~1 MB, loaded lazily).
   4. If nothing: pyzbar on the binarized image, then on rotations.
3. **QR parse:**
   - Split on `|`, strip, and map by position.
   - Validate the RUC check digit, `tipo ∈ {01,03,07,08,12…}`, the serie regex, decimals, and the date (two formats).
   - A payload starting with `http` becomes `document_url`.
4. **OCR fallback** (subprocess `tesseract - - -l spa --psm 4 --oem 1`, 30 s timeout):
   - Preprocess: 4-point perspective warp → deskew → CLAHE → adaptive Gaussian threshold → morphological open.
   - Rule file `config/parsing_rules.yaml`, keyword-anchored regexes:
     - `R\.?U\.?C\.?\s*:?\s*(\d{11})`
     - `(FACTURA|BOLETA)\s+ELECTR[OÓ]NICA`
     - serie-número `([FBE][A-Z0-9]{3}|\d{4})\s*[-–]\s*(\d{1,8})`
     - date formats
     - `(IMPORTE\s+)?TOTAL(\s+A\s+PAGAR)?\s*:?\s*S/?\.?\s*([\d.,]+)`
     - `IGV`, `OP\.?\s*GRAVADA`
   - The first 11-digit RUC with a valid check digit near the header is the issuer; the second one is the buyer.
5. **Enrich:**
   - Padrón lookup gives `razon_social`, `estado` and `condicion`.
   - Optional: a `config/issuers.yaml` alias table gives a friendly name (for example, "Tanta Larcomar").
6. **Score:**
   - Per-field confidence: QR = 1.0, padrón = 1.0, OCR scaled by the Tesseract word confidence and by cross-checks.
   - The overall status is `ok` when every required field has confidence ≥ 0.8, otherwise `needs_review`. Uncertain fields are highlighted in the summary.

## 6. Fetcher chain
Interface: `async def fetch(inv: Invoice, ctx: FetchContext) -> FetchResult(ok, files[], reason)`. The `FetchContext` gives access to:
- the shared browser (lazy),
- `ask_user(image, prompt, timeout)` for captchas,
- the HTTP client,
- secrets.

**Portal registry** (`config/portals.yaml`):
```yaml
- key: nubefact
  match: { ruc: ["20xxxxxxxxx", "..."], url_contains: ["nubefact"] }
  adapter: generic_form                 # or python: fetchers.portals.nubefact:Adapter
  url: "https://.../buscar"
  fields:                               # CSS selector -> expression
    "#ruc": "{ruc_emisor}"
    "#tipo": { select: "{tipo}" }
    "#serie": "{serie}"
    "#numero": "{numero}"
    "#fecha": "{fecha:%d/%m/%Y}"
    "#total": "{total:.2f}"
  captcha: { image: "#captchaImg", input: "#captcha", solver: [tesseract, user] }
  submit: "#btnBuscar"
  downloads: { xml: "a:has-text('XML')", pdf: "a:has-text('PDF')" }
```
- **Seeding the registry:** you supply a list of frequent issuers (RUC → link), which matches what you described ("in parallel we are going to define…"). New issuers default to the next fetcher.
- **`email_inbox`:**
  - Gmail query `has:attachment (filename:xml OR filename:pdf) "{serie}-{numero}" newer_than:30d`.
  - Graph `$search` equivalent on Outlook.
  - Validate the XML against the invoice (R-5.6).
  - Retry schedule: +5 min, +1 h, +6 h, +24 h, because issuers send asynchronously. Meanwhile the invoice stays in `FETCHING_DOCS` with "waiting for email". Final failure → `MANUAL_REQUIRED`.
- **`sunat_sol` (flow A):**
  - Playwright login with the secondary SOL user, navigate to "Consulta de comprobantes de pago", query by issuer, serie and número, download the XML.
  - The selectors live in a file so they're easy to fix when SUNAT changes the UI.
  - **To validate manually in Phase 0:** that this menu actually offers the XML for received invoices.
- **`manual`:** marks the state and composes the message listing the failed fetchers and reasons.

## 7. Sinks and flows (declarative)
`config/example.yaml` shows the full shape. Key ideas:
- A flow lists `fetchers` (ordered), `required_docs`, and `sinks` (ordered). Some sinks are `batch_aware`.
- A sink receives a `DeliveryContext` (invoice, files, previous sink outputs such as Drive links) and returns refs that later sinks or the final message can use.
- Template expressions use a tiny safe formatter (`str.format_map` with a restricted namespace plus formatting filters), not full Jinja, to save memory. Jinja is used only for email bodies, and imported lazily.

## 8. Resource budget and efficiency tactics
- **One uvicorn worker.** CPU work (OpenCV) runs in `asyncio.to_thread`, guarded by `HEAVY = asyncio.Semaphore(1)`. Tesseract and Chromium run as subprocesses, so their memory is returned to the OS when they exit.
- **Browser lifecycle:** `BrowserPool(max=1, idle_close=60s)` with one context per fetch.
  - Launch flags: `--disable-dev-shm-usage --no-zygote --renderer-process-limit=1 --disable-gpu --blink-settings=imagesEnabled=false` (images are re-enabled only for captcha pages).
  - Block fonts, media and analytics with route interception.
- **Lazy imports:** heavy modules (cv2, playwright, openpyxl, lxml, jinja2) are imported inside the functions that need them. Idle RSS stays at about 60 MB.
- **Bounded queues:** at most 20 queued jobs per sender. Back-pressure message: "estoy procesando N facturas, te aviso".
- **Postgres:** small footprint (`shared_buffers=64MB`, `max_connections=20`, app pool of 5), indices on `invoices(state)`, `jobs(run_after)`, `invoices(natural_key)`. The worker sleeps on `LISTEN jobs` instead of polling.
- **Files:** stream downloads to `/data/tmp/{invoice_id}/`, using SHA-256 for dedup of identical photos. Purge after `DONE`, plus a nightly housekeeping cron.
- **Padrón import:** stream-unzip, parse line by line, and stream into `COPY padron_staging`, then swap tables in one transaction. Peak RAM stays under 50 MB.
- **Measure, don't guess:** a `scripts/memprofile.py` runs the golden set and records peak RSS per step (`psutil`). CI fails if the peak is over 800 MB (R-9.1).

## 9. Security
- Webhook: HMAC SHA-256 check with the app secret. Verify-token handshake on GET.
- Allowlist: `senders:` in config, mapped to a profile (allowed flows, permissions such as `can_send_batch`, admin).
- Secrets in `.env` (root-only permissions). Optional `sops` + `age` for encrypted-at-rest files in the repo. The token caches (`msal`, Google refresh token) live in `/data/secrets/` with mode 600.
- Telegram: webhook secret token (`X-Telegram-Bot-Api-Secret-Token`) checked on every call; allowlist by user id.
- API and MCP: bearer tokens stored as hashes, scopes `read`/`write`, revocable (R-12.3). Event webhooks are signed with HMAC-SHA256 per subscriber.
- A missing secret fails closed: an empty token or secret disables the endpoint, it never skips the check.
- SOL: secondary user with only the query options. Credentials are never sent to paid APIs unless that adapter is explicitly enabled.
- The scraper honors per-domain rate limits (≥ 10 s between requests to the same portal), uses a realistic UA, and makes no parallel requests.

## 10. Repository layout (planned)
```
mk-invoice-bot/
  specs/                 # this spec set
  config/                example.yaml, portals.yaml, parsing_rules.yaml, issuers.yaml
  src/invoicebot/
    app.py               # FastAPI app, routes, lifespan
    api/{rest,mcp,auth}.py  # public contract (R-12)
    events/{schema,publisher}.py
    settings.py
    channels/{base,whatsapp,telegram}.py
    store/{db,repo}.py   # migrations/ holds Alembic plain-SQL revisions
    queue/{jobs,worker,scheduler}.py
    pipeline/{states,machine,messages_es}.py
    routing/router.py    # ordered rules → flow id
    rules/catalog.py     # BR-* ids → (state, message key); parity test vs specs/06
    gaps/{service,github,alerts,stubs}.py
    extract/{image,qr,ocr,parse,validate,padron,ubl,llm_fallback}.py
    sunat/{validez,sol}.py
    fetchers/{base,qr_url,portal_registry,email_inbox,sunat_sol,paid_api,manual}.py
    fetchers/portals/{generic_form,...}.py
    sinks/{base,google_drive,google_sheets,onedrive,excel_template,mailer_gmail,mailer_graph}.py
    auth/{google_oauth,msal_cache}.py
    browser/pool.py
  scripts/{auth_google.py,auth_microsoft.py,import_padron.py,memprofile.py,check_config.py}
  .github/workflows/{ci.yml,gap-watch.yml}
  .github/ISSUE_TEMPLATE/gap.md
  tests/{unit,integration,golden/{images,expected}}/
  migrations/
  deploy/{Dockerfile,docker-compose.yml,cloudflared.yml}
```

## 11. Testing strategy
- **Unit tests:**
  - QR payload parser (variants), RUC check digit, regex rules (table-driven), UBL parser, state machine transitions, cell-mapping formatter, batch strategies.
- **Golden set:** ≥ 30 real photos (anonymized buyer data) with `expected.json`, run in CI. It reports per-field accuracy against R-2.9.
- **Contract tests:**
  - Recorded fixtures (VCR-style) for Meta, Google, Graph and SUNAT.
  - Fake channel for end-to-end conversation tests.
- **Portal adapters:** a smoke test command `invoicebot portal-test <key> <sample-invoice>` that you run by hand, because live portals stay out of CI.
- **Resource test:** `memprofile.py` (R-9.1) and a latency report (R-9.2).

## 12. Routing (replaces the flow menu)
```python
@dataclass(frozen=True, slots=True)
class Rule:            # compiled once at config load
    id: str            # "BR-R01"
    when: Mapping[str, frozenset[str]]   # field -> allowed values (sets → O(1) membership)
    flow: str
def route(inv, sender) -> RouteResult:   # first match wins; O(rules) ≈ O(1) for a handful of rules
```
- Rules come from the `routing:` section of the config. `{owner.ruc}` and `{work.ruc}` are resolved at load time.
- Unmatched → `RouteResult(flow=None, rule="BR-R03", candidates=[...flows allowed for the sender's profile])`.
- The low-confidence check (BR-R04) runs **after** the match, using `fields["num_doc_adq"].confidence`.
- Adding a flow C (for example, a second company) means a new `flows:` entry plus one routing rule. The code stays the same.

## 13. Gap service and GitHub alerts
```
pipeline ──raise_gap(kind, subject, invoice_id, hint)──► GapService.upsert()  (same DB tx as the transition)
                                                           │ new or re-alert due?
                                                           ▼
                                   outbox rows: github.issue → github.branch+commit → github.draft_pr ; whatsapp.admin
                                                           ▼ (worker, retries with backoff, daily budget ≤ 20 GitHub writes)
                                            GitHub REST via the shared httpx client (no extra dependency)
```
- **Idempotency:**
  - The fingerprint is the gaps PK.
  - Before creating anything, the issue search uses the label `gap` plus a hidden marker `<!-- gap:fingerprint -->` in the body, so a lost DB row never duplicates an issue.
  - The branch name is deterministic (`gap/portal-missing-20123456789`).
- **Draft PR content (`PORTAL_MISSING`):** an appended YAML block, built from the taught URL, the printed URL or a placeholder.
  ```yaml
  - key: todo-20123456789          # TODO: rename
    status: todo                    # check_config.py fails CI while this is "todo"
    match: { ruc: ["20123456789"] }
    name: "RESTAURANTE EJEMPLO S.A.C."
    url: "https://example.com/consulta"   # taught via WhatsApp on 2026-10-23 (validated https)
    adapter: generic_form
    fields: {}                      # TODO: CSS selectors
    seen: { invoices: 3, series: ["F001"] }
  ```
  The PR body includes a checklist: fill the selectors, run `invoicebot portal-test <key>`, set `status: active`.
- **CI red:**
  - `ci.yml` runs lint, typecheck, tests, the rules↔catalog parity test (R-11.1) and `check_config.py`, which validates all YAML with the same pydantic models as the app and fails on `todo`/`broken`.
  - `gap-watch.yml` (cron, daily) uses the built-in `GITHUB_TOKEN` (read-only issues) and fails when a `gap` issue is older than `gap_sla_days`. GitHub emails the failure.
- **Admin alert (WhatsApp):** `🧩 Falta configurar: {kind_desc} — {subject}. Issue: {issue_url} · PR: {pr_url}`. Sent once per fingerprint per `realert_days`; "digest" kinds are batched into one daily message. The message is a free-form service message when the admin has written to the bot within the last 24 h; otherwise it uses a pre-approved **utility template** `gap_alert` (one is needed, because business-initiated messages outside the 24 h window require templates).
- **Deploy loop (how a merged fix reaches the VPS, R-11.9):**
  1. A systemd timer on the VPS runs `git fetch && git merge --ff-only origin/main` every 10 min. It needs no inbound access and no deploy keys with write rights; a read-only deploy key is enough.
  2. When `config/` changes, the app gets `SIGHUP` and hot-reloads its config.
  3. When code changes, a container rebuild is needed.
  4. After a reload, `GapService.reevaluate()` checks each open gap against the new registry, marks the fixed ones, and re-enqueues the linked invoices.
- **Credential:** a fine-grained personal access token (D17) limited to this repo, with Issues, Pull requests and Contents (write). It is stored in `.env` as `GITHUB_TOKEN`; its expiry date is stored too, for BR-D05.
- **Budget:** the gap service holds no memory. It is one table plus outbox rows, and it sends ≤ 20 GitHub calls a day.

## 14. Integration contract (D39, P10, R-12)
```
                ┌──────────── mk-invoice-bot ────────────┐
 Jarvis / n8n ──┤ REST /api/v1  (read, write scopes)      │
 future apps  ──┤ MCP  /mcp     (same scopes, same rules) │
                │ events ──HMAC POST──► subscribers ──────┼──► Jarvis / n8n
                └─────────────────────────────────────────┘
```
- **Dependency direction:** consumers depend on the bot's published contract. The bot depends on no consumer. An unreachable subscriber only delays its own events (outbox retries).
- **Same rules everywhere:** a write through REST or MCP (`reprocess_invoice`, `cancel_invoice`, `attach_document`) goes through the same state machine and business rules as the chat command, and is recorded with the client token name.
- **Event envelope:** `{id, type, version, occurred_at, invoice: {id, natural_key, flow_id, state, totals…}, links: {api, files}}`. File links point to the sinks (Drive or OneDrive), never to bot-local paths.
- **Versioning:** `/api/v1`, MCP tool names stay stable, and events carry `version`. A breaking change creates v2 and keeps v1 for at least one release (R-12.6).
- **What Jarvis adds (outside this repo):** natural-language questions over invoices (MCP read tools), reminders from `invoice.manual_required` and `gap.raised`, and fetching documents through the owner's workstation. None of it is needed for the bot to work.
