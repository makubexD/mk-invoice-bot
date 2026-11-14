# 02 · Requirements

Acceptance criteria use **EARS** syntax: *WHEN* (trigger), *IF* (unwanted condition), *WHILE* (state), and *THE SYSTEM SHALL* (response).
Priority: **M** = must, in the MVP; **S** = should, in v1; **C** = could (later).
**v1 = the MVP = milestones M1–M7** (spec 04). Requirements marked **v1.1** (R-1.7 and all of R-12, milestone M8) and stories marked *post-MVP* ship after it, whatever their letter.

## Glossary (Peru and SUNAT)
| Term | Meaning |
|---|---|
| **SUNAT** | Peru's tax authority. It defines electronic invoices and keeps the RUC registry |
| **RUC** | 11-digit taxpayer number with a mod-11 check digit. **20…** = a company (*persona jurídica*); **10…** = a person with a business (*persona natural*); a RUC 10 contains that person's 8-digit DNI. **15/17** exist but are rare |
| **DNI** | 8-digit national ID of a person |
| **CPE** | *Comprobante de pago electrónico*, an electronic invoice of any kind |
| **`tipo`** (document type) | `01` **factura** (buyer must have a RUC; deductible; what an employer reimburses), `03` **boleta** (buyer is a person with a DNI or nobody), `07` credit note, `08` debit note |
| **Serie-número** | Document id, e.g. `F014-10158`. The serie's first letter usually tells the type: **F** factura, **B** boleta, **E** issued from SUNAT's own portal; some issuers use 4 digits |
| **Issuer / `ruc_emisor`** | The store that issues the invoice (always a RUC, 10 or 20) |
| **Buyer / `tipo_doc_adq`, `num_doc_adq`** | Who the invoice is made out to. `tipo_doc_adq` is `6` = RUC, `1` = DNI, `0` or `-` = none. **Routing uses the buyer** (R-3.1) |
| **IGV** | Peru's VAT: 18%, or a reduced 10% / 10.5% for small restaurants and hotels under Ley 31556 (the reduced rate changes by year) |
| **Base / OP. GRAVADA** | The taxable amount before IGV |
| ***Recargo al consumo*** | Restaurant service charge, outside the IGV base; in the XML an `AllowanceCharge` (codes 46/50) |
| **ICBPER** | Small fixed tax per plastic bag |
| **Padrón RUC** | SUNAT's public list of every RUC with its name, status (`ACTIVO`…) and address condition (`HABIDO`…) |
| **SOL / Clave SOL** | SUNAT's online portal and its login |
| **OSE / PSE** | Companies that issue and validate CPEs for stores; many store "portals" are really one PSE's site shared by many stores |
| **UBL 2.1** | The XML format of a CPE; signed with **XMLDSig** |
| **Comercio** | The short store name the owner uses in folder names (`LaLena`), from the store directory (R-5.8) |

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
- **R-1.1 (M)** WHEN an allowlisted sender sends an image (photo or image document) or a PDF of the invoice (D58), THE SYSTEM SHALL acknowledge with a single message within 5 s and enqueue a job.
- **R-1.2 (M)** IF the sender isn't allowlisted, THE SYSTEM SHALL ignore the message and log it with the number or user id hashed (R-9.12), without replying.
- **R-1.3 (M)** THE SYSTEM SHALL verify `X-Hub-Signature-256` on every webhook call and reject invalid ones with 401.
- **R-1.4 (M)** THE SYSTEM SHALL fetch media by its **media id**, which stays valid for 7 days. Because the media URL expires 5 minutes after it is issued, THE SYSTEM SHALL request a fresh URL on every download attempt, including retries. Downloading within 60 s of the webhook is a soft target, not a correctness condition.
- **R-1.5 (S)** WHEN a sender sends a document while an invoice is in `MANUAL_REQUIRED` or `CONFIRMING`, THE SYSTEM SHALL attach it to that invoice if the invoice's flow lists that kind in `manual_uploads` (flow B: `[pdf, xml]`, because the employer asks for both; each flow sets its own, D66). Matching is **per sender profile, across channels**: the invoice is the one the message quotes, or else the most recent open invoice of the same profile. An XML is parsed as in R-2.12 and must match the invoice (R-5.6). Delivery resumes when every `required_docs` entry is present; until then BR-D09 says what is still missing. A document can never **start** an invoice unless it is a photo or a PDF (D58).
- **R-1.6 (S)** WHEN a sender sends several photos in a row, THE SYSTEM SHALL process them as independent jobs, serialized (FIFO).
- **R-1.7 (S, v1.1)** THE SYSTEM SHALL support **Telegram** as a second channel through the same port (D41): allowlist by Telegram user id, webhook verified with `X-Telegram-Bot-Api-Secret-Token`, inline keyboards in place of reply buttons. Each sender profile can link one WhatsApp number and one Telegram id.
- **R-1.8 (M)** Webhook processing SHALL be idempotent **per channel message id** (`(channel, external_id)`), because Meta and Telegram retry deliveries.
- **R-1.9 (M)** THE SYSTEM SHALL handle every channel through one `Channel` port with a canonical inbound and outbound message (PT-1). Each invoice SHALL store its `profile_id` and `origin_channel`. Replies about an invoice SHALL go to its origin channel. Admin alerts SHALL go to the owner profile's preferred channel.
- **R-1.10 (M)** Before sending on WhatsApp, the outbox SHALL check the 24 h customer-service window per recipient. Outside the window, THE SYSTEM SHALL send the matching approved **utility template** (completion, manual-required, resume notice, pending-user reminder, batch preview, admin alert). IF Meta answers error 131047, THE SYSTEM SHALL map it to `WINDOW_EXPIRED` and resend as the template (PT-4, BR-S04). Templates are charged per message with no free tier, so the happy path SHALL stay inside the window. The batch preview template is post-MVP, with `on_command` (R-7.5).
- **R-1.11 (M)** THE SYSTEM SHALL enforce media limits before processing: reject images whose header declares more than 40 megapixels **before decoding**; apply byte caps per media type (`limits.media` in config, never above the channel's own limit, e.g. Telegram `getFile` 20 MB); cap zip entry count and compression ratio. A rejected file gets BR-E07. IF the worker dies twice on the same job, THE SYSTEM SHALL move the invoice to `FAILED` (crash-loop guard, BR-S03).

## R-2 · Extraction (deterministic first)
- **R-2.1 (M)** THE SYSTEM SHALL try QR decoding on every photo, alongside OCR (R-2.3, D81), with a cascade of zxing-cpp → WeChat QR → pyzbar on preprocessed variants (original, grayscale, upscaled, sharpened, rotated 90°/180°/270°), stopping at the first valid SUNAT payload.
- **R-2.2 (M)** WHEN a SUNAT QR is decoded, THE SYSTEM SHALL extract: `ruc_emisor`, `tipo` (01/03/07/08), `serie`, `numero`, `igv`, `total`, `fecha_emision`, `tipo_doc_adq`, `num_doc_adq`. A payload with a wrong or empty document-type or buyer-type part is repaired, not rejected (D79).
- **R-2.3 (M)** THE SYSTEM SHALL run OCR (OpenCV preprocessing + Tesseract `spa`) on every photo, whether or not a QR decodes, and a regex/keyword parser that extracts the same fields plus `razon_social` (header text). The OCR and QR readings are merged (D81):
  - a field both give with the same value gets confidence 1.0;
  - when they disagree, the OCR value stays and the field is flagged `conflict`;
  - a field only one of them gives comes from that one.
- **R-2.4 (M)** THE SYSTEM SHALL validate:
  - the RUC check digit (mod 11);
  - the serie pattern (`[FBE][A-Z0-9]{3}` or 4 digits);
  - the date (not in the future, not older than the configured limit);
  - `total ≈ base + IGV + other charges − discounts` (tolerance S/ 0.05) when base and IGV are present. Other charges are the restaurant service charge (*recargo al consumo*) and ICBPER: 26 of the owner's 35 work invoices in 2026 carry a service charge, so a check without it would flag them wrongly;
  - `IGV ≈ base × rate` (tolerance S/ 0.05) for a rate in the configured list `extraction.igv_rates` (today 0.18, plus the reduced restaurant rates 0.10 and 0.105 under Ley 31556, which change by year; all three appear in the 2026 invoices). A miss marks `igv` as `needs_review`; it never rejects the invoice.
  - Outcomes: a bad RUC check digit → BR-E02; a date out of bounds → BR-E03; a `tipo` outside `extraction.supported_tipos` → BR-E05; any other failed check, or a required field below `review_threshold` → `CONFIRMING` with BR-E09.
- **R-2.5 (M)** THE SYSTEM SHALL resolve `razon_social` and `estado/condición` of the issuer from the local padrón RUC. OCR text is the fallback.
- **R-2.6 (M)** Each field SHALL carry `source ∈ {qr, xml, ocr, padron, llm, user}` and `confidence ∈ [0, 1]`. It MAY also carry a flag: `inferred` for a value the reader deduced (D79), or `conflict` for an OCR value the QR contradicts (D81). Both flags are shown on the summary's warning line (D80).
- **R-2.7 (S)** WHEN the official XML is obtained, THE SYSTEM SHALL read it with the hardened parser (R-2.12) for two things only: to check it is the right invoice (R-5.6), and to take its amounts (base, IGV, service charge, ICBPER, discounts, total) as authoritative. Any discrepancy from the photo data is reported in the completion message. IF the XML can't be read, the photo's values are kept and marked `needs_review` (D57).
- **R-2.8 (S)** IF the QR contains a URL instead of data, THE SYSTEM SHALL store it as `document_url` for the fetcher.
- **R-2.10 (S)** THE SYSTEM SHALL capture URLs printed on the receipt (OCR regex such as `consulte .* en (\S+)`) as `printed_urls`. They are hints for the gap issue (R-11.5, R-11.12), not fetch targets.
- **R-2.9 (M)** Extraction accuracy target: ≥ 95% of invoices with a readable QR (`has_qr: true` in the expected JSON) get every QR field exactly right; with OCR only, ≥ 80% of the required fields (R-2.13) are right. A field is right when it equals the expected value (amounts compared as decimals, dates as ISO dates). The target is measured on the **private real set** (the owner's photos, each paired with its official XML) by a local script, and the aggregate result, with no RUCs, is recorded in `specs/spikes/accuracy.md` (D54). CI runs the committed synthetic set as a regression check with the same thresholds.
- **R-2.11 (C)** IF QR, XML and OCR all fail to give the required fields, AND `llm_fallback` is enabled **for the invoice's flow** (per-flow opt-in, off by default, **never flow B by default**; D24, D43), THE SYSTEM SHALL send the downscaled image to the configured vision LLM through a provider-neutral port. Every returned field SHALL pass the R-2.4 validators, be tagged `source=llm` with confidence ≤ 0.5, and **always** require user confirmation (overrides `confirm_mode: auto`). Raw LLM text is never shown or stored outside the validated fields. Each call is logged in `llm_usage` with tokens and cost and counts against a monthly cap; at the cap the step is skipped with BR-E08.
  - Note: the flow is only known after routing. When the buyer can't be read, the LLM runs only if every flow the sender's profile may use has opted in.
- **R-2.13 (M)** The **required fields** are `ruc_emisor`, `tipo`, `serie`, `numero`, `fecha_emision` and `total`. Without all of them, extraction ends in BR-E01. The buyer is not required here: a missing buyer is handled by routing (BR-R03).
- **R-2.14 (M)** WHEN present, THE SYSTEM SHALL also extract `base`, `service_charge` (*recargo al consumo*), `icbper` and `discounts`, from the XML or by OCR, because R-2.4 and the flow B Excel need them.
- **R-2.15 (M)** WHEN the input is a PDF, THE SYSTEM SHALL read its text layer and any QR first; IF it has no usable text, it SHALL render page 1 to an image and continue as for a photo. A PDF over the byte cap or with more than 5 pages is rejected with BR-E07 (D58).
- **R-2.12 (M)** THE SYSTEM SHALL parse XML with entity resolution, DTD loading and network access disabled, reject any document with a `DOCTYPE`, and cap XML size at 2 MB (BR-F09). Signature checks (XMLDSig) are post-MVP (D57): until then an XML is trusted only through the R-5.6 match.

## R-3 · Routing and confirmation (no flow menu)
- **R-3.1 (M)** THE SYSTEM SHALL pick the flow **automatically** with ordered routing rules (first match wins). The default rules are buyer RUC = owner RUC → A (BR-R01) and buyer RUC = work RUC → B (BR-R02). Rules can match on `num_doc_adq`, `tipo_doc_adq`, `ruc_emisor`, `tipo` and the sender profile, so adding a flow C later is a config change, not new code. Each flow also declares `accepted_tipos` (for example flow B: `["01"]`, facturas only); an invoice routed to a flow that doesn't accept its type stops with BR-R08. Everything that differs between flows (routing, accepted types, fetchers, required docs, storage, mail, templates, cap) is config; adding or changing a flow never touches the pipeline or another flow (D53). *Note:* a flow C also needs the R-3.2 question to use a list message (see there).
- **R-3.2 (M)** IF no rule matches (unknown RUC, DNI or no buyer) and `routing.unmatched: ask`, THE SYSTEM SHALL ask with **one** message with reply buttons for the candidate flows plus Cancel (BR-R03). This is the **only** situation with a flow menu. IF `routing.unmatched: reject`, THE SYSTEM SHALL cancel with BR-R07.
  - Note: WhatsApp allows at most 3 reply buttons (titles ≤ 20 characters), so BR-R03 (two flows plus Cancelar) is at the limit. A third flow needs list-message support first (up to 10 rows). Telegram inline keyboards have no such limit.
- **R-3.3 (M)** IF the buyer field came from OCR with confidence below `review_threshold`, THE SYSTEM SHALL ask for confirmation with the detected flow pre-selected (BR-R04).
- **R-3.4 (M)** WHEN routing succeeds and every required field is `ok`, THE SYSTEM SHALL send the summary (fields plus the detected flow) and **continue without waiting** (`confirm_mode: auto`). The summary carries **Editar** and **Cancelar** buttons, which stay valid until delivery starts. `confirm_mode: always` restores the explicit confirmation step.
- **R-3.5 (S)** WHEN the user picks "Editar", THE SYSTEM SHALL accept `field=value` lines (for example, `total=45.50`), re-validate, re-route if the buyer changed, and show the summary again. Short aliases are accepted: `ruc` = `ruc_emisor`, `fecha` = `fecha_emision`; `tipo` is inferred from the serie's first letter (F → 01, B → 03) when not given.
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
- **R-5.1 (M)** THE SYSTEM SHALL try fetchers in the **order configured per flow**, stopping when every document in the flow's `required_docs` has been found (flow B: PDF and XML; flow A: XML, with the PDF desired). WHEN a fetcher fails for good (including the last of its scheduled retries), THE SYSTEM SHALL pass to the **next fetcher** in the order, never straight to `MANUAL_REQUIRED`. Retries are scheduled jobs, so a waiting fetcher never holds the worker. WHEN a portal answers that the invoice doesn't exist yet, THE SYSTEM SHALL retry it on the portal's `retry_schedule` (default +1 h, +6 h, +24 h, +72 h; stores often publish hours or days later) and show the invoice as "esperando al portal"; only after the last retry does the chain move on (D65).
- **R-5.2 (M)** Fetchers available:
  1. **`qr_url`:** follow the URL printed in the QR or on the receipt.
  2. **`portal_registry`:** look up the issuer RUC → portal definition (generic YAML form adapter or custom adapter).
  3. **`sunat_sol`:** *(only when the buyer is the RUC of the SOL account; today flow A)* log into SOL with the owner's secondary user, query received CPE, and download the XML.
  4. **`email_inbox`:** search the configured mailbox (Gmail or Outlook) for `serie-numero` / issuer RUC and download the XML/PDF attachments.
  - The list order is only the default for flow A (`qr_url, portal_registry, sunat_sol, email_inbox, manual`); each flow sets its own order.
  5. **`paid_api`:** *(optional, off by default)* Factiliza, json.pe or similar.
  6. **`manual`:** always the last step.
- **R-5.3 (M)** WHEN a portal shows an image captcha, THE SYSTEM SHALL first try local OCR (≤ 2 attempts). It SHALL then send the captcha image to the user and wait up to 5 min for a text reply.
- **R-5.4 (S)** WHEN a portal shows reCAPTCHA/hCaptcha, THE SYSTEM SHALL skip to the next fetcher, unless a paid solver is enabled.
- **R-5.5 (M)** IF every fetcher fails, THE SYSTEM SHALL set `MANUAL_REQUIRED` and send BR-F07: which fetchers failed and why, one line each, using the BR-F catalog. It SHALL accept the files later (R-1.5).
- **R-5.6 (M)** A fetched XML SHALL be checked to match the invoice: its RUC, serie, número and total equal the confirmed data.
- **R-5.7 (S)** THE SYSTEM SHALL record per-portal success and failure stats, so you can see which registry entries are broken.
- **R-5.8 (M)** Store and portal knowledge SHALL live in a **directory** of small YAML files (D47):
  - `stores/<ruc>.yaml`: one file per issuer RUC with `razon_social`, `tipo` (restaurante, grifo, …), `brands` (each with `comercio`, the short name used in folder names, and an optional `serie_prefix` list that picks the brand when one RUC runs several), the `portal` key, and free `notes`. `portal: manual` marks a store with no website: its files can only come from the store itself, so the chain skips to `manual` with BR-F11, and the store's contact (a phone number) lives only in the private config. A RUC with several brands and no matching prefix gets its brand from a one-tap question.
  - `portals/<key>.yaml`: one file per portal site, shared by every store that uses it: `url`, `adapter` (`generic_form`, or `python:<module>` accepted only under `invoicebot.fetchers.portals.*`, checked by `check_config.py`), `fields` (form field → invoice expression), `captcha ∈ {none, image, recaptcha}`, `downloads`, `retry_schedule`, and `status ∈ {active, todo, broken}`. A URL may use `{ruc_emisor}` (some providers have one page per issuer). Only `active` portals are used.
  - The directory lives in the repo under `config/stores/` and `config/portals/`, by the owner's decision, including persons with a business, whose RUC 10 contains a DNI (D47). CI's real-RUC check (R-9.17) accepts a RUC only as the `ruc:` of `config/stores/<ruc>.yaml`, the file named after it; any other real RUC still fails. Changes reach the VPS through the CI-gated deploy and a `SIGHUP` reload (D46, R-11.9).
- **R-5.10 (M)** Every outbound request to a URL that came from outside the code (QR URLs, printed URLs, taught links, portal entries) SHALL pass **one request guard** shared by `httpx` and Chromium:
  - `https` only; `file:`, `data:` and other schemes are blocked. The one exception is a portal file that sets `allow_http: true` (D76): `http` is then allowed to that portal's own host and nowhere else, and every other check still applies;
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
Flow B mirrors the owner's existing manual process for work meal reimbursements (D48). The values below are the owner's defaults; every one of them is config.
- **R-7.1 (M)** THE SYSTEM SHALL create **one folder per invoice**, named from a configurable template (default `{fecha:%Y}-{fecha:%m}-Support_{seq}_{comercio}`) followed by a status suffix (R-7.11). `seq` is the next free number among the flow's folders for the invoice's month; folders are never renumbered. The count includes every folder already there for that month, whether the bot or the owner made it, in both the `2026-7` and `2026-07` styles, on the bot's main storage target; folders the owner makes by hand only in the work OneDrive are invisible to it, so manual folders stop once the bot is live. `comercio` comes from the store directory (R-5.8). IF the issuer has no store file, `comercio` is a CamelCase slug of the first two words of its `razon_social`, and the missing store is raised as a `PORTAL_MISSING` gap. IF the store has several brands and no serie prefix matches, the sender picks one (BR-R09).
- **R-7.2 (M)** The folder SHALL hold exactly the filled Excel, the PDF and the XML, named by configurable templates (default `Formato de reembolsos.xlsx`, `{profile.legal_name}.pdf`, `{profile.legal_name}.xml`), plus a small `.invoicebot.json` manifest with the invoice id, and the `.eml` draft when `eml_draft` is on. The photo never goes in the folder, and the bot deletes it once the batch is sent (R-9.7, D58).
- **R-7.3 (M)** Storage targets SHALL be used in the configured order (D49):
  1. **`onedrive_personal`** (default main copy): the owner's personal Microsoft account through Graph; no work-tenant consent needed.
  2. **`onedrive_work`**: the work OneDrive folder, only once the work tenant allows the bot's app (S0.8). The personal copy then becomes a backup.
  3. **Laptop agent** (outside the bot, R-7.14): a script on the owner's laptop, where both OneDrive accounts sync, copies new folders and applies renames and moves, one way, from the personal copy into the work folder, matching folders by the manifest's invoice id, never by name.
- **R-7.4 (M)** THE SYSTEM SHALL fill **one Excel file per invoice** from the pinned template by changing only the **mapped cells** (`cell → expression`) inside the `.xlsx` package and leaving every other part byte-for-byte (D71): a load-and-save with openpyxl drops the template's drawn shapes, custom metadata and the cached value of the total. The cached values of the formula cells that depend on the written cells are written too, so the total shows in previews that don't recalculate. The default reason (`Support release`) and description (`Cena en {comercio}`) are config; the sender can override either with `motivo=…` / `descripcion=…` as the photo's caption, or later as a reply to the bot's summary; a later override regenerates the Excel (and the prepared email) any time before the batch is `sent` (D70). Subtotal, IGV, service charge and total come from the XML (R-2.7), or, with no readable XML, from the photo or PDF marked for review, and are written as numbers, replacing the template's formulas in those cells; the cap formula (`=MIN(I16, 50)`) stays.
- **R-7.5 (M)** **Email trigger:** `per_invoice` in the MVP: each invoice is a batch of one, and its email is prepared as soon as its folder is complete. `on_command` (`/enviar`) and `scheduled` (cron) are post-MVP and may be combined with it later.
- **R-7.6 (M)** Recipients (to, cc), subject and body SHALL be templates in config. Defaults: subject `Support release {fecha:%d/%m/%Y}`, exactly, with no tag or id added (D69); body `Hola,` / `Adjunto los documentos solicitados para el reembolso.`; attachments the Excel, PDF and XML; above 3 MB in total, an upload session (Graph) or a zip. Real addresses live only in the VPS config, never in git.
- **R-7.7 (M)** **Mail modes** (D49), configured per flow; several can be on at once:
  1. **`dry_run`** (default): writes the email as an `.eml` file in the bot's storage and logs it; nothing leaves the server. It is what every mode does while `mail.send_enabled` is false; real sending needs `mail.send_enabled: true`, which startup refuses unless `env: production`.
  2. **`inbox_rule`** (D61, D67; the default for the work account when its tenant gives no consent): the bot sends the prepared email from its own **dedicated mailbox** (D63) to the owner's work address only. An Outlook rule the owner creates (sender = the bot's mailbox and subject starts with the configured prefix) forwards it to HR. The rule is **trusted**: the bot can't see the forward, so the batch is `sent` once Graph accepts the email to the owner's inbox, and the completion message carries a **[No llegó]** button (BR-D13).
  3. **`laptop_outlook`** (D61): the bot drops the email as a job file in a synced `_outbox/` folder of the personal OneDrive; the laptop agent sends it through Outlook classic **from the owner's work account** and writes a receipt file back (R-7.14).
  4. **`forward_to_self`**: like `inbox_rule` but without the rule: the owner forwards the email to HR by hand from Outlook, then taps [Ya lo envié].
  5. **`eml_draft`**: writes an unsent draft (`X-Unsent: 1`) into the invoice folder; opened in Outlook desktop it shows recipient, subject, body and attachments ready to send.
  6. **`graph`**: sends directly through Graph from the account named in `mail.account`, to the configured recipients. `account: personal` is the first mode built: during the MVP its recipient is the owner's own test inbox, never HR (D68). `account: work` (written `graph_work` elsewhere in these specs) works only once the work tenant allows the app. Sender account and recipients are config, so moving between accounts is a config change.
  - **Order and fallback** (D61, D67): the flow lists its modes in order of preference (default without consent: `inbox_rule`, then `laptop_outlook`, then `forward_to_self` and `eml_draft`). Only the first automatic mode sends. THE SYSTEM SHALL NOT send through a second mode on its own, so HR never gets a claim twice: another mode is used only when the sender asks for it (BR-D11), after tapping [No llegó] or when a laptop receipt is overdue.
  - The **phone route** (sharing files from WhatsApp into Outlook or OneDrive on the owner's phone) is not a mode. It stays a manual last resort only if the S0.11 test shows Intune allows it.
  - A Gmail mailer is post-MVP (S11.10).
- **R-7.8 (M)** **When a batch is `sent`**, per mode: `graph`, when Graph accepts the message (BR-D10); `inbox_rule`, when Graph accepts the bot's email to the owner's inbox (BR-D13, trusted, D67); `laptop_outlook`, when the agent's receipt file appears (BR-D10); `forward_to_self`, `eml_draft` and `dry_run`, when the sender taps **[Ya lo envié]** on BR-D07 (BR-D08). A batch left `prepared` for 2 days is listed by `/pendientes`. Jobs and receipts are matched by job id; nothing is added to the subject (D69).
- **R-7.9 (M)** Microsoft Graph SHALL use minimal delegated scopes, one token cache per account (D63): the owner's personal account `offline_access Files.ReadWrite` (folders and the `_outbox/` exchange); the bot's dedicated mailbox `offline_access Mail.Send` (it reads no mail); the personal account also gets `Mail.Send` only while `graph` with `account: personal` is on; the work account `offline_access Files.ReadWrite Mail.Send` only when `onedrive_work` or `graph_work` is on. IF a tenant blocks device-code flow, THE SYSTEM SHALL offer an **auth-code + PKCE loopback** login. THE SYSTEM SHALL NOT sign in with another publisher's client id to get around a tenant's consent policy.
- **R-7.10 (M)** Excel writes SHALL store text values as strings (never formulas) with the same leading-character escape as R-6.6; numbers from validated decimal fields are written as numbers. THE SYSTEM SHALL pin the template's SHA-256 in config and refuse a template that doesn't match (BR-D02). `.xlsm` templates (`keep_vba`) SHALL be used only with an explicit opt-in. The template holds personal data (DNI, bank account, signatures), so it lives only in OneDrive and the bot's storage, never in git.
- **R-7.11 (M)** **Folder status:** the folder name gets `_Sent` when its batch is `sent` (R-7.8), and `_PAID_{dd}_{mm}___{amount}` when a payment is recorded (R-7.12), where `amount` is the payment's total with `_` for the decimal point (`144_6`). On PAID the folder moves into the `{fecha:%Y}` year folder at once. Each rename or move is a job with the usual retries. Example: `2026-11-Support_2_LaLena` → `2026-11-Support_2_LaLena_Sent` → `2026/2026-11-Support_2_LaLena_Sent_PAID_15_12___144_6`. The amount drops trailing zeros: `42.00` → `42`, `143.90` → `143_9`.
- **R-7.12 (M)** `/pagado dd/mm monto` SHALL record a reimbursement: the bot looks for sets of the sender's `sent`, unpaid invoices whose claimed amounts (capped, R-7.13) add up to `monto`, and asks for confirmation (BR-P01 to BR-P03). `/pagado dd/mm #12 #14` names the invoices directly. On confirmation it stores the payment, applies R-7.11 to each folder and replies with BR-P04. Candidates are the sender's sent, unpaid invoices of the last 180 days, at most 20.
- **R-7.13 (M)** IF an invoice total is above the flow's `reimbursement_cap` (default S/ 50), THE SYSTEM SHALL warn and wait for [Continuar] or [Cancelar] before delivery (BR-D06). The claimed amount is `min(total, cap)`, the same as the template's cap formula.
- **R-7.14 (M)** **Laptop agent** (D61): a PowerShell script run every 5 minutes by a Windows scheduled task on the owner's laptop. It talks to the bot **only through files in the personal OneDrive**; nothing connects to the laptop. It SHALL:
  1. mirror folders as in R-7.3;
  2. for each job file in `_outbox/` that carries a valid HMAC (a key shared by the bot and the agent, so a stranger who can write to the OneDrive folder can't make the laptop send mail), send the email through Outlook classic (COM) from the work account, but only to recipients on its own local allowlist (the configured HR address), only with attachments inside that invoice's folder, and only once per job id;
  3. write `_outbox/receipts/<job id>.json` (sent time, or the error), then delete the job file;
  4. log locally without message bodies, and support `-DryRun`.
  IF a job file is older than `agent_stale_after` (default 4 h, the laptop is probably off), THE SYSTEM SHALL tell the sender once (BR-D12). The new Outlook for Windows has no COM automation, so `laptop_outlook` needs Outlook classic (S0.11).
- **R-7.15 (M)** **Interface** (D62): chat (WhatsApp, later Telegram) is the only v1 interface, for capture, questions and notifications. There is no native mobile app: on a company-managed phone it could neither use Outlook's or OneDrive's sign-in nor pass files to them any more than WhatsApp can. After v1, a small mobile web page ("mis reembolsos": pending, sent, paid, pick a payment) is served by the bot behind a login, on the v1.1 API (E17).
- **R-7.16 (M)** **Go-live import** (D72): a one-time script reads reimbursement folders the owner points it to (their XML and folder names) and registers each claim that is sent but not yet paid as a `DONE` flow B invoice with its month number, so `seq` and `/pagado` are right from the first day. It writes nothing to those folders.

## R-8 · Completion message
- **R-8.1 (M)** WHEN a flow completes (flow A: `DONE`; flow B: `BATCHED` with its email ready, BR-D07), THE SYSTEM SHALL send one chat message, on the invoice's origin channel, containing:
  - the status (✅ / ⚠️ docs pending / ❌);
  - the key fields;
  - links (Sheet row and Drive folder, or OneDrive folder); for flow B, the folder name and the [Ya lo envié] button.
  - The [Ya lo envié] tap gets a one-line reply (BR-D08), not a second completion message.
- **R-8.2 (M)** IF any step fails irrecoverably, THE SYSTEM SHALL say which step failed and what the user can do.

## R-9 · Non-functional
- **R-9.1 (M)** Memory: steady state ≤ 350 MB RSS, peak ≤ 800 MB for the `app` container (Postgres excluded, ≤ 300 MB on its own), verified by a load test of 10 queued invoices.
- **R-9.2 (M)** Latency: acknowledgement in < 5 s. Summary in < 60 s, on 1 vCPU. There is one budget, because every photo runs OCR (D81).
- **R-9.3 (M)** WhatsApp messages per invoice: ≤ 3 in the happy path (acknowledgement, summary, completion; for flow B the completion is BR-D07, plus the one-line reply to the [Ya lo envié] tap), thanks to auto-routing; ≤ 5 with a gap question. All of them are service messages inside the 24 h window, which are free up to 1,000 per month per number. Templates (R-1.10) are charged per message, so they are used only outside the window.
- **R-9.4 (M)** Durability: a restart in any state resumes or reports the job. There are no lost invoices.
- **R-9.5 (M)** Observability: THE SYSTEM SHALL write one JSON object per log line with at least `ts`, `level`, `event`, `invoice_id`, `job_id`, `step`, `rule_id` and `duration_ms`, and SHALL record the duration of every pipeline step per job, so `/estado` and the logs show where time went. `/health` is defined in R-9.14. Secrets are never logged (R-9.16).
- **R-9.6 (M)** Configuration (D59): the store directory (`config/stores/`, `config/portals/`) comes from git through the CI deploy; one private `config.yaml` with the real values (HR address, work RUC, accounts) lives only on the VPS, never in git, and is merged with it at start and on `SIGHUP`; `.env` holds other **non-secret** settings; secrets come from the encrypted secrets file (R-9.9). Git-tracked config reaches the host only through the CI deploy (D46). WHEN the process receives `SIGHUP`, THE SYSTEM SHALL validate the new config and swap it in only if it passes; a bad config keeps the old one and alerts the owner.
- **R-9.7 (S)** Data retention: local temporary files are deleted after `DONE` (flow B: after the batch is sent). The Postgres record is kept. Originals live in Drive or OneDrive. Per-table retention is R-9.12.
- **R-9.8 (M)** Language: user-facing messages in Spanish (es-PE), code and logs in English.
- **R-9.9 (M)** Secrets SHALL be stored in a file encrypted with `age` (or as `systemd-creds`) and decrypted into memory at start; they are never written to disk in plain text. `.env` holds only non-secret settings. The `kv` token cache SHALL be excluded from plain dumps or encrypted.
- **R-9.10 (M)** Each adapter SHALL receive only the secrets it declares. Only `sunat_sol` gets SOL credentials, and it SHALL type them only when `page.url` matches the SUNAT origins hard-coded in source. SOL credentials SHALL never leave the host, including to paid APIs.
- **R-9.11 (M)** Backups: THE SYSTEM SHALL run a nightly `pg_dump` plus a config backup, encrypted to an `age` public key and stored off-site (RPO ≤ 24 h). A restore drill into an empty Postgres SHALL be run and documented before go-live and after schema changes. A failed backup alerts the owner (BR-O04).
- **R-9.12 (M)** Retention per table: raw `messages.payload_json` 30 days; `outbox`, `events` and `jobs` 90 days; `invoices` kept. Log lines about non-allowlisted senders SHALL store only a hash of the number or user id.
- **R-9.13 (M)** Migrations SHALL follow **expand/contract** (a release never drops or renames something the previous image still uses). The deploy SHALL `pg_dump` before migrating, and rollback means redeploying the previous image.
- **R-9.14 (M)** Health and self-alerts: `/health` SHALL check the DB connection and return 503 when it fails. An external uptime check SHALL poll it. THE SYSTEM SHALL alert the owner about failed jobs (BR-S03), outbox backlog (BR-O01), disk usage (BR-O02) and channel token problems (BR-O03).
- **R-9.15 (M)** IF Postgres is down, the webhook SHALL return 503 without acknowledging, so Meta (and Telegram) retry the delivery. The runbook documents this behaviour.
- **R-9.16 (M)** Logs SHALL redact secrets, including the Telegram bot token that appears in request URLs. A test SHALL prove that no configured secret appears in captured log output. Logs SHALL NOT contain extracted values (RUCs, DNIs, names, OCR text, amounts); they carry ids, rule ids, field names and confidences. A test SHALL prove it for the extraction step.
- **R-9.17 (M)** CI security: gitleaks, pip-audit and bandit run on every push and PR (PT-8); no `pull_request_target`; no self-hosted runners; fork PR runs need approval; YAML is loaded only with `yaml.safe_load`. While the repo is public (D27), CI SHALL reject committed images that carry EXIF data and any valid RUC that isn't on the allowlist of synthetic RUCs. The one exception is a store directory file's own RUC (R-5.8, D47).

## R-10 · Commands (chat)
| Command | Purpose | Priority |
|---|---|---|
| `/ayuda` | List the commands | M |
| `/estado <id>` | Show an invoice's status | S |
| `/pendientes` | List invoices waiting for user input or in MANUAL_REQUIRED | S |
| `/lote` | Show the open flow B batch (count, total) *(post-MVP, with `on_command`)* | C |
| `/enviar` | Send the open batch now *(post-MVP, with `on_command`)* | C |
| `/pagado dd/mm monto` | Record a flow B reimbursement and mark its folders PAID (R-7.12); `#id` list form also accepted | M |
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
- **R-11.6 (M)** CI SHALL run `scripts/check_config.py`, which **fails** on any config that fails validation. A portal entry with `status: todo|broken` SHALL **fail CI only on a pull request that adds or changes its `portals/<key>.yaml`**; everywhere else (other pull requests, `main`) it is reported in the job summary but doesn't fail it, so deploys and unrelated work never freeze (D83). With D44, `status: broken` is set by the owner, not by a bot PR.
- **R-11.7 (C, post-MVP)** A scheduled workflow (`gap-watch`, daily) SHALL fail while any `gap` issue has been open for more than `gap_sla_days` (default 3). Deferred by D44.
- **R-11.8 (M)** WHEN a gap is new, THE SYSTEM SHALL send **one** chat message to each `admins` profile, on its preferred channel, with the kind, the subject and the issue link. Kinds marked "digest" go into a daily summary instead. Alerts for the same fingerprint SHALL be rate-limited to one per `realert_days` (default 7).
- **R-11.9 (M)** WHEN a deploy changes config (the store directory, `parsing_rules.yaml`, routing, templates), THE SYSTEM SHALL reload it on `SIGHUP` (R-9.6), re-evaluate open gaps, and resume the affected invoices: for each gap that is now fixed, it SHALL re-enqueue the linked invoices (`MANUAL_REQUIRED`/`PENDING_AUTH` → `FETCHING_DOCS`/`DELIVERING`) and tell their senders. Config reaches the host only through the CI-gated deploy (D46); there is no pull timer.
- **R-11.10 (M)** GitHub access SHALL use a **GitHub App** (D45) installed only on the ops repo, with short-lived installation tokens and the **Issues** permission only (no Contents, Pull requests or Workflows). The App's private key is a secret (R-9.9). The daily budget for GitHub writes is ≤ 20 operations. Failures to reach GitHub SHALL NOT affect invoice processing; they are retried from the outbox. Key or installation problems alert the owner (BR-D05). At the end of development, once the main repo is private, the App is also installed there with Contents and Pull requests, only to open the gap PR (S16.2, D52).
- **R-11.11 (S)** IF GitHub is disabled or unreachable for more than 24 h, THE SYSTEM SHALL still alert the admin in chat, so the alert path never depends only on GitHub.
- **R-11.12 (M)** Gap issue text SHALL be built **only from typed fields**: the 11-digit issuer RUC, the business name from the padrón only, CPE type, fetcher reason codes, and a URL reduced to `https://host/path` whose characters match `[A-Za-z0-9._~/-]`, placed in a code fence. `@` is stripped everywhere. No raw OCR text and no free text from senders. Any YAML the bot writes in the future SHALL use a serializer and a parse-back equality check.

## R-12 · Integration contract (D39, constitution P10) · v1.1
Consumers (Jarvis, n8n, future apps) integrate only through this surface. The bot works fully without any consumer. **All of R-12 ships in v1.1 (M8)**, except R-12.7, which holds from the MVP on.
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
