# 02 · Requirements

Acceptance criteria use **EARS** syntax: *WHEN* (trigger), *IF* (unwanted condition), *WHILE* (state), and *THE SYSTEM SHALL* (response).
Priority: **M** = must, in the MVP; **S** = should, in v1; **C** = could (later).
**v1 = the MVP = milestones M1–M5** (spec 04). Requirements marked **v1.1** (R-1.7 and all of R-12, milestone M6) and stories marked *post-MVP* ship after it, whatever their letter.

## Actors
- **Owner:** you. Configures everything and has a RUC (20…) with Clave SOL.
- **Sender:** an allowlisted person (sender profile) with one WhatsApp number and/or one Telegram user id (≤ ~10 people, < 200 invoices per month in total).
- **Issuer:** the store or restaurant that issued the CPE.

## Flows (initial)
| Flow id | Buyer RUC on the invoice | Delivery | SUNAT fallback available |
|---|---|---|---|
| `personal_google` (A) | Owner's company RUC | Google Drive + Google Sheets | **Yes** (owner's SOL) |
| `work_microsoft` (B) | Employer's RUC | OneDrive + Excel template + email | **No** (status validation only) |

---

## R-1 · Intake (channel)
- **R-1.1 (M)** WHEN an allowlisted sender sends an image (photo or image document), THE SYSTEM SHALL acknowledge with a single message within 5 s and enqueue a job.
- **R-1.2 (M)** IF the sender isn't allowlisted, THE SYSTEM SHALL ignore the message and log it with the number or user id hashed (R-9.12), without replying.
- **R-1.3 (M)** THE SYSTEM SHALL verify `X-Hub-Signature-256` on every webhook call and reject invalid ones with 401.
- **R-1.4 (M)** THE SYSTEM SHALL fetch media by its **media id**, which stays valid for 7 days. Because the media URL expires 5 minutes after it is issued, THE SYSTEM SHALL request a fresh URL on every download attempt, including retries. Downloading within 60 s of the webhook is a soft target, not a correctness condition.
- **R-1.5 (S)** WHEN a sender sends a PDF or XML document while an invoice is in `MANUAL_REQUIRED` or `CONFIRMING`, THE SYSTEM SHALL attach it to that invoice. Matching is **per sender profile, across channels**: the invoice is the one the message quotes, or else the most recent open invoice of the same profile, whichever channel it arrived on. An XML is checked as in R-2.12 and R-5.6.
- **R-1.6 (S)** WHEN a sender sends several photos in a row, THE SYSTEM SHALL process them as independent jobs, serialized (FIFO).
- **R-1.7 (S, v1.1)** THE SYSTEM SHALL support **Telegram** as a second channel through the same port (D41): allowlist by Telegram user id, webhook verified with `X-Telegram-Bot-Api-Secret-Token`, inline keyboards in place of reply buttons. Each sender profile can link one WhatsApp number and one Telegram id.
- **R-1.8 (M)** Webhook processing SHALL be idempotent **per channel message id** (`(channel, external_id)`), because Meta and Telegram retry deliveries.
- **R-1.9 (M)** THE SYSTEM SHALL handle every channel through one `Channel` port with a canonical inbound and outbound message (PT-1). Each invoice SHALL store its `profile_id` and `origin_channel`. Replies about an invoice SHALL go to its origin channel. Admin alerts SHALL go to the owner profile's preferred channel.
- **R-1.10 (M)** Before sending on WhatsApp, the outbox SHALL check the 24 h customer-service window per recipient. Outside the window, THE SYSTEM SHALL send the matching approved **utility template** (completion, manual-required, resume notice, pending-user reminder, batch preview, admin alert). IF Meta answers error 131047, THE SYSTEM SHALL map it to `WINDOW_EXPIRED` and resend as the template (PT-4, BR-S04). Templates are charged per message with no free tier, so the happy path SHALL stay inside the window.
- **R-1.11 (M)** THE SYSTEM SHALL enforce media limits before processing: reject images whose header declares more than 40 megapixels **before decoding**; apply byte caps per media type (`limits.media` in config, never above the channel's own limit, e.g. Telegram `getFile` 20 MB); cap zip entry count and compression ratio. A rejected file gets BR-E07. IF the worker dies twice on the same job, THE SYSTEM SHALL move the invoice to `FAILED` (crash-loop guard, BR-S03).

## R-2 · Extraction (deterministic first)
- **R-2.1 (M)** THE SYSTEM SHALL try QR decoding first, with a cascade of zxing-cpp → WeChat QR → pyzbar on preprocessed variants (original, grayscale, upscaled, sharpened, rotated 90°/180°/270°), stopping at the first valid SUNAT payload.
- **R-2.2 (M)** WHEN a SUNAT QR is decoded, THE SYSTEM SHALL extract: `ruc_emisor`, `tipo` (01/03/07/08), `serie`, `numero`, `igv`, `total`, `fecha_emision`, `tipo_doc_adq`, `num_doc_adq`.
- **R-2.3 (M)** IF no valid QR is found, THE SYSTEM SHALL run OCR (OpenCV preprocessing + Tesseract `spa`) and a regex/keyword parser that extracts the same fields plus `razon_social` (header text).
- **R-2.4 (M)** THE SYSTEM SHALL validate:
  - the RUC check digit (mod 11);
  - the serie pattern (`[FBE][A-Z0-9]{3}` or 4 digits);
  - the date (not in the future, not older than the configured limit);
  - `total ≈ base + IGV` (with a tolerance of S/ 0.05) when both are present.
- **R-2.5 (M)** THE SYSTEM SHALL resolve `razon_social` and `estado/condición` of the issuer from the local padrón RUC. OCR text is the fallback.
- **R-2.6 (M)** Each field SHALL carry `source ∈ {qr, xml, ocr, padron, llm, user}` and `confidence ∈ [0, 1]`.
- **R-2.7 (S)** WHEN the official XML is later obtained, THE SYSTEM SHALL parse UBL 2.1 and treat the XML values as authoritative. Any discrepancy from the photo data is reported to the user.
- **R-2.8 (S)** IF the QR contains a URL instead of data, THE SYSTEM SHALL store it as `document_url` for the fetcher.
- **R-2.10 (S)** THE SYSTEM SHALL capture URLs printed on the receipt (OCR regex such as `consulte .* en (\S+)`) as `printed_urls`. They are hints for the gap issue (R-11.5, R-11.12), not fetch targets.
- **R-2.9 (M)** Extraction accuracy target, measured on the golden dataset: ≥ 95% of invoices with a readable QR get every QR field exactly right; with OCR only, ≥ 80% of fields are right.
- **R-2.11 (C)** IF QR, XML and OCR all fail to give the required fields, AND `llm_fallback` is enabled **for the invoice's flow** (per-flow opt-in, off by default, **never flow B by default**; D24, D43), THE SYSTEM SHALL send the downscaled image to the configured vision LLM through a provider-neutral port. Every returned field SHALL pass the R-2.4 validators, be tagged `source=llm` with confidence ≤ 0.5, and **always** require user confirmation (overrides `confirm_mode: auto`). Raw LLM text is never shown or stored outside the validated fields. Each call is logged in `llm_usage` with tokens and cost and counts against a monthly cap; at the cap the step is skipped with BR-E08.
  - Note: the flow is only known after routing. When the buyer can't be read, the LLM runs only if every flow the sender's profile may use has opted in.
- **R-2.12 (M)** THE SYSTEM SHALL parse XML with entity resolution, DTD loading and network access disabled, reject any document with a `DOCTYPE`, and cap XML size at 2 MB (BR-F09). An XML from `email_inbox` or a user upload SHALL be authoritative only after its XMLDSig signature verifies **and** the signer's RUC equals `ruc_emisor`; otherwise it is kept as a hint (BR-F10). An XML downloaded from SUNAT SOL is trusted by origin.

## R-3 · Routing and confirmation (no flow menu)
- **R-3.1 (M)** THE SYSTEM SHALL pick the flow **automatically** with ordered routing rules (first match wins). The default rules are buyer RUC = owner RUC → A (BR-R01) and buyer RUC = work RUC → B (BR-R02). Rules can match on `num_doc_adq`, `tipo_doc_adq`, `ruc_emisor`, `tipo` and the sender profile, so adding a flow C later is a config change, not new code. *Note:* a flow C also needs the R-3.2 question to use a list message (see there).
- **R-3.2 (M)** IF no rule matches (unknown RUC, DNI or no buyer) and `routing.unmatched: ask`, THE SYSTEM SHALL ask with **one** message with reply buttons for the candidate flows plus Cancel (BR-R03). This is the **only** situation with a flow menu. IF `routing.unmatched: reject`, THE SYSTEM SHALL cancel with BR-R07.
  - Note: WhatsApp allows at most 3 reply buttons (titles ≤ 20 characters), so BR-R03 (two flows plus Cancelar) is at the limit. A third flow needs list-message support first (up to 10 rows). Telegram inline keyboards have no such limit.
- **R-3.3 (M)** IF the buyer field came from OCR with confidence below `review_threshold`, THE SYSTEM SHALL ask for confirmation with the detected flow pre-selected (BR-R04).
- **R-3.4 (M)** WHEN routing succeeds and every required field is `ok`, THE SYSTEM SHALL send the summary (fields plus the detected flow) and **continue without waiting** (`confirm_mode: auto`). The summary carries **Editar** and **Cancelar** buttons, which stay valid until delivery starts. `confirm_mode: always` restores the explicit confirmation step.
- **R-3.5 (S)** WHEN the user picks "Editar", THE SYSTEM SHALL accept `field=value` lines (for example, `total=45.50`), re-validate, re-route if the buyer changed, and show the summary again.
- **R-3.6 (M)** IF the natural key `(ruc_emisor, tipo, serie, numero)` already exists in a non-failed state, THE SYSTEM SHALL stop with BR-E04, unless the user sends `/reprocesar <id>`.
- **R-3.7 (S)** IF a question to the user isn't answered within the configured timeout (default 24 h), from `CONFIRMING`, `EXTRACT_FAILED` or `CONFIRM_INVALID`, THE SYSTEM SHALL park the invoice as `PENDING_USER` (BR-S08). WHEN the user answers the pending question, directly or from `/pendientes`, THE SYSTEM SHALL return the invoice to the state it was waiting in.
- **R-3.8 (M)** THE SYSTEM SHALL check the sender profile's permissions **after** routing, and reject with BR-R05 when needed.

## R-4 · Validation with SUNAT
- **R-4.1 (S)** WHEN SUNAT API credentials are configured, THE SYSTEM SHALL call `validarcomprobante` for every confirmed invoice, regardless of flow, and store `estadoCp`, `estadoRuc` and `condDomiRuc`.
- **R-4.2 (S)** THE SYSTEM SHALL act on `estadoCp` as spec 06 defines:
  - `1` (accepted) → continue;
  - `0` (doesn't exist yet) → continue and re-check later (BR-V01);
  - `2` (annulled) → ask whether to continue (BR-V02);
  - `3` (authorized, only for some CPE types) → continue with a note (BR-V05);
  - `4` (not authorized) → ask whether to continue (BR-V06).
- **R-4.3 (M)** THE SYSTEM SHALL cache the OAuth token until 60 s before it expires.

## R-5 · Document retrieval (PDF/XML)
- **R-5.1 (M)** THE SYSTEM SHALL try fetchers in the **order configured per flow**, stopping at the first one that returns a valid XML. A PDF is optional but desired. WHEN a fetcher fails for good (including the last of its scheduled retries), THE SYSTEM SHALL pass to the **next fetcher** in the order, never straight to `MANUAL_REQUIRED`. Retries are scheduled jobs, so a waiting fetcher never holds the worker.
- **R-5.2 (M)** Fetchers available:
  1. **`qr_url`:** follow the URL printed in the QR or on the receipt.
  2. **`portal_registry`:** look up the issuer RUC → portal definition (generic YAML form adapter or custom adapter).
  3. **`sunat_sol`:** *(flow A only)* log into SOL with the owner's secondary user, query received CPE, and download the XML.
  4. **`email_inbox`:** search the configured mailbox (Gmail or Outlook) for `serie-numero` / issuer RUC and download the XML/PDF attachments.
  - The list order is only the default for flow A (`qr_url, portal_registry, sunat_sol, email_inbox, manual`); each flow sets its own order.
  5. **`paid_api`:** *(optional, off by default)* Factiliza, json.pe or similar.
  6. **`manual`:** always the last step.
- **R-5.3 (M)** WHEN a portal shows an image captcha, THE SYSTEM SHALL first try local OCR (≤ 2 attempts). It SHALL then send the captcha image to the user and wait up to 5 min for a text reply.
- **R-5.4 (S)** WHEN a portal shows reCAPTCHA/hCaptcha, THE SYSTEM SHALL skip to the next fetcher, unless a paid solver is enabled.
- **R-5.5 (M)** IF every fetcher fails, THE SYSTEM SHALL set `MANUAL_REQUIRED` and send BR-F07: which fetchers failed and why, one line each, using the BR-F catalog. It SHALL accept the files later (R-1.5).
- **R-5.6 (M)** A fetched XML SHALL be checked to match the invoice: its RUC, serie, número and total equal the confirmed data.
- **R-5.7 (S)** THE SYSTEM SHALL record per-portal success and failure stats, so you can see which registry entries are broken.
- **R-5.8 (M)** Portal registry entries SHALL live in `config/portals.yaml` in the repo, keyed by issuer RUC, with an optional match by name. Each entry has a `status ∈ {active, todo, broken}`, and only `active` entries are used. A merged change SHALL reach the VPS through the CI-gated deploy (D46, R-11.9). A `python:` adapter SHALL be accepted only under `invoicebot.fetchers.portals.*` (checked by `check_config.py`).
- **R-5.10 (M)** Every outbound request to a URL that came from outside the code (QR URLs, printed URLs, taught links, portal entries) SHALL pass **one request guard** shared by `httpx` and Chromium:
  - `https` only; `file:`, `data:` and other schemes are blocked;
  - the host is resolved and the connection pinned to a **public** IP; private, loopback, link-local and CGNAT ranges are blocked;
  - every redirect is re-checked;
  - the browser container has egress to the internet only;
  - a failure is reported as an enum reason code (BR-F08), never as response text.
- **R-5.9 (C)** IF the flow allows it, THE SYSTEM MAY continue delivery without the PDF/XML, marking the row "documents pending", and attach them later when they arrive.

## R-6 · Flow A: Google Drive + Sheets
- **R-6.1 (M)** THE SYSTEM SHALL upload the photo, PDF and XML to the Drive folder `{root}/{YYYY}/{MM}/` with names `{fecha}_{ruc}_{serie}-{numero}.{ext}`. The path template is configurable.
- **R-6.2 (M)** THE SYSTEM SHALL append one row to the configured Sheet and tab. The column mapping is configurable and includes Drive links.
- **R-6.3 (M)** IF the row for the natural key already exists, THE SYSTEM SHALL update it, not duplicate it.
- **R-6.4 (S)** THE SYSTEM SHALL use OAuth with a refresh token, with the consent screen in production mode.
- **R-6.5 (M)** Google OAuth SHALL use least privilege: `drive.file`; `spreadsheets` only when the owner uses an existing sheet (Q6); `gmail.readonly` for the inbox fallback only, preferably on a dedicated mailbox. The OAuth app SHALL be in **production** status (unverified is fine for personal use), because testing mode expires refresh tokens after 7 days.
- **R-6.6 (M)** Sheets writes SHALL use `valueInputOption=RAW`, and any text value starting with `=`, `+`, `-`, `@`, tab or carriage return SHALL be escaped with a leading `'`, so no cell is ever evaluated as a formula.

## R-7 · Flow B: OneDrive + Excel template + email
- **R-7.1 (M)** THE SYSTEM SHALL store the photo, PDF and XML in the configured OneDrive folder (personal Microsoft account through Graph). The path template is configurable.
- **R-7.2 (M)** THE SYSTEM SHALL fill the Excel template by writing **mapped cells** (`cell → expression`, for example `C5: "{issuer.name}"`). It works on a copy of the template downloaded from OneDrive, edited with openpyxl, and uploaded with `If-Match`.
- **R-7.3 (M)** The email trigger SHALL be configurable per flow:
  1. **`per_invoice`:** one email per invoice, with its PDF, XML and filled Excel. *(post-MVP)*
  2. **`on_command`:** invoices accumulate in an open "batch" (state `BATCHED`); `/enviar` (or a button) sends **one** email with all PDFs, XMLs and Excel files. *(MVP)*
  3. **`scheduled`:** a cron expression (for example, every Friday 18:00 Lima) sends the open batch automatically. *(post-MVP)*
  4. Modes can be combined (for example, scheduled plus manual early send).
  - A flow B invoice is `DONE` only when its batch is `sent`. Its local files are purged only then.
- **R-7.4 (M)** Email recipients (to, cc), subject and body SHALL be templates in config (Jinja-style placeholders: totals, count, date range).
- **R-7.5 (S)** IF the attachments exceed the inline limit (3 MB on Graph), THE SYSTEM SHALL use an upload session, or zip the files.
- **R-7.6 (S)** Before sending, THE SYSTEM SHALL send the user a preview (recipients, count, total) with Send/Cancel buttons, configurable `confirm_before_send: true`.
- **R-7.7 (M)** THE SYSTEM SHALL send email via Graph `sendMail` (Outlook) **or** the Gmail API, chosen by config. *The MVP ships Graph only; the Gmail mailer is post-MVP.*
- **R-7.8 (C)** Optional mirror to a folder shared with the work account (see Q2).
- **R-7.9 (M)** Microsoft Graph SHALL use the minimal delegated scopes (`offline_access Files.ReadWrite Mail.Send`, plus `Mail.Read` only when the Outlook inbox fallback is on). IF the work tenant blocks device-code flow, THE SYSTEM SHALL offer an **auth-code + PKCE loopback** login as the fallback.
- **R-7.10 (M)** Excel writes SHALL store text values as strings (never formulas) with the same leading-character escape as R-6.6; numbers from validated decimal fields are written as numbers. THE SYSTEM SHALL pin the template's SHA-256 in config and refuse a template that doesn't match (BR-D02). `.xlsm` templates (`keep_vba`) SHALL be used only with an explicit opt-in.

## R-8 · Completion message
- **R-8.1 (M)** WHEN a flow completes (flow A: `DONE`; flow B: `BATCHED`, and again when the batch is sent), THE SYSTEM SHALL send one chat message, on the invoice's origin channel, containing:
  - the status (✅ / ⚠️ docs pending / ❌);
  - the key fields;
  - links (Sheet row and Drive folder, or OneDrive folder) and the batch status for flow B.
- **R-8.2 (M)** IF any step fails irrecoverably, THE SYSTEM SHALL say which step failed and what the user can do.

## R-9 · Non-functional
- **R-9.1 (M)** Memory: steady state ≤ 350 MB RSS, peak ≤ 800 MB for the `app` container (Postgres excluded, ≤ 300 MB on its own), verified by a load test of 10 queued invoices.
- **R-9.2 (M)** Latency: acknowledgement in < 5 s. Summary (QR path) in < 20 s, (OCR path) in < 60 s, on 1 vCPU.
- **R-9.3 (M)** WhatsApp messages per invoice: ≤ 3 in the happy path (acknowledgement, summary, completion), thanks to auto-routing; ≤ 5 with a gap question. All of them are service messages inside the 24 h window, which are free up to 1,000 per month per number. Templates (R-1.10) are charged per message, so they are used only outside the window.
- **R-9.4 (M)** Durability: a restart in any state resumes or reports the job. There are no lost invoices.
- **R-9.5 (M)** Observability: THE SYSTEM SHALL write one JSON object per log line with at least `ts`, `level`, `event`, `invoice_id`, `job_id`, `step`, `rule_id` and `duration_ms`, and SHALL record the duration of every pipeline step per job, so `/estado` and the logs show where time went. `/health` is defined in R-9.14. Secrets are never logged (R-9.16).
- **R-9.6 (M)** Configuration: one YAML file plus `.env` for **non-secret** settings; secrets come from the encrypted secrets file (R-9.9). Config reaches the host only through the CI deploy (D46). WHEN the process receives `SIGHUP`, THE SYSTEM SHALL validate the new config and swap it in only if it passes; a bad config keeps the old one and alerts the owner.
- **R-9.7 (S)** Data retention: local temporary files are deleted after `DONE` (flow B: after the batch is sent). The Postgres record is kept. Originals live in Drive or OneDrive. Per-table retention is R-9.12.
- **R-9.8 (M)** Language: user-facing messages in Spanish (es-PE), code and logs in English.
- **R-9.9 (M)** Secrets SHALL be stored in a file encrypted with `age` (or as `systemd-creds`) and decrypted into memory at start; they are never written to disk in plain text. `.env` holds only non-secret settings. The `kv` token cache SHALL be excluded from plain dumps or encrypted.
- **R-9.10 (M)** Each adapter SHALL receive only the secrets it declares. Only `sunat_sol` gets SOL credentials, and it SHALL type them only when `page.url` matches the SUNAT origins hard-coded in source. SOL credentials SHALL never leave the host, including to paid APIs.
- **R-9.11 (M)** Backups: THE SYSTEM SHALL run a nightly `pg_dump` plus a config backup, encrypted to an `age` public key and stored off-site (RPO ≤ 24 h). A restore drill into an empty Postgres SHALL be run and documented before go-live and after schema changes. A failed backup alerts the owner (BR-O04).
- **R-9.12 (M)** Retention per table: raw `messages.payload_json` 30 days; `outbox`, `events` and `jobs` 90 days; `invoices` kept. Log lines about non-allowlisted senders SHALL store only a hash of the number or user id.
- **R-9.13 (M)** Migrations SHALL follow **expand/contract** (a release never drops or renames something the previous image still uses). The deploy SHALL `pg_dump` before migrating, and rollback means redeploying the previous image.
- **R-9.14 (M)** Health and self-alerts: `/health` SHALL check the DB connection and return 503 when it fails. An external uptime check SHALL poll it. THE SYSTEM SHALL alert the owner about failed jobs (BR-S03), outbox backlog (BR-O01), disk usage (BR-O02) and channel token problems (BR-O03).
- **R-9.15 (M)** IF Postgres is down, the webhook SHALL return 503 without acknowledging, so Meta (and Telegram) retry the delivery. The runbook documents this behaviour.
- **R-9.16 (M)** Logs SHALL redact secrets, including the Telegram bot token that appears in request URLs. A test SHALL prove that no configured secret appears in captured log output.
- **R-9.17 (M)** CI security: gitleaks, pip-audit and bandit run on every push and PR (PT-8); no `pull_request_target`; no self-hosted runners; fork PR runs need approval; YAML is loaded only with `yaml.safe_load`. While the repo is public (D27), CI SHALL reject committed images that carry EXIF data and any valid RUC that isn't on the allowlist of synthetic RUCs.

## R-10 · Commands (chat)
| Command | Purpose | Priority |
|---|---|---|
| `/ayuda` | List the commands | M |
| `/estado <id>` | Show an invoice's status | S |
| `/pendientes` | List invoices waiting for user input or in MANUAL_REQUIRED | S |
| `/lote` | Show the open flow B batch (count, total) | M |
| `/enviar` | Send the open batch now | M |
| `/reprocesar <id>` | Re-run fetch and delivery | S |
| `/cancelar <id>` | Cancel an invoice | S |
| `/portal <ruc> <url>` | Teach the portal link for an issuer. Same effect as answering the BR-F02 question | S |
| `/gaps` | *(admin)* List open gaps with their issue links | S |
| `/gap ignorar <id>` | *(admin)* Stop alerting for one gap fingerprint | S |

- **R-10.1 (M)** Commands SHALL act only on the sender profile's own invoices, unless the sender is an admin. An id that doesn't exist or belongs to another profile SHALL get the same answer (BR-S07), so ids can't be probed.
- **R-10.2 (M)** A portal link taught with `/portal` or as an answer to BR-F02 SHALL be used only after the **owner approves it** in chat (BR-S06).

## R-11 · Business rules, gaps and alerts
- **R-11.1 (M)** Every stop, warning or reroute SHALL reference a rule ID from `specs/06-business-rules.md`, and SHALL use that rule's message from the catalog. A test SHALL fail if any rule in the spec has no catalog entry, or the other way round.
- **R-11.2 (M)** WHEN a rule marked as a gap fires, THE SYSTEM SHALL upsert a `gaps` row by fingerprint (count, last_seen, linked invoice IDs). Detecting a gap SHALL **never** block the fetcher chain.
- **R-11.3 (M)** The chat layer SHALL tell the user, in plain words, what is missing and whether the bot will retry by itself (`{gap_line}`).
- **R-11.4 (S)** `PORTAL_MISSING` SHALL be raised only when the fetcher chain ends in `MANUAL_REQUIRED` and the issuer has no portal entry. WHEN it fires for an issuer for the first time, THE SYSTEM SHALL ask the user for the portal link, once per issuer and not once per invoice (BR-F02). Answers SHALL be validated (only `https`, a public hostname, ≤ 300 characters, no credentials in the URL, R-5.10) before they are stored, and used only after the owner approves them (R-10.2).
- **R-11.5 (M)** WHEN a gap is new (or reopened), THE SYSTEM SHALL, through the outbox and with retries, open an issue in the **private ops repo** (`gaps.github.repo`, D44), labels `gap` and `gap:<kind>`, from a template, including only the data allowed by the privacy rule (R-11.12). WHEN a fixed gap is verified (spec 06 §3), THE SYSTEM SHALL close its issue with a comment.
  - ~~Create branch `gap/<fingerprint-slug>`, commit a stub or status change to `config/portals.yaml`, and open a draft PR that says `Closes #<issue>`~~ → post-MVP (D44, spec 04 E16). The bot never pushes to any branch in the MVP.
- **R-11.6 (M)** CI SHALL run `scripts/check_config.py`, which **fails** on any config that fails validation. Portal entries with `status: todo|broken` SHALL **fail CI only on pull requests**; on `main` they are reported in the job summary but don't fail it, so deploys never freeze. With D44, `status: broken` is set by the owner, not by a bot PR.
- **R-11.7 (C, post-MVP)** A scheduled workflow (`gap-watch`, daily) SHALL fail while any `gap` issue has been open for more than `gap_sla_days` (default 3). Deferred by D44.
- **R-11.8 (M)** WHEN a gap is new, THE SYSTEM SHALL send **one** chat message to each `admins` profile, on its preferred channel, with the kind, the subject and the issue link. Kinds marked "digest" go into a daily summary instead. Alerts for the same fingerprint SHALL be rate-limited to one per `realert_days` (default 7).
- **R-11.9 (M)** WHEN a deploy changes config (`portals.yaml`, `parsing_rules.yaml`, routing, templates), THE SYSTEM SHALL reload it on `SIGHUP` (R-9.6), re-evaluate open gaps, and resume the affected invoices: for each gap that is now fixed, it SHALL re-enqueue the linked invoices (`MANUAL_REQUIRED`/`PENDING_AUTH` → `FETCHING_DOCS`/`DELIVERING`) and tell their senders. Config reaches the host only through the CI-gated deploy (D46); there is no pull timer.
- **R-11.10 (M)** GitHub access SHALL use a **GitHub App** (D45) installed only on the ops repo, with short-lived installation tokens and the **Issues** permission only (no Contents, Pull requests or Workflows). The App's private key is a secret (R-9.9). The daily budget for GitHub writes is ≤ 20 operations. Failures to reach GitHub SHALL NOT affect invoice processing; they are retried from the outbox. Key or installation problems alert the owner (BR-D05).
- **R-11.11 (S)** IF GitHub is disabled or unreachable for more than 24 h, THE SYSTEM SHALL still alert the admin in chat, so the alert path never depends only on GitHub.
- **R-11.12 (M)** Gap issue text SHALL be built **only from typed fields**: the 11-digit issuer RUC, the business name from the padrón only, CPE type, fetcher reason codes, and a URL reduced to `https://host/path` whose characters match `[A-Za-z0-9._~/-]`, placed in a code fence. `@` is stripped everywhere. No raw OCR text and no free text from senders. Any YAML the bot writes in the future SHALL use a serializer and a parse-back equality check.

## R-12 · Integration contract (D39, constitution P10) · v1.1
Consumers (Jarvis, n8n, future apps) integrate only through this surface. The bot works fully without any consumer. **All of R-12 ships in v1.1 (M6)**, except R-12.7, which holds from the MVP on.
- **R-12.1 (S, v1.1)** THE SYSTEM SHALL expose a versioned REST API (`/api/v1`, OpenAPI published) to list and get invoices, filtered by date, flow, issuer and state, and to read spend summaries by period, flow and issuer.
- **R-12.2 (S, v1.1)** THE SYSTEM SHALL expose an **MCP server** (streamable HTTP) with read tools (`list_invoices`, `get_invoice`, `spend_summary`, `pending_manual`, `list_gaps`) and write tools (`reprocess_invoice`, `cancel_invoice`, `attach_document`). Write tools go through the same state machine and business rules as chat commands, after the owner confirms them (R-12.8).
- **R-12.3 (S, v1.1)** Every API and MCP client SHALL authenticate with a **scoped token** (`read`, `write`), stored hashed and revocable. A missing or invalid token gets 401; the read scope is the default.
- **R-12.4 (S, v1.1)** WHEN an invoice reaches `DONE`, `MANUAL_REQUIRED`, `PENDING_USER` or `FAILED`, or a gap is raised or closed, THE SYSTEM SHALL POST a versioned event (`invoice.delivered`, `invoice.manual_required`, `invoice.pending_user`, `invoice.failed`, `gap.raised`, `gap.closed`) to every configured subscriber, signed with HMAC-SHA256, through the outbox with retries. `invoice.delivered` means `DONE`: for flow A, all sinks finished; for flow B, the invoice's batch was sent (`BATCHED` alone is not delivered). n8n is just one event subscriber; there is no n8n sink.
- **R-12.5 (S, v1.1)** Events and API responses SHALL include only invoice data and ids, never secrets, OAuth tokens or raw credentials.
- **R-12.6 (C, v1.1)** A breaking change to the API, MCP tools or event schema SHALL ship as a new version, with the previous version kept for at least one release.
- **R-12.7 (M)** The bot SHALL NOT import, call or require any consumer for its core flows. An unreachable subscriber only delays its own events.
- **R-12.8 (S, v1.1)** Contract hardening:
  - Events and API responses SHALL separate typed data from untrusted text fields (`issuer_name`, `printed_urls`, OCR text). Each untrusted field carries `trust: "untrusted"` and its `source`, is capped at 120 characters, and has control characters and markdown stripped. Raw OCR or LLM text is never included.
  - MCP and REST write tools (`cancel_invoice`, `reprocess_invoice`, `attach_document`) SHALL create a **pending action** that the owner confirms in chat (BR-S05); nothing changes until then.
  - The event HMAC SHALL sign `timestamp.body`. Subscribers reject requests older than 5 minutes and dedupe by event id.
  - The MCP endpoint SHALL validate the `Origin` header.
  - The API and MCP SHALL be reachable only through the tunnel with an access policy (e.g. Cloudflare Access), in addition to tokens.
