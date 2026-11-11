# 03 · Design

## 1. Architecture overview

```
 WhatsApp Cloud API / Telegram ──HTTPS──► Cloudflare Tunnel ──► app (single Python process, asyncio) ◄── Postgres
                                                      │
   ┌──────────────────────────────────────────────────┼───────────────────────────────────────────┐
   │ channels/        ← port: Channel (whatsapp, telegram), canonical in/out messages (PT-1)      │
   │ api/webhook      → verify signature → dedupe (channel, msg id) → persist → enqueue → 200     │
   │                    (Postgres down → 503, the channel retries)                                │
   │ api/             REST /api/v1 (OpenAPI) + MCP server, scoped tokens (v1.1)                   │
   │ queue/           Postgres job queue (SKIP LOCKED, lease + retry + backoff)                   │
   │ worker/          1 async worker loop; HEAVY semaphore = 1 (OCR / browser)                    │
   │ pipeline/        state machine per invoice (see §4)                                          │
   │   routing/       ordered rules → flow id (no menu unless unmatched, see §12)                 │
   │   rules/         business-rule catalog BR-* → state + es-PE message (spec 06)                │
   │   extract/       qr → ocr → parse → validate (+ padron) → llm_fallback (per flow) → validate │
   │   sunat/         validez API client (token cache)                                            │
   │   fetchers/      chain (flow A): qr_url → portal_registry → sunat_sol → email_inbox → manual │
   │   sinks/         google_drive, google_sheets, onedrive, excel_template, mailer               │
   │ net/             outbound request guard shared by httpx and Chromium (R-5.10)                │
   │ secrets/         age / systemd-creds loader; per-adapter secret scoping (R-9.9, R-9.10)      │
   │ flows/           declarative YAML: which fetchers, which sinks, batching, templates          │
   │ scheduler/       tiny cron (asyncio): batches, housekeeping, backups, self-checks            │
   │ store/           Postgres: messages, invoices, jobs, batches, files, outbox, events, kv      │
   │ events/          signed event webhooks to subscribers (Jarvis, n8n, …) via the outbox        │
   │ gaps/            gap service → outbox → GitHub App (issue in ops repo) + owner chat alert    │
   └──────────────────────────────────────────────────────────────────────────────────────────────┘
         ▲ on demand only: chromium-headless-shell (Playwright), tesseract (subprocess)
```

**Pattern map:**
- **Hexagonal (ports and adapters):** channels, fetchers and sinks.
- **Chain of Responsibility:** fetchers.
- **State machine:** invoice lifecycle.
- **Strategy:** email trigger modes.
- **Registry:** portals and flows.
- **Rules engine (ordered predicates, first match):** routing.
- **Observer/outbox:** gap events fan out to a GitHub issue and an owner chat alert without blocking the pipeline.
- **Outbox:** outgoing chat messages (WhatsApp, Telegram), GitHub calls and event webhooks are persisted, then sent, so they're retry-safe.
- **Published contract (P10):** REST, MCP and events are the only way other systems (Jarvis, n8n) reach the bot. The bot never calls into them except to deliver events.

## 2. Tech stack (chosen for a 2 GB RAM host, D42)
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
| Excel | `openpyxl` (keep formatting; `keep_vba` for .xlsm only with explicit opt-in); template SHA-256 pinned in config | Graph Excel API is unsupported on personal OneDrive; R-7.10 |
| XML | `lxml` with UBL 2.1 XPaths (ported from Greenter), parser with `resolve_entities=False`, `no_network=True`, `load_dtd=False`; `DOCTYPE` rejected; 2 MB cap. XMLDSig verified with `signxml` | Fast, small; R-2.12 |
| Auth | Google OAuth refresh token (manual HTTP), `msal` token cache for Graph | |
| Padrón RUC | Postgres table `padron(ruc bigint PRIMARY KEY, razon_social, estado, condicion)`, refreshed weekly by streaming the ZIP into `COPY` on a staging table, then swapping. SUNAT updates the ZIP daily; it is ~395 MB, so a weekly refresh costs ~1.6 GB/month of bandwidth and needs ~0.4 GB of free disk while it streams (see §8) | Indexed lookup, ~0 app RAM |
| LLM fallback | Optional adapter behind an `LLMExtractor` port; thin `httpx` call to an OpenAI-compatible endpoint. Default **DeepSeek `deepseek-flash`** (accepts images, ~1,024 tokens per image), **per-flow opt-in, never flow B** (D43). Usage recorded in `llm_usage` | D24, D43, R-2.11 |
| Secrets | One file encrypted with `age` (or `systemd-creds`), decrypted into memory at start; `.env` for non-secret settings only | R-9.9 |
| Deploy | Docker Compose: `app` (mem_limit 900m) + `postgres` (mem_limit 300m, `shared_buffers=64MB`) + `cloudflared`; 1–2 GB swapfile on host. **One path (D46):** green CI on `main` builds the image and deploys code and config (§15). Local dev uses the same Compose file (D31) | |
| n8n | **Optional** event subscriber (R-12.4) | See below |

