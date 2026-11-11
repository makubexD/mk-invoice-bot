# 02 · Requirements

Acceptance criteria use **EARS** syntax: *WHEN* (trigger), *IF* (unwanted condition), *WHILE* (state), and *THE SYSTEM SHALL* (response).
Priority: **M** = MVP, **S** = should (v1), **C** = could (later).

## Actors
- **Owner:** you. Configures everything and has a RUC (20…) with Clave SOL.
- **Sender:** an allowlisted WhatsApp number (≤ ~10 people, < 200 invoices per month in total).
- **Issuer:** the store or restaurant that issued the CPE.

## Flows (initial)
| Flow id | Buyer RUC on the invoice | Delivery | SUNAT fallback available |
|---|---|---|---|
| `personal_google` (A) | Owner's company RUC | Google Drive + Google Sheets | **Yes** (owner's SOL) |
| `work_microsoft` (B) | Employer's RUC | OneDrive + Excel template + email | **No** (status validation only) |

---

## R-1 · Intake (channel)
- **R-1.1 (M)** WHEN an allowlisted sender sends an image (photo or image document), THE SYSTEM SHALL acknowledge with a single message within 5 s and enqueue a job.
- **R-1.2 (M)** IF the sender isn't allowlisted, THE SYSTEM SHALL ignore the message and log it, without replying.
- **R-1.3 (M)** THE SYSTEM SHALL verify `X-Hub-Signature-256` on every webhook call and reject invalid ones with 401.
- **R-1.4 (M)** THE SYSTEM SHALL download the media within 60 s of the webhook, because Meta's media URL expires after 5 minutes.
- **R-1.5 (S)** WHEN a sender sends a PDF or XML document while an invoice is in `MANUAL_REQUIRED` or `CONFIRMING`, THE SYSTEM SHALL attach it to that invoice. The invoice is the one the message quotes, or the most recent open one.
- **R-1.6 (S)** WHEN a sender sends several photos in a row, THE SYSTEM SHALL process them as independent jobs, serialized (FIFO).
- **R-1.7 (S)** THE SYSTEM SHALL support **Telegram** as a second channel through the same port (D41): allowlist by Telegram user id, webhook verified with `X-Telegram-Bot-Api-Secret-Token`, inline keyboards in place of reply buttons. Each sender profile can link one WhatsApp number and one Telegram id.
- **R-1.8 (M)** Webhook processing SHALL be idempotent per Meta `message.id`, because Meta retries deliveries.

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
- **R-2.6 (M)** Each field SHALL carry `source ∈ {qr, xml, ocr, padron, user}` and `confidence ∈ [0, 1]`.
- **R-2.7 (S)** WHEN the official XML is later obtained, THE SYSTEM SHALL parse UBL 2.1 and treat the XML values as authoritative. Any discrepancy from the photo data is reported to the user.
- **R-2.8 (S)** IF the QR contains a URL instead of data, THE SYSTEM SHALL store it as `document_url` for the fetcher.
- **R-2.10 (S)** THE SYSTEM SHALL capture URLs printed on the receipt (OCR regex such as `consulte .* en (\S+)`) as `printed_urls`. They are hints for the gap PR (R-11.5), not fetch targets.
- **R-2.9 (M)** Extraction accuracy target, measured on the golden dataset: ≥ 95% of invoices with a readable QR get every QR field exactly right; with OCR only, ≥ 80% of fields are right.
- **R-2.11 (C)** IF QR, XML and OCR all fail to give the required fields, AND the `llm_fallback` adapter is enabled (off by default, D24), THE SYSTEM SHALL send the downscaled image to the configured vision LLM through a provider-neutral port, tag every returned field `source=llm` with confidence ≤ 0.5, and **always** require user confirmation (overrides `confirm_mode: auto`). Each call is logged with tokens and cost and counts against a monthly cap.

