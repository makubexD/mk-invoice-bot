# 04 · Backlog: epics and user stories

**Format:** each story is small or medium, independently testable, and traced to requirement IDs (spec 02) and business rules (spec 06).

| Size | Meaning |
|---|---|
| **S** (small) | ≤ 1 day, one module, a few tests |
| **M** (medium) | 2–3 days, touches 2–3 modules or an external API |

Anything bigger gets split before work starts.

**Story template:**
- *As …, I want …, so that …*
- **AC** (acceptance criteria): each one is a test.
- **Reqs** and **Deps**.

**Definition of done (every story):**
- Every AC has a test.
- `ruff`, `mypy` and `pytest` are green, and so is `check_config.py`.
- Requirement IDs and rule IDs are cited in the PR.
- No secrets in code or logs.
- Memory impact is noted if the story touches extraction or the browser.

## Milestones
| Milestone | Epics | Demo |
|---|---|---|
| **M0 · De-risk** | E0 | Spike reports answer the risky unknowns |
| **M1 · Read & route** | E1–E5 | Send a photo; the bot replies with the parsed summary and the **auto-detected** flow. Golden-set accuracy report |
| **M2 · Flow A + gaps** | E6–E9 | Photo → row in Sheets plus files in Drive. An unknown store → chat explanation, a GitHub issue, a draft PR, red CI and an admin alert |
| **M3 · Portals** | E10 | A seeded portal downloads XML/PDF automatically (captcha included). A merged gap PR auto-resumes stuck invoices |
| **M4 · Flow B** | E11 | 3 work invoices → `/enviar` → one email with PDFs, XMLs and the filled Excel |
| **M5 · Ops** | E12 | Memory budget enforced in CI, backups, runbook |
| **M6 · Contract & channels** | E13, E14 | Telegram works like WhatsApp; Jarvis or n8n can read invoices over MCP/REST and receives signed events |
| *(optional)* | E3b | Vision-LLM fallback, off by default |

**MVP = M1 to M5 (flows A and B, D32).** M6 is v1.1. Each milestone is a checkpoint you can test with real invoices.

---