### Note on n8n (open question Q1)
You chose "code + n8n". On a small host, n8n (250–450 MB resident) **plus** a Chromium fetch (≈300 MB) plus OCR can exceed the budget.

**Design decision:** the Python core implements every adapter natively, through thin REST calls. n8n, like Jarvis, is an **optional event subscriber** (R-12.4): it receives the signed `invoice.delivered` event with the invoice JSON and file URLs, and can call back through the REST API.

2 GB is now the baseline host (D42), and that is still tight for n8n beside a Chromium fetch. Enable it when:
- a. n8n runs on another machine, or
- b. `memprofile.py` shows enough headroom on the bot's host during a browser job.

You keep visual customization when you want it, and the core never depends on it.

## 3. Data model (Postgres)
```
messages(channel TEXT, external_id TEXT, profile_id, kind, received_at, payload_json,
         PRIMARY KEY (channel, external_id))                          -- idempotency (R-1.8)
invoices(id INTEGER PK, profile_id, origin_channel /*whatsapp|telegram*/, flow_id, state,
         natural_key TEXT UNIQUE NULL,
         ruc_emisor, tipo, serie, numero, fecha, moneda, base, igv, total,
         ruc_adq, razon_social, fields_json /*value+source+confidence*/,
         sunat_status_json, batch_id NULL, rule_id NULL, waiting_state NULL /*for PENDING_USER*/,
         error, created_at, updated_at)
files(id PK, invoice_id, kind /*photo|pdf|xml|xlsx|captcha*/, local_path NULL,
      remote_ref NULL, sha256, size, xml_trust NULL /*signed|sol|hint*/, created_at)
jobs(id PK, invoice_id, step, run_after, attempts, crashes, lease_until, last_error) -- queue
batches(id PK, flow_id, state /*open|sending|sent|failed*/, opened_at, sent_at, email_ref)
fetch_attempts(id PK, invoice_id, fetcher, portal_key, ok, reason /*enum code*/, ms, at) -- R-5.7
outbox(id PK, channel /*whatsapp|telegram|github|event*/, to /*profile id, repo or subscriber*/,
       payload_json, template NULL, state, attempts, created_at)        -- outbox pattern
gaps(fingerprint TEXT PK, kind, subject, state /*open|reported|fixed|closed|ignored*/,
     hint_url NULL, count, first_seen, last_seen, last_alert_at, issue_url NULL)
invoice_gaps(invoice_id, fingerprint, PRIMARY KEY(invoice_id, fingerprint))
taught_links(ruc_emisor bigint PK, url, profile_id, state /*proposed|approved|rejected*/,
             approved_at NULL, at)                                    -- R-11.4, R-10.2
pending_actions(id PK, kind, invoice_id, requested_by /*token name or profile*/,
                state /*pending|approved|rejected|expired*/, created_at, decided_at) -- R-12.8
llm_usage(id PK, invoice_id, provider, model, tokens_in, tokens_out, cost_usd, at)  -- R-2.11
kv(key PK, value, expires_at)          -- UNLOGGED; token cache; excluded from plain dumps (R-9.9)
padron(ruc bigint PK, razon_social, estado, condicion)
api_tokens(id PK, name, token_hash, scopes text[], created_at, last_used_at, revoked_at)   -- R-12.3
event_subscribers(id PK, name, url, secret_ref, event_types text[], active)              -- R-12.4
events(id PK, type, version, invoice_id NULL, payload_json, created_at)                   -- delivered via outbox
```
Natural key: `"{ruc_emisor}-{tipo}-{serie}-{int(numero)}"`. It is set once confirmed.