## R-3 · Routing and confirmation (no flow menu)
- **R-3.1 (M)** THE SYSTEM SHALL pick the flow **automatically** with ordered routing rules (first match wins). The default rules are buyer RUC = owner RUC → A (BR-R01) and buyer RUC = work RUC → B (BR-R02). Rules can match on `num_doc_adq`, `tipo_doc_adq`, `ruc_emisor`, `tipo` and the sender profile, so adding a flow C later is a config change, not new code.
- **R-3.2 (M)** IF no rule matches (unknown RUC, DNI or no buyer), THE SYSTEM SHALL ask with **one** message with reply buttons for the candidate flows plus Cancel (BR-R03). This is the **only** situation with a flow menu.
- **R-3.3 (M)** IF the buyer field came from OCR with confidence below `review_threshold`, THE SYSTEM SHALL ask for confirmation with the detected flow pre-selected (BR-R04).
- **R-3.4 (M)** WHEN routing succeeds and every required field is `ok`, THE SYSTEM SHALL send the summary (fields plus the detected flow) and **continue without waiting** (`confirm_mode: auto`). The summary carries **Editar** and **Cancelar** buttons, which stay valid until delivery starts. `confirm_mode: always` restores the explicit confirmation step.
- **R-3.5 (S)** WHEN the user picks "Editar", THE SYSTEM SHALL accept `field=value` lines (for example, `total=45.50`), re-validate, re-route if the buyer changed, and show the summary again.
- **R-3.6 (M)** IF the natural key `(ruc_emisor, tipo, serie, numero)` already exists in a non-failed state, THE SYSTEM SHALL stop with BR-E04, unless the user sends `/reprocesar <id>`.
- **R-3.7 (S)** IF a question to the user isn't answered within the configured timeout (default 24 h), THE SYSTEM SHALL park the invoice as `PENDING_USER`. It is resumable with `/pendientes`.
- **R-3.8 (M)** THE SYSTEM SHALL check the sender profile's permissions **after** routing, and reject with BR-R05 when needed.

## R-4 · Validation with SUNAT
- **R-4.1 (S)** WHEN SUNAT API credentials are configured, THE SYSTEM SHALL call `validarcomprobante` for every confirmed invoice, regardless of flow, and store `estadoCp`, `estadoRuc` and `condDomiRuc`.
- **R-4.2 (S)** IF `estadoCp ≠ 1` (accepted), THE SYSTEM SHALL warn the user and ask whether to continue.
- **R-4.3 (M)** THE SYSTEM SHALL cache the OAuth token until 60 s before it expires.

## R-5 · Document retrieval (PDF/XML)
- **R-5.1 (M)** THE SYSTEM SHALL try fetchers in the **order configured per flow**, stopping at the first one that returns a valid XML. A PDF is optional but desired.
- **R-5.2 (M)** Fetchers available:
  1. **`qr_url`:** follow the URL printed in the QR or on the receipt.
  2. **`portal_registry`:** look up the issuer RUC → portal definition (generic YAML form adapter or custom adapter).
  3. **`email_inbox`:** search the configured mailbox (Gmail or Outlook) for `serie-numero` / issuer RUC and download the XML/PDF attachments.
  4. **`sunat_sol`:** *(flow A only)* log into SOL with the owner's secondary user, query received CPE, and download the XML.
  5. **`paid_api`:** *(optional, off by default)* Factiliza, json.pe or similar.
  6. **`manual`:** always the last step.
- **R-5.3 (M)** WHEN a portal shows an image captcha, THE SYSTEM SHALL first try local OCR (≤ 2 attempts). It SHALL then send the captcha image to the user and wait up to 5 min for a text reply.
- **R-5.4 (S)** WHEN a portal shows reCAPTCHA/hCaptcha, THE SYSTEM SHALL skip to the next fetcher, unless a paid solver is enabled.
- **R-5.5 (M)** IF every fetcher fails, THE SYSTEM SHALL set `MANUAL_REQUIRED` and send BR-F07: which fetchers failed and why, one line each, using the BR-F catalog. It SHALL accept the files later (R-1.5).
- **R-5.6 (M)** A fetched XML SHALL be checked to match the invoice: its RUC, serie, número and total equal the confirmed data.
- **R-5.7 (S)** THE SYSTEM SHALL record per-portal success and failure stats, so you can see which registry entries are broken.
- **R-5.8 (M)** Portal registry entries SHALL live in `config/portals.yaml` in the repo, keyed by issuer RUC, with an optional match by name. Each entry has a `status ∈ {active, todo, broken}`, and only `active` entries are used. A merged change SHALL reach the VPS without a manual deploy (R-11.9).
- **R-5.9 (C)** IF the flow allows it, THE SYSTEM MAY continue delivery without the PDF/XML, marking the row "documents pending", and attach them later when they arrive.

## R-6 · Flow A: Google Drive + Sheets
- **R-6.1 (M)** THE SYSTEM SHALL upload the photo, PDF and XML to the Drive folder `{root}/{YYYY}/{MM}/` with names `{fecha}_{ruc}_{serie}-{numero}.{ext}`. The path template is configurable.
- **R-6.2 (M)** THE SYSTEM SHALL append one row to the configured Sheet and tab. The column mapping is configurable and includes Drive links.
- **R-6.3 (M)** IF the row for the natural key already exists, THE SYSTEM SHALL update it, not duplicate it.
- **R-6.4 (S)** THE SYSTEM SHALL use OAuth with a refresh token, with the consent screen in production mode.