## E0 · Spikes and setup (owner + dev)
| ID | Story | Size | Done when | Reqs |
|---|---|---|---|---|
| S0.1 | Collect **≥ 30 real invoice photos** with an expected JSON for each (anonymized buyer) → `tests/golden/` | S | Dataset committed | R-2.9 |
| S0.2 | Spike: QR cascade hit rate per decoder on the golden set | S | `specs/spikes/qr.md` | R-2.1 |
| S0.3 | Spike: Tesseract field accuracy on the no-QR cases | S | Report | R-2.3 |
| S0.4 | Manual check: can SOL "Consulta de comprobantes" download the XML of **received** invoices? | S | Yes/no plus screenshots | R-5.2 |
| S0.5 | Manual check: does the SUNAT validez API answer for a *work* invoice (a different buyer RUC)? | S | Works or not | R-4.1 |
| S0.6 | List the top 10–20 issuers (RUC, link, fields, captcha type) → seed `portals.yaml` | S | File committed | R-5.8 |
| S0.7 | Meta app, a **dedicated** number (a Cloud API number can't stay on the normal WhatsApp app) and a payment method; webhook through a local Cloudflare quick tunnel (D31) | S | An echo bot running in local Docker receives an image | R-1.x |
| S0.8 | Entra app; device-code login with the personal **and** work accounts | S | Learn whether the work tenant allows consent (Q2) | R-7.1 |
| S0.9 | Memory spike: Chromium headless-shell plus one page, with Postgres running, under a 2 GB Docker memory limit | S | Peak RSS measured | R-9.1 |
| S0.10 | Create the fine-grained GitHub token (this repo only: Issues, PRs, Contents) and the `gap` labels | S | The token can open an issue on the repo | R-11.10 |

## E1 · Foundation
**S1.1 · Project scaffold** · S · Reqs R-9.5
- *As the developer, I want a scaffolded repo with uv, ruff, mypy, pytest, pre-commit and a Dockerfile, so that every later story starts from green.*
- **AC:**
  - `GET /health` returns 200.
  - The Docker image builds, and `docker compose up` starts `app` + `postgres` locally.
  - Idle RSS of `app` is < 80 MB.
  - Pre-commit runs ruff and mypy.

**S1.2 · Typed config + `check_config.py`** · M · Reqs R-9.6, R-11.6 · Deps S1.1
- *As the owner, I want my YAML validated at startup and in CI, so that a typo never reaches production.*
- **AC:**
  - Pydantic models for every config section.
  - Unknown keys are rejected.
  - `{owner.ruc}` placeholders are resolved.
  - `check_config.py` exits ≠ 0 on an invalid config **or** on any portal with `status: todo|broken`, and lists them.

**S1.3 · Postgres store + migrations** · S · Reqs R-9.4 · Deps S1.1
- *As the system, I want a Postgres schema with versioned Alembic migrations, so that state survives restarts.*
- **AC:**
  - All the spec 03 §3 tables exist.
  - `alembic upgrade head` runs on an empty DB and is a no-op on a migrated one; `downgrade -1` works for the latest revision.
  - Tests run against a real Postgres container, not SQLite.

**S1.4 · Job queue with lease and backoff** · M · Reqs R-9.4 · Deps S1.3
- *As the system, I want durable jobs with leases and exponential backoff, so that a crash never loses work.*
- **AC:**
  - Jobs are claimed with `FOR UPDATE SKIP LOCKED`; two workers never take the same job.
  - A killed worker's job is re-leased after `lease_until`.
  - The worker wakes on `LISTEN/NOTIFY` and doesn't busy-poll.
  - After 5 failures the job goes to `FAILED`.
  - `HEAVY` semaphore = 1.

**S1.5 · Outbox** · S · Reqs R-1.8, R-11.5 · Deps S1.4
- *As the system, I want outgoing WhatsApp and GitHub calls persisted before sending, so that they are retry-safe.*
- **AC:**
  - The outbox row is written in the same transaction as the state change.
  - Its sender retries with backoff.
  - It supports the `whatsapp`, `telegram`, `github` and `event` channels.

**S1.6 · CI workflow** · S · Reqs R-11.6 · Deps S1.2
- *As the owner, I want CI on every push and PR, so that a red build signals that something needs fixing.*
- **AC:** `ci.yml` runs lint, mypy, tests and `check_config.py`, and a deliberately added `status: todo` makes it fail.

## E2 · WhatsApp channel
**S2.1 · Webhook verify + signature** · S · Reqs R-1.3
- **AC:**
  - The GET handshake works.
  - A bad `X-Hub-Signature-256` gets 401.
  - A valid one gets 200 in < 1 s.

**S2.2 · Idempotent intake, allowlist, ack** · M · Reqs R-1.1, R-1.2, R-1.8 · Deps S2.1, S1.5
- *As a sender, I want an instant "recibido" when I send a photo, so that I know it's being processed.*
- **AC:**
  - A duplicate `message.id` is ignored.
  - A non-allowlisted sender is ignored silently.
  - The ack is enqueued within 5 s.
  - The job is created.

**S2.3 · Media download** · S · Reqs R-1.4
- **AC:**
  - The download streams to `/data/tmp/{id}/` within 60 s.
  - The SHA-256 is stored.
  - Oversized files are rejected.

**S2.4 · Send text, buttons and lists** · S · Reqs R-3.2
- **AC:**
  - The payload builders for text, reply buttons (≤ 3) and lists are unit-tested against the Meta schema.
  - Button IDs encode `invoice_id:action`.

**S2.5 · Interactive reply handling** · S · Reqs R-3.2, R-3.5 · Deps S2.4
- **AC:**
  - Button and list replies map to `(invoice_id, action)`.
  - A stale button (invoice already past `DELIVERING`) gets a polite "ya está en proceso".

**S2.6 · Fake channel for tests** · S · Deps S2.4
- *As the developer, I want an in-memory channel, so that end-to-end conversation tests run without Meta.*
- **AC:** Conversation scripts can assert the exact messages sent, by rule ID.

## E3 · Extraction (no AI)
| ID | Story | Size | Key AC | Reqs |
|---|---|---|---|---|
| S3.1 | Validators: RUC mod-11, serie regex, date bounds, total ≈ base + IGV | S | Table-driven tests, including edge cases (check digits 10 → 0 and 11 → 1) | R-2.4 |
| S3.2 | SUNAT QR payload parser (pipe format, both date formats, URL payloads) | S | 20+ fixture payloads; a URL becomes `document_url` | R-2.2, R-2.8 |
| S3.3 | QR cascade decoder (zxing → crops/upscale → WeChat → pyzbar, rotations) | M | Hit rate on the golden set ≥ S0.2 baseline; peak RSS logged | R-2.1 |
| S3.4 | Padrón import (streaming) + lookup | M | Import < 50 MB peak RAM; lookup < 1 ms; weekly refresh job | R-2.5 |
| S3.5 | OCR preprocessing + Tesseract subprocess with timeout | M | Warp/deskew/threshold pipeline; 30 s timeout → BR-E01 | R-2.3 |
| S3.6 | OCR rule parser (`parsing_rules.yaml`) + printed-URL capture | M | Golden OCR field accuracy ≥ 80%; `printed_urls` captured | R-2.3, R-2.10 |
| S3.7 | Per-field source/confidence + `ok`/`needs_review` | S | Uncertain fields are flagged in the model | R-2.6 |
| S3.8 | Golden-set accuracy report (CI artifact) | S | Per-field accuracy table; CI fails below the R-2.9 thresholds | R-2.9 |

## E4 · Routing and conversation (no flow menu)
**S4.1 · Routing rules engine** · S · Reqs R-3.1 · Rules BR-R01/02
- *As the owner, I want the flow chosen from the buyer RUC, so that I never have to pick it by hand.*
- **AC:**
  - Ordered rules; the first match wins.
  - Rules are compiled to frozensets at load time.
  - Owner RUC → `personal_google`; work RUC → `work_microsoft`.
  - A new rule in YAML routes to a new flow with no code change (test with a fake flow C).

**S4.2 · Summary + auto-continue** · M · Reqs R-3.4, R-9.3 · Deps S4.1, S2.4
- *As a sender, I want one summary showing what was read and where it's going, and processing that just continues, so that the UX is smooth.*
- **AC:**
  - With `confirm_mode: auto` and all fields `ok`, the pipeline moves to `VALIDATING` without waiting.
  - The summary includes `🏢 Mi empresa → Google` or `💼 Trabajo → Microsoft` and [Editar] [Cancelar].
  - The happy path uses ≤ 3 messages.
  - `confirm_mode: always` waits for a tap.

**S4.3 · Unknown buyer → ask (only case with a menu)** · S · Rules BR-R03 · Reqs R-3.2
- **AC:**
  - A DNI, a missing buyer or an unmatched RUC gets one message with buttons for the flows allowed for the sender, plus Cancelar.
  - The choice resumes the pipeline.

**S4.4 · Low-confidence buyer → confirm** · S · Rules BR-R04 · Reqs R-3.3
- **AC:**
  - An OCR buyer below the threshold gets the message with the detected flow pre-selected: [Sí] [Cambiar] [Cancelar].

**S4.5 · Edit with `field=value`** · M · Reqs R-3.5
- **AC:**
  - Multi-line edits are parsed and re-validated.
  - Changing `num_doc_adq` re-routes.
  - Invalid values get the specific BR-E message.

**S4.6 · Duplicates** · S · Rules BR-E04 · Reqs R-3.6
- **AC:**
  - A second photo of the same CPE is stopped, with a reference to the first one.
  - `/reprocesar <id>` bypasses the check.

**S4.7 · Profile permissions after routing** · S · Rules BR-R05 · Reqs R-3.8
- **AC:** A limited sender sending a work invoice is rejected with the BR-R05 message.

**S4.8 · Timeouts → PENDING_USER** · S · Reqs R-3.7
- **AC:**
  - An unanswered question parks the invoice after 24 h.
  - `/pendientes` lists it and lets you resume it.

**S4.9 · Basic commands** · M · Reqs R-10
- **AC:**
  - `/ayuda`, `/estado <id>` (shows state **and the rule ID and message that explain it**), `/pendientes`, `/cancelar <id>` and `/reprocesar <id>` all work.

## E5 · Business-rule catalog
**S5.1 · Catalog + parity test** · S · Reqs R-11.1
- *As the owner, I want every message to come from one catalog keyed by rule ID, so that wording is consistent and nothing is silent.*
- **AC:**
  - `rules/catalog.py` maps BR-* → (state, message template).
  - A test parses `specs/06-business-rules.md` and fails on any ID that is missing on either side.

**S5.2 · Rule ID on every transition** · S · Reqs R-11.1 · Deps S5.1
- **AC:**
  - `invoices.rule_id` is set on every non-happy transition.
  - Rendering a message with a missing placeholder fails the test suite.

## E6 · SUNAT validation
| ID | Story | Size | Key AC | Reqs / rules |
|---|---|---|---|---|
| S6.1 | OAuth client_credentials + token cache in `kv` | S | Refreshes 60 s before expiry; works across restarts | R-4.3 |
| S6.2 | `validarcomprobante` + BR-V01..V04 handling | M | Each estadoCp, RUC estado and outage case is mapped to its rule and message; the V01 re-check is scheduled | R-4.1, R-4.2 |

## E7 · Fetcher framework
| ID | Story | Size | Key AC | Reqs / rules |
|---|---|---|---|---|
| S7.1 | Chain of responsibility + `FetchResult` + `fetch_attempts` stats | M | Per-flow order from config; stops at the first valid XML; every attempt is recorded with its reason | R-5.1, R-5.7 |
| S7.2 | `qr_url` fetcher | S | Follows the QR or printed URL; httpx streaming; size limits | R-5.2 |
| S7.3 | UBL 2.1 parser + XML-matches-invoice check | M | Wrong XML → BR-F06; the XML values become authoritative, and discrepancies are reported | R-5.6, R-2.7 |
| S7.4 | `manual` terminal step (BR-F07 message, built from the attempt reasons) | S | One `•` line per fetcher, using the BR-F texts (F01 for flow B) | R-5.5 |
| S7.5 | Accept user-sent PDF/XML for `MANUAL_REQUIRED` | S | A quoted or most-recent open invoice is matched; the XML is validated; delivery resumes | R-1.5 |

## E8 · Gaps, GitHub and admin alerts
**S8.1 · Gap registry + dedupe** · S · Reqs R-11.2
- *As the owner, I want 10 invoices from the same unknown store to create one gap, not ten, so that alerts stay useful.*
- **AC:**
  - `raise_gap()` upserts by fingerprint in the same transaction.
  - `invoice_gaps` links the invoices.
  - The fetcher chain keeps running.

**S8.2 · Teach the portal link via chat** · M · Rules BR-F02 · Reqs R-11.4 · Deps S8.1
- *As a sender, I want the bot to ask me for the store's download link when it doesn't know it, so that the fix is mostly done by the time someone looks at it.*
- **AC:**
  - The question is asked once per issuer, not per invoice.
  - The answer is validated (https, public host, ≤ 300 characters, no credentials).
  - "no sé" is accepted.
  - `/portal <ruc> <url>` does the same thing.
  - `printed_urls` are offered as a suggestion.

**S8.3 · GitHub issue (idempotent)** · M · Reqs R-11.5, R-11.10 · Deps S1.5, S8.1
- **AC:**
  - Created through the outbox from `.github/ISSUE_TEMPLATE/gap.md`, with labels `gap` and `gap:<kind>`.
  - A hidden `<!-- gap:fp -->` marker prevents duplicates even after a DB loss.
  - Only privacy-safe fields are included (snapshot test).
  - Respects the daily budget.

**S8.4 · Draft PR with portal stub** · M · Reqs R-11.5 · Deps S8.3
- *As the owner, I want a ready-to-finish PR, so that fixing a missing portal means filling in selectors, not starting from zero.*
- **AC:**
  - Deterministic branch `gap/<slug>`.
  - YAML stub appended with comments preserved and `status: todo`, plus the taught or printed URL.
  - Draft PR with "Closes #n" and a checklist.
  - Never touches the default branch.
  - Re-running doesn't duplicate anything.

**S8.5 · Admin WhatsApp alert + digest** · M · Reqs R-11.8, R-11.11
- **AC:**
  - One alert per new fingerprint, with the issue and PR links.
  - Re-alerts are rate-limited to `realert_days`.
  - Digest kinds go into one daily message.
  - The `gap_alert` utility template is used outside the 24 h window.
  - The alert is still sent if GitHub is down.

**S8.6 · `gap-watch` scheduled workflow** · S · Reqs R-11.7
- **AC:** The workflow fails while a `gap` issue is older than `gap_sla_days`, and passes otherwise.

**S8.7 · Config pull + hot reload + auto-resume** · M · Reqs R-11.9 · Deps S1.2, S8.1
- *As a sender, I want my stuck invoice to finish by itself once the portal is added, so that I don't resend anything.*
- **AC:**
  - A systemd timer pulls with `--ff-only`.
  - `SIGHUP` reloads the config, which is validated **before** it is swapped in (a bad config keeps the old one and alerts the admin).
  - `reevaluate()` re-enqueues the invoices linked to fixed gaps and notifies their senders.

**S8.8 · PORTAL_BROKEN detection** · S · Rules BR-F03 · Deps S7.1, S8.4
- **AC:**
  - `broken_after` consecutive *technical* failures (not "not found") raise the gap.
  - The PR flips the entry to `status: broken`.

**S8.9 · Admin commands + token expiry** · S · Rules BR-D05 · Reqs R-10
- **AC:**
  - `/gaps` lists open gaps with their links.
  - `/gap ignorar <id>` silences one.
  - A daily check warns 14 days before `GITHUB_TOKEN_EXPIRES`.

## E9 · Flow A (Google)
| ID | Story | Size | Key AC | Reqs |
|---|---|---|---|---|
| S9.1 | Google OAuth bootstrap script + refresh | S | A one-time consent stores the refresh token (mode 600); BR-D01 on revocation → `PENDING_AUTH` | R-6.4 |
| S9.2 | Drive sink: folders + upload + links | M | Path and filename templates; returns refs for the later sinks | R-6.1 |
| S9.3 | Sheets sink: column mapping + upsert by natural key | M | A re-run updates the row and never duplicates it | R-6.2, R-6.3 |
| S9.4 | Completion message | S | ✅/⚠️/❌ + key fields + links in one message | R-8.1, R-8.2 |
| S9.5 | `email_inbox` (Gmail, read-only) with a retry schedule | M | Finds the XML/PDF by serie-número; 5m/1h/6h/24h retries; the invoice shows "esperando correo" | R-5.2 |

## E10 · Browser fetchers
| ID | Story | Size | Key AC | Reqs |
|---|---|---|---|---|
| S10.1 | BrowserPool (lazy, idle close, resource blocking) | M | Chromium is never resident while idle; peak RSS < budget (S0.9) | R-9.1 |
| S10.2 | `generic_form` adapter driven by `portals.yaml` | M | Fills, submits and downloads using only YAML; ≥ 3 seeded portals pass `portal-test` | R-5.8 |
| S10.3 | Captcha: local OCR attempts | S | ≤ 2 attempts, then hand over to S10.4 | R-5.3 |
| S10.4 | Captcha: ask the user over WhatsApp | M | Sends the image, waits up to 5 min, BR-F05 on timeout; reCAPTCHA → BR-F04 | R-5.3, R-5.4 |
| S10.5 | `invoicebot portal-test <key>` CLI | S | Runs one portal end to end against a sample; used in the gap PR checklist | R-5.7 |
| S10.6 | `sunat_sol` fetcher (flow A only; depends on S0.4) | M | Secondary SOL user; selectors in a file; skipped in flow B with BR-F01 | R-5.2 |
| S10.7 | Custom adapters for the 2–3 portals the generic adapter can't handle | M each | Each passes `portal-test` | R-5.8 |

## E11 · Flow B (Microsoft)
| ID | Story | Size | Key AC | Reqs |
|---|---|---|---|---|
| S11.1 | MSAL device-code bootstrap + token cache | S | Personal account works; the work account result is documented (Q2) | R-7.1 |
| S11.2 | OneDrive sink (upload, folders, links) | M | Path templates; upload sessions for > 4 MB | R-7.1 |
| S11.3 | Excel template sink (openpyxl, mapped cells, If-Match) | M | Formatting kept; a missing template or bad mapping → BR-D02 + `TEMPLATE_INVALID` gap | R-7.2 |
| S11.4 | Batch model + `/lote` | S | Open batch per flow; count and total | R-7.3 |
| S11.5 | `per_invoice` trigger | S | One email per invoice | R-7.3 |
| S11.6 | `on_command` trigger + `/enviar` + preview/confirm | M | Preview with recipients, count and total; [Enviar] [Cancelar]; BR-D04 on an empty batch | R-7.3, R-7.6 |
| S11.7 | `scheduled` trigger (cron) | S | Sends the open batch on schedule; combinable with on_command | R-7.3 |
| S11.8 | Mailer (Graph and Gmail) + templates + large attachments | M | Zip or upload when over 3 MB; BR-D03 on failure | R-7.4, R-7.5, R-7.7 |
| S11.9 | Outlook variant of `email_inbox` | S | Same contract as S9.5 | R-5.2 |

## E12 · Hardening and operations
| ID | Story | Size | Key AC | Reqs |
|---|---|---|---|---|
| S12.1 | `memprofile.py` + latency report in CI | M | CI fails if peak > 800 MB or latency targets are missed | R-9.1, R-9.2 |
| S12.2 | Deploy: compose (app + postgres + cloudflared) + swapfile; **CI-gated** deploy with migrations, health check and rollback (D37) | M | A fresh VPS is running in < 30 min following the runbook; a failing health check rolls back | R-11.9 |
| S12.3 | Nightly encrypted `pg_dump` + config backup off-site (Drive, OneDrive or object storage) | S | Restore into an empty Postgres tested | — |
| S12.4 | Housekeeping (temp purge, stale jobs, padrón refresh) | S | Disk stays bounded | R-9.7 |
| S12.5 | Runbook (re-auth tokens, add a portal, rotate secrets, handle a gap PR) | S | Reviewed by the owner | — |
| S12.6 | Optional: paid CPE API and captcha-solver adapters | M each | Off by default | R-5.2 |
| S12.7 | **Last step (D27):** on the owner's word, make the GitHub repo private and confirm the deploy still pulls | S | Repo private; deploy green | — |

## E3b · LLM fallback extraction (optional, off by default)
| ID | Story | Size | Key AC | Reqs |
|---|---|---|---|---|
| S3b.1 | `LLMExtractor` port + OpenAI-compatible adapter (DeepSeek default); spike: does the provider accept images? | M | Off by default; runs only after QR, XML and OCR fail; fields tagged `source=llm`, confidence ≤ 0.5; always forces confirmation | R-2.11 |
| S3b.2 | Usage log and monthly cap | S | Each call records tokens and cost; above the cap the adapter is skipped with a clear message | R-2.11 |

## E13 · Integration contract (v1.1)
| ID | Story | Size | Key AC | Reqs |
|---|---|---|---|---|
| S13.1 | API tokens: create, list and revoke (CLI), hashed storage, `read`/`write` scopes | S | 401 without a token; a read token can't call write endpoints | R-12.3 |
| S13.2 | REST `/api/v1`: invoices list/get, spend summary | M | OpenAPI published; filters by date, flow, issuer, state | R-12.1 |
| S13.3 | MCP server: read tools | M | An MCP client (e.g. Claude Code or Jarvis) lists and queries invoices with a read token | R-12.2 |
| S13.4 | MCP and REST write tools through the state machine | M | `reprocess`, `cancel` and `attach_document` behave exactly like the chat commands, with the same BR ids; the token name is recorded | R-12.2 |
| S13.5 | Event publisher: subscribers, HMAC signing, outbox retries | M | A test subscriber receives `invoice.delivered` with a valid signature; a down subscriber doesn't block the pipeline | R-12.4, R-12.5 |
| S13.6 | Contract tests: OpenAPI snapshot, MCP tool schemas, event schema | S | CI fails on an unversioned breaking change | R-12.6 |

## E14 · Telegram channel (v1.1)
| ID | Story | Size | Key AC | Reqs |
|---|---|---|---|---|
| S14.1 | Telegram adapter on the `Channel` port: webhook with secret token, photos and documents, inline keyboards | M | The same conversation tests that pass on the fake channel pass on Telegram fixtures | R-1.7 |
| S14.2 | Sender profiles link one WhatsApp number and one Telegram id | S | An invoice started on one channel can be queried with `/estado` on the other | R-1.7 |