- **Profiles** (people) live in config, not in a table: each links one WhatsApp number and one Telegram user id (R-1.9). `profile_id` is the config key.
- **Retention (R-9.12):** `messages.payload_json` is nulled after 30 days; `outbox`, `events` and `jobs` rows are deleted after 90 days; `invoices` are kept. A nightly housekeeping job enforces it.

## 4. Invoice state machine
Main path:
```
RECEIVED ─► EXTRACTING ─► ROUTING ─► VALIDATING ─► FETCHING_DOCS ─► DELIVERING ─► DONE                    (flow A)
            qr → ocr →                                 ▲   │            └─► BATCHED ─(batch sent)─► DONE   (flow B)
            llm if the flow opts in                    │   ▼
                                                    WAITING_CAPTCHA
```
Side states and exits (each row names the rule that causes it):

| From | Trigger | To |
|---|---|---|
| EXTRACTING | QR, XML and OCR miss required fields, and the LLM is off for the flow, fails, or hit its cap (BR-E01, BR-E08) | EXTRACT_FAILED |
| EXTRACTING | Unsupported or oversized media (BR-E07) | CANCELLED |
| EXTRACT_FAILED | The user sends a better photo, or the data as `campo=valor` | EXTRACTING / ROUTING |
| ROUTING | BR-R03, BR-R04, BR-E03, any `source=llm` field, or `confirm_mode: always` | CONFIRMING |
| ROUTING | `unmatched: reject` (BR-R07), or the flow isn't allowed for the profile (BR-R05) | CANCELLED |
| CONFIRMING | The user confirms / edits | VALIDATING / ROUTING |
| VALIDATING | `estadoCp` 2 or 4 (BR-V02, BR-V06) | CONFIRM_INVALID |
| CONFIRM_INVALID | The user continues / cancels | FETCHING_DOCS / CANCELLED |
| EXTRACT_FAILED, CONFIRMING, CONFIRM_INVALID | No answer within `question_timeout` (BR-S08) | PENDING_USER (the waiting state is stored in `waiting_state`) |
| PENDING_USER | The user answers the pending question, directly or from `/pendientes` | the stored waiting state |
| WAITING_CAPTCHA | The user answers / times out (BR-F05) | FETCHING_DOCS (same fetcher / next fetcher) |
| FETCHING_DOCS | The chain ends in `manual` (BR-F07) | MANUAL_REQUIRED |
| MANUAL_REQUIRED | The user sends the files (R-1.5) / a linked gap is fixed (R-11.9) | DELIVERING / FETCHING_DOCS |
| DELIVERING | A token expired or was revoked (BR-D01) | PENDING_AUTH, then DELIVERING after re-auth |
| DELIVERING | The Excel template is missing, invalid or its hash doesn't match (BR-D02) | MANUAL_REQUIRED |
| DELIVERING (flow B) | Every sink done; added to the open batch | BATCHED |
| BATCHED | The batch is sent | DONE (files purged, `invoice.delivered`) |
| BATCHED | The batch email fails after retries (BR-D03) | stays BATCHED; the batch goes `failed`; retry with `/enviar` |
| any non-terminal | `/cancelar`, BR-E05 | CANCELLED |
| any | A transient error 5 times, or the worker dies twice on the same job (BR-S03) | FAILED |
| FAILED | `/reprocesar <id>` | FETCHING_DOCS |

Terminal states: `DONE`, `CANCELLED`, `FAILED` (reprocessable).

- Each transition is a single Postgres transaction (state update + next job insert + outbox message), and it records the **rule ID** that caused it (`invoices.rule_id`). `/estado` can therefore always explain *why* an invoice is where it is.
- With `confirm_mode: auto`, the "Editar" and "Cancelar" buttons from the summary are accepted in any state before `DELIVERING`. An edit re-enters `ROUTING`. A button pressed later gets BR-S02.