## R-7 · Flow B: OneDrive + Excel template + email
- **R-7.1 (M)** THE SYSTEM SHALL store the photo, PDF and XML in the configured OneDrive folder (personal Microsoft account through Graph). The path template is configurable.
- **R-7.2 (M)** THE SYSTEM SHALL fill the Excel template by writing **mapped cells** (`cell → expression`, for example `C5: "{issuer.name}"`). It works on a copy of the template downloaded from OneDrive, edited with openpyxl, and uploaded with `If-Match`.
- **R-7.3 (M)** The email trigger SHALL be configurable per flow:
  1. **`per_invoice`:** one email per invoice, with its PDF, XML and filled Excel.
  2. **`on_command`:** invoices accumulate in an open "batch"; `/enviar` (or a button) sends **one** email with all PDFs, XMLs and Excel files.
  3. **`scheduled`:** a cron expression (for example, every Friday 18:00 Lima) sends the open batch automatically.
  4. Modes can be combined (for example, scheduled plus manual early send).
- **R-7.4 (M)** Email recipients (to, cc), subject and body SHALL be templates in config (Jinja-style placeholders: totals, count, date range).
- **R-7.5 (S)** IF the attachments exceed the inline limit (3 MB on Graph), THE SYSTEM SHALL use an upload session, or zip the files.
- **R-7.6 (S)** Before sending, THE SYSTEM SHALL send the user a preview (recipients, count, total) with Send/Cancel buttons, configurable `confirm_before_send: true`.
- **R-7.7 (M)** THE SYSTEM SHALL send email via Graph `sendMail` (Outlook) **or** the Gmail API, chosen by config.
- **R-7.8 (C)** Optional mirror to a folder shared with the work account (see Q2).

## R-8 · Completion message
- **R-8.1 (M)** WHEN a flow completes, THE SYSTEM SHALL send one WhatsApp message containing:
  - the status (✅ / ⚠️ docs pending / ❌);
  - the key fields;
  - links (Sheet row and Drive folder, or OneDrive folder) and the batch status for flow B.
- **R-8.2 (M)** IF any step fails irrecoverably, THE SYSTEM SHALL say which step failed and what the user can do.

## R-9 · Non-functional
- **R-9.1 (M)** Memory: steady state ≤ 350 MB RSS, peak ≤ 800 MB for the `app` container (Postgres excluded, ≤ 300 MB on its own), verified by a load test of 10 queued invoices.
- **R-9.2 (M)** Latency: acknowledgement in < 5 s. Summary (QR path) in < 20 s, (OCR path) in < 60 s, on 1 vCPU.
- **R-9.3 (M)** WhatsApp messages per invoice: ≤ 3 in the happy path (acknowledgement, summary, completion), thanks to auto-routing; ≤ 5 with a gap question. This keeps us inside the free service-message allowance.
- **R-9.4 (M)** Durability: a restart in any state resumes or reports the job. There are no lost invoices.
- **R-9.5 (M)** Observability: structured JSON logs, a `/health` endpoint, and per-job step timings. Secrets are never logged.
- **R-9.6 (M)** Configuration: one YAML file plus `.env` for secrets. Changing flows, portals or templates needs a restart at most (hot reload: C).
- **R-9.7 (S)** Data retention: local temporary files are deleted after delivery. The Postgres record is kept. Originals live in Drive or OneDrive.
- **R-9.8 (M)** Language: user-facing messages in Spanish (es-PE), code and logs in English.

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
| `/gaps` | *(admin)* List open gaps with their issue and PR links | S |
| `/gap ignorar <id>` | *(admin)* Stop alerting for one gap fingerprint | S |