## 5. Extraction pipeline detail
1. **Load:**
   - Check the byte cap for the media type, and read the image header: more than 40 megapixels is rejected **before decoding** (R-1.11, BR-E07).
   - Decode with `cv2.imdecode`, downscale to ≤ 2000 px on the long side.
   - Keep a grayscale copy and drop the color image as soon as possible.
2. **QR cascade** (stop at the first payload that parses as SUNAT):
   1. zxing-cpp `read_barcodes(gray, formats=QRCode|PDF417)`. Older CPEs used PDF417.
   2. If nothing: crop candidate regions (bottom 40%, and contour-detected squares), apply 2× upscale + unsharp mask, retry zxing.
   3. If nothing: WeChat detector (its models are ~1 MB, loaded lazily).
   4. If nothing: pyzbar on the binarized image, then on rotations.
3. **QR parse:**
   - Split on `|`, strip, and map by position.
   - Validate the RUC check digit, `tipo ∈ {01,03,07,08,12…}`, the serie regex, decimals, and the date (`yyyy-mm-dd` per RS 114-2019 Anexo VI; `dd/mm/yyyy` tolerated).
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
7. **LLM fallback** (D43, R-2.11), only when steps 2–4 miss required fields **and** `llm_fallback` is enabled for the flow (never flow B) **and** the monthly cap isn't reached:
   - Send the image downscaled to ≤ 1024 px on the long side (about one image-token block), with no other invoice data.
   - Parse the JSON answer into fields with `source=llm`, confidence ≤ 0.5, and run them through the same validators (R-2.4). A field that fails is dropped, not kept as text.
   - Record tokens and cost in `llm_usage`. At the cap, skip with BR-E08.
   - Any `source=llm` field forces `CONFIRMING`.

## 6. Fetcher chain
Interface: `async def fetch(inv: Invoice, ctx: FetchContext) -> FetchResult(ok, files[], reason)`. `reason` is an enum code, never response text. The `FetchContext` gives access to:
- the shared browser (lazy),
- `ask_user(image, prompt, timeout)` for captchas,
- the HTTP client, wrapped by the request guard (R-5.10),
- **only the secrets the adapter declares** (R-9.10).

Default order for flow A: `qr_url → portal_registry → sunat_sol → email_inbox → manual`. When a fetcher fails for good, the chain moves to the **next** fetcher (R-5.1).

**Portal registry** (`config/portals.yaml`):
```yaml
- key: nubefact
  match: { ruc: ["20xxxxxxxxx", "..."], url_contains: ["nubefact"] }
  adapter: generic_form                 # or python: invoicebot.fetchers.portals.nubefact:Adapter (only this package is allowed)
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
- **Seeding the registry:** you supply a list of frequent issuers (RUC → link), which matches what you described ("in parallel we are going to define…"). New issuers default to the next fetcher. A link taught in chat is used only after the owner approves it (R-10.2).
- **`sunat_sol` (flow A):**
  - Playwright login with the secondary SOL user, navigate to "Consulta de comprobantes de pago", query by issuer, serie and número, download the XML.
  - SOL credentials are typed only when `page.url` matches the SUNAT origins hard-coded in source (R-9.10).
  - The selectors live in a file so they're easy to fix when SUNAT changes the UI.
  - **To validate manually in Phase 0 (S0.4, still a blocker):** that this menu actually offers the XML for received invoices. SIRE Compras returns rows, not XML, so it can't replace this fetcher.
- **`email_inbox`:**
  - Gmail query `has:attachment (filename:xml OR filename:pdf) "{serie}-{numero}" newer_than:30d`. Graph `$search` equivalent on Outlook. A dedicated invoice mailbox is preferred (R-6.5).
  - Validate the XML (R-2.12: signature and signer RUC) and match it against the invoice (R-5.6).
  - Retry schedule: +5 min, +1 h, +6 h, +24 h, because issuers send asynchronously. Each retry is a scheduled job, so nothing waits in the worker; meanwhile the invoice stays in `FETCHING_DOCS` with "esperando correo". After the last retry fails, the chain moves to the **next fetcher** (with the default order, `manual`).
- **XML handling (R-2.12):** every XML is parsed with the hardened parser (§2). XML from SOL is trusted by origin; XML from email or a user upload is authoritative only when its XMLDSig verifies and the signer's RUC equals `ruc_emisor`, otherwise it is stored as a hint (BR-F10).
- **Request guard (R-5.10):** one module, `net/guard.py`, used by `httpx` (a custom transport) and by Chromium (route interception): `https` only, DNS resolved once and the connection pinned to a public IP, private/loopback/link-local/CGNAT ranges blocked, every redirect re-checked, `file:`/`data:` blocked. The browser container's network allows internet egress only. A block returns an enum reason (BR-F08).
- **`manual`:** marks the state and composes the message listing the failed fetchers and reasons.

## 7. Sinks and flows (declarative)
`config/example.yaml` shows the full shape. Key ideas:
- A flow lists `fetchers` (ordered), `required_docs`, and `sinks` (ordered). Some sinks are `batch_aware`.
- A sink receives a `DeliveryContext` (invoice, files, previous sink outputs such as Drive links) and returns refs that later sinks or the final message can use.
- Template expressions use a tiny safe formatter (`str.format_map` with a restricted namespace plus formatting filters), not full Jinja, to save memory. Jinja is used only for email bodies, and imported lazily.
- **Formula injection (R-6.6, R-7.10):** Sheets writes use `valueInputOption=RAW`; Excel text cells are written as strings; any text starting with `= + - @`, tab or carriage return gets a leading `'`. The Excel template's SHA-256 is pinned in config.
- **Flow B batches:** `DELIVERING` uploads the files and fills the Excel, then the invoice is `BATCHED` in the open batch. The MVP sends a batch only `on_command` (`/enviar`); `per_invoice` and `scheduled` are post-MVP. When the batch is `sent`, its invoices become `DONE`.

## 8. Resource budget and efficiency tactics
- **One uvicorn worker.** CPU work (OpenCV) runs in `asyncio.to_thread`, guarded by `HEAVY = asyncio.Semaphore(1)`. Tesseract and Chromium run as subprocesses, so their memory is returned to the OS when they exit.
- **Browser lifecycle:** `BrowserPool(max=1, idle_close=60s)` with one context per fetch.
  - Launch flags: `--disable-dev-shm-usage --no-zygote --renderer-process-limit=1 --disable-gpu --blink-settings=imagesEnabled=false` (images are re-enabled only for captcha pages).
  - Block fonts, media and analytics with route interception.
- **Lazy imports:** heavy modules (cv2, playwright, openpyxl, lxml, jinja2) are imported inside the functions that need them. Idle RSS stays at about 60 MB.
- **Bounded queues:** at most 20 queued jobs per sender profile. Over the limit, the new message is stored but not enqueued, and the sender gets BR-S01.
- **Postgres:** small footprint (`shared_buffers=64MB`, `max_connections=20`, app pool of 5), indices on `invoices(state)`, `jobs(run_after)`, `invoices(natural_key)`. The worker sleeps on `LISTEN jobs` instead of polling.
- **Files:** stream downloads to `/data/tmp/{invoice_id}/`, using SHA-256 for dedup of identical photos. Purge after `DONE` (flow B: only once the batch is `sent`, never while `BATCHED`), plus a nightly housekeeping cron.
- **Padrón import:** stream-unzip, parse line by line, and stream into `COPY padron_staging`, then swap tables in one transaction. Peak RAM stays under 50 MB. The ZIP is ~395 MB (updated daily by SUNAT; we refresh weekly): keep ≥ 1 GB free disk for the download plus the staging table, and delete the ZIP after the swap.
- **Measure, don't guess:** a `scripts/memprofile.py` runs the golden set and records peak RSS per step (`psutil`). CI fails if the peak is over 800 MB (R-9.1).