## R-11 · Business rules, gaps and alerts
- **R-11.1 (M)** Every stop, warning or reroute SHALL reference a rule ID from `specs/06-business-rules.md`, and SHALL use that rule's message from the catalog. A test SHALL fail if any rule in the spec has no catalog entry, or the other way round.
- **R-11.2 (M)** WHEN a rule marked as a gap fires, THE SYSTEM SHALL upsert a `gaps` row by fingerprint (count, last_seen, linked invoice IDs). Detecting a gap SHALL **never** block the fetcher chain.
- **R-11.3 (M)** The chat layer SHALL tell the user, in plain words, what is missing and whether the bot will retry by itself (`{gap_line}`).
- **R-11.4 (S)** WHEN `PORTAL_MISSING` fires for an issuer for the first time, THE SYSTEM SHALL ask the user for the portal link, once per issuer and not once per invoice (BR-F02). Answers SHALL be validated (only `https`, a public hostname, ≤ 300 characters, no credentials in the URL) before they are stored.
- **R-11.5 (M)** WHEN a gap is new (or reopened), THE SYSTEM SHALL, through the outbox and with retries:
  1. open a GitHub issue (labels `gap`, `gap:<kind>`) from a template, including only the data allowed by the privacy rule;
  2. for `PORTAL_MISSING` and `PORTAL_BROKEN`, create branch `gap/<fingerprint-slug>`, commit the stub or status change to `config/portals.yaml` (an append or a single-line edit that keeps comments intact), and open a **draft PR** that says `Closes #<issue>`;
  3. never push to the default branch.
- **R-11.6 (M)** CI SHALL run `scripts/check_config.py` and **fail** while any `portals.yaml` entry is `todo` or `broken`, or while config fails validation. A gap PR is therefore red until someone completes it.
- **R-11.7 (S)** A scheduled workflow (`gap-watch`, daily) SHALL fail while any `gap` issue has been open for more than `gap_sla_days` (default 3). This keeps the Actions status red and triggers GitHub's failure email.
- **R-11.8 (M)** WHEN a gap is new, THE SYSTEM SHALL send **one** WhatsApp message to each `admins` number with the kind, the subject and the issue/PR links. Kinds marked "digest" go into a daily summary instead. Alerts for the same fingerprint SHALL be rate-limited to one per `realert_days` (default 7).
- **R-11.9 (M)** WHEN the deployed config changes (a pull from the default branch, then a hot reload of `portals.yaml`, `parsing_rules.yaml` and routing), THE SYSTEM SHALL re-evaluate open gaps. For each gap that is now fixed, it SHALL re-enqueue the linked invoices (`MANUAL_REQUIRED`/`PENDING_AUTH` → `FETCHING_DOCS`/`DELIVERING`) and tell their senders.
- **R-11.10 (M)** GitHub access SHALL use a credential scoped to **this repository only**, with Issues, Pull requests and Contents (write) permissions. The daily budget for GitHub writes is ≤ 20 operations. Failures to reach GitHub SHALL NOT affect invoice processing; they are retried from the outbox.
- **R-11.11 (S)** IF GitHub is disabled or unreachable for more than 24 h, THE SYSTEM SHALL still alert the admin on WhatsApp, so the alert path never depends only on GitHub.

## R-12 · Integration contract (D39, constitution P10)
Consumers (Jarvis, n8n, future apps) integrate only through this surface. The bot works fully without any consumer.
- **R-12.1 (S)** THE SYSTEM SHALL expose a versioned REST API (`/api/v1`, OpenAPI published) to list and get invoices, filtered by date, flow, issuer and state, and to read spend summaries by period, flow and issuer.
- **R-12.2 (S)** THE SYSTEM SHALL expose an **MCP server** (streamable HTTP) with read tools (`list_invoices`, `get_invoice`, `spend_summary`, `pending_manual`, `list_gaps`) and write tools (`reprocess_invoice`, `cancel_invoice`, `attach_document`). Write tools go through the same state machine and business rules as chat commands.
- **R-12.3 (S)** Every API and MCP client SHALL authenticate with a **scoped token** (`read`, `write`), stored hashed and revocable. A missing or invalid token gets 401; the read scope is the default.
- **R-12.4 (S)** WHEN an invoice reaches `DELIVERED`, `MANUAL_REQUIRED`, `PENDING_USER` or `FAILED`, or a gap is raised or closed, THE SYSTEM SHALL POST a versioned event (`invoice.delivered`, `invoice.manual_required`, `invoice.pending_user`, `invoice.failed`, `gap.raised`, `gap.closed`) to every configured subscriber, signed with HMAC-SHA256, through the outbox with retries. The existing `n8n_hook` sink becomes one subscriber of this mechanism.
- **R-12.5 (S)** Events and API responses SHALL include only invoice data and ids, never secrets, OAuth tokens or raw credentials.
- **R-12.6 (C)** A breaking change to the API, MCP tools or event schema SHALL ship as a new version, with the previous version kept for at least one release.
- **R-12.7 (M)** The bot SHALL NOT import, call or require any consumer for its core flows. An unreachable subscriber only delays its own events.