## 9. Security
- Webhook: HMAC SHA-256 check with the app secret. Verify-token handshake on GET.
- Allowlist: `profiles:` in config. Each profile is one person with one WhatsApp number and/or one Telegram user id, allowed flows, permissions such as `can_send_batch`, and `admin`. Non-allowlisted senders are logged with a hashed id (R-9.12).
- **Secrets (R-9.9):** one secrets file encrypted with `age` (or `systemd-creds`), decrypted into memory at start and never written back in plain text. `.env` holds only non-secret settings. The token caches (`msal`, Google refresh token) are stored encrypted with the same key; the `kv` table is UNLOGGED and excluded from plain dumps.
- **Per-adapter secrets (R-9.10):** an adapter declares the secret names it needs, and the loader hands it only those. Only `sunat_sol` gets SOL credentials, and it types them only on SUNAT origins hard-coded in source. SOL credentials never leave the host: paid CPE APIs never receive them.
- Telegram: webhook secret token (`X-Telegram-Bot-Api-Secret-Token`) checked on every call; allowlist by user id.
- API and MCP (v1.1): bearer tokens stored as hashes, scopes `read`/`write`, revocable (R-12.3). Event webhooks are signed with HMAC-SHA256 per subscriber over `timestamp.body` (R-12.8).
- A missing secret fails closed: an empty token or secret disables the endpoint, it never skips the check.
- SOL: secondary user with only the query options.
- **Untrusted input:** outbound fetches go through the request guard (§6, R-5.10); XML through the hardened parser (R-2.12); sheet and Excel writes are formula-safe (§7); GitHub issue text is built from typed fields only (§13, R-11.12).
- **Logs (R-9.16):** a redaction filter masks every loaded secret and the Telegram bot token (which appears in API URLs); a test proves it.
- **Commands (R-10.1):** a command acts only on the sender profile's own invoices unless the sender is an admin; an unknown or foreign id gets the same answer (BR-S07).
- **CI (R-9.17):** gitleaks, pip-audit and bandit; no `pull_request_target`; no self-hosted runners; fork PR runs need approval; `yaml.safe_load` everywhere. While the repo is public, CI rejects images with EXIF data and any valid RUC not on the synthetic allowlist.
- **OAuth (R-6.5, R-7.9):** Google `drive.file` (+ `spreadsheets` only for an existing sheet); the OAuth app in production status. Microsoft: minimal Graph scopes; device code first, auth-code + PKCE loopback when the tenant blocks device code.
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
    pipeline/{states,machine}.py
    routing/router.py    # ordered rules → flow id
    rules/catalog.py     # BR-* ids → (state, message key); parity test vs specs/06
    rules/messages_es.py # es-PE message templates, keyed by rule ID
    gaps/{service,github_app,alerts}.py   # gaps/templates/gap_issue.md; stubs.py is post-MVP (D44)
    net/guard.py         # outbound request guard (R-5.10)
    secrets/loader.py    # age / systemd-creds, per-adapter scoping (R-9.9, R-9.10)
    ops/{backup,health,selfcheck}.py
    extract/{image,qr,ocr,parse,validate,padron,ubl,xmlsig,llm_fallback}.py
    sunat/{validez,sol}.py
    fetchers/{base,qr_url,portal_registry,email_inbox,sunat_sol,paid_api,manual}.py
    fetchers/portals/{generic_form,...}.py
    sinks/{base,google_drive,google_sheets,onedrive,excel_template,mailer_gmail,mailer_graph}.py
    auth/{google_oauth,msal_cache}.py
    browser/pool.py
  scripts/{auth_google.py,auth_microsoft.py,import_padron.py,memprofile.py,check_config.py,restore_drill.sh}
  .github/workflows/{ci.yml,deploy.yml}   # gap-watch.yml is post-MVP (D44)
  tests/{unit,integration,golden/{images,expected}}/
  migrations/
  deploy/{Dockerfile,docker-compose.yml,cloudflared.yml}
```

## 11. Testing strategy
- **Unit tests:**
  - QR payload parser (variants), RUC check digit, regex rules (table-driven), UBL parser, state machine transitions, cell-mapping formatter, batch strategies.
- **Golden set:** ≥ 30 photos with `expected.json`, run in CI. It reports per-field accuracy against R-2.9. While the repo is public (D27) the committed set is synthetic or anonymised; CI rejects images with EXIF data and any valid RUC not on the synthetic allowlist (R-9.17). Real photos stay outside git until the repo is private.
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
- Unmatched → `RouteResult(flow=None, rule="BR-R03", candidates=[...flows allowed for the sender's profile])` with `unmatched: ask`, or `RouteResult(flow=None, rule="BR-R07")` with `unmatched: reject`.
- BR-R03 uses WhatsApp's maximum of 3 reply buttons (two flows plus Cancelar). A third candidate flow needs list-message support (up to 10 rows) first.
- The low-confidence check (BR-R04) runs **after** the match, using `fields["num_doc_adq"].confidence`.
- Adding a flow C (for example, a second company) means a new `flows:` entry plus one routing rule. The code stays the same, apart from the list-message support above.

## 13. Gap service and GitHub alerts (D44, D45)
```
pipeline ──raise_gap(kind, subject, invoice_id, hint)──► GapService.upsert()  (same DB tx as the transition)
                                                           │ new or re-alert due?
                                                           ▼
                                   outbox rows: github.issue (ops repo) ; chat.admin (owner's preferred channel)
                                                           ▼ (worker, retries with backoff, daily budget ≤ 20 GitHub writes)
                                            GitHub REST via the shared httpx client (no extra dependency)
```
- **Where:** issues go only to the **private ops repo** (`gaps.github.repo`, placeholder `owner/mk-invoice-bot-ops`). The main repo gets no bot writes in the MVP.
- **Idempotency:**
  - The fingerprint is the gaps PK.
  - Before creating anything, the issue search uses the label `gap` plus a hidden marker `<!-- gap:fingerprint -->` in the body, so a lost DB row never duplicates an issue.
- **Issue text (R-11.12):** rendered from `gaps/templates/gap_issue.md` with typed fields only: the 11-digit issuer RUC, the padrón business name, the CPE type, fetcher reason codes, and the URL (if any) reduced to `https://host/path` with characters in `[A-Za-z0-9._~/-]`, inside a code fence. `@` is stripped. No OCR text, no sender text, no counts or dates. A snapshot test covers the template.
- **Lifecycle:** the bot opens the issue (REPORTED). The owner fixes the capability in the main repo and merges; CI deploys (§15). After the `SIGHUP` reload, `GapService.reevaluate()` marks the gap FIXED and re-enqueues the linked invoices. When one of them gets past the failing step (or, with no linked invoice left, right after the re-check), the gap is CLOSED and the bot closes the issue with a comment (spec 06 §3).
- **CI and portal status (R-11.6):** `ci.yml` runs lint, typecheck, tests, the rules↔catalog parity test (R-11.1) and `check_config.py`, which validates all YAML with the same pydantic models as the app. `todo`/`broken` portal entries fail **pull requests** only; on `main` they are listed in the job summary, so deploys never freeze. The owner sets `status: broken` by hand when BR-F03 reports a broken portal.
- **Owner alert (chat):** `🧩 Falta configurar: {kind_desc} — {subject}. Issue: {issue_url}`. Sent once per fingerprint per `realert_days`; "digest" kinds are batched into one daily message. It goes to each `admins` profile on its preferred channel. On WhatsApp it is a free-form service message inside the 24 h window, and the approved **utility template** `admin_alert` outside it (R-1.10).
- **Credential (D45):** a **GitHub App** installed only on the ops repo, with the **Issues** permission only. The bot signs a JWT with the App's private key (from the encrypted secrets file) and exchanges it for a short-lived installation token, cached in `kv` until shortly before it expires. Failure to get a token (key revoked, App uninstalled, permission missing) raises BR-D05.
- **Budget:** the gap service holds no memory. It is one table plus outbox rows, and it sends ≤ 20 GitHub calls a day.
- **Post-MVP (D44, spec 04 E16):** draft PRs with a `portals.yaml` stub, the `gap-watch` workflow, and bot-made `status: broken` flips. If they return, the App gets Contents and Pull requests on the main repo, YAML is written with a serializer plus a parse-back equality check, and nothing is pushed to the default branch.

## 14. Integration contract (D39, P10, R-12)
```
                ┌──────────── mk-invoice-bot ────────────┐
 Jarvis / n8n ──┤ REST /api/v1  (read, write scopes)      │
 future apps  ──┤ MCP  /mcp     (same scopes, same rules) │
                │ events ──HMAC POST──► subscribers ──────┼──► Jarvis / n8n
                └─────────────────────────────────────────┘
```
- **Dependency direction:** consumers depend on the bot's published contract. The bot depends on no consumer. An unreachable subscriber only delays its own events (outbox retries).
- **Same rules everywhere:** a write through REST or MCP (`reprocess_invoice`, `cancel_invoice`, `attach_document`) creates a `pending_actions` row and asks the owner in chat (BR-S05). Only after approval does it go through the same state machine and business rules as the chat command; it is recorded with the client token name.
- **Event envelope:** `{id, type, version, occurred_at, invoice: {id, natural_key, flow_id, state, totals…}, untrusted: {issuer_name: {value, trust: "untrusted", source}, printed_urls: […]}, links: {api, files}}`. Typed data and untrusted text are separate; untrusted values are capped at 120 characters with control characters and markdown stripped; raw OCR or LLM text is never included (R-12.8). File links point to the sinks (Drive or OneDrive), never to bot-local paths.
- **Signing and replay (R-12.8):** headers `X-Signature: sha256=HMAC(secret, timestamp + "." + body)` and `X-Timestamp`. Subscribers reject requests older than 5 minutes and dedupe by event `id`.
- **Exposure (R-12.8):** the API and MCP are reachable only through the tunnel behind an access policy (e.g. Cloudflare Access) plus tokens; the MCP endpoint validates `Origin`.
- **`invoice.delivered`** is emitted when the invoice reaches `DONE`: flow A after all sinks, flow B when its batch is sent.
- **Versioning:** `/api/v1`, MCP tool names stay stable, and events carry `version`. A breaking change creates v2 and keeps v1 for at least one release (R-12.6).
- **What Jarvis adds (outside this repo):** natural-language questions over invoices (MCP read tools), reminders from `invoice.manual_required` and `gap.raised`, and fetching documents through the owner's workstation. None of it is needed for the bot to work.

## 15. Deploy and operations (D46, R-9.11 to R-9.15)
- **One deploy path (D46):** a green CI run on `main` triggers `deploy.yml`:
  1. build and push the image;
  2. `pg_dump` the database (encrypted, R-9.11) before migrating;
  3. copy the config (`config/*.yaml`, never secrets) to the host;
  4. run migrations, start the new image, and check `/health`;
  5. on failure, roll back to the previous image (migrations are expand/contract, so the previous image still works, R-9.13).
  - When only config changed, the deploy copies it and sends `SIGHUP`; the app validates it before swapping it in (R-9.6), then `GapService.reevaluate()` resumes the affected invoices (R-11.9).
  - There is no pull timer on the host and no `deploy.config_pull` setting.
- **Backups (R-9.11):** a nightly job runs `pg_dump` (excluding `kv`) and tars the config, encrypts both to an `age` public key (the private key is kept off the host), and uploads them off-site. RPO ≤ 24 h. `scripts/restore_drill.sh` restores the latest dump into an empty Postgres and runs smoke queries; the drill is run before go-live and after schema changes. A failed backup raises BR-O04.
- **Retention (R-9.12):** see §3.
- **Health (R-9.14):** `/health` runs `SELECT 1` and returns 503 when the DB is unreachable. An external uptime check (any free monitor) polls it through the tunnel.
- **Self-alerts (R-9.14):** a scheduler job checks every 5 minutes for failed jobs (BR-S03), outbox backlog (BR-O01), disk usage (BR-O02) and channel token problems (BR-O03), and alerts the owner on the preferred channel. Each alert is rate-limited per condition.
- **Postgres down (R-9.15):** the webhook can't persist, so it returns 503 and does not acknowledge. Meta and Telegram retry, so no message is lost while the outage is shorter than their retry window. Documented in the runbook.
