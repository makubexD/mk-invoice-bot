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
| **M1 · Read & route** | E1–E5 | Send a photo; the bot replies with the parsed summary and the **auto-detected** flow. Golden-set accuracy report. The CI deploy pipeline (S12.2) is proven end to end against a local Docker target, since the VPS is chosen later (D31) |
| **M2 · Flow A + gaps + ops baseline** | E6–E9, E15 | Photo → row in Sheets plus files in Drive. An unknown store → chat explanation, an issue in the private ops repo and an owner alert. Nightly encrypted backup and a restore drill; self-alerts |
| **M3 · Portals** | E10 | A seeded portal downloads XML/PDF automatically (captcha included). A portal fix merged by the owner deploys through CI and auto-resumes stuck invoices |
| **M4 · Flow B** | E11 | 3 work invoices → `/enviar` → one email with PDFs, XMLs and the filled Excel (`on_command` only) |
| **M5 · Ops** | E12 | Memory budget enforced in CI, runbook, repo private |
| **M6 · Contract & channels (v1.1)** | E13, E14 | Telegram works like WhatsApp; Jarvis or n8n can read invoices over MCP/REST and receives signed events |
| *Post-MVP* | E16, S11.5, S11.7, S11.10 | Gap automation (draft PRs, `gap-watch`), `per_invoice` and `scheduled` triggers, Gmail mailer |
| *(optional)* | E3b | Vision-LLM fallback, per-flow opt-in, never flow B (D43) |

**MVP = v1 = M1 to M5 (flows A and B, D32).** M6 is v1.1. Each milestone is a checkpoint you can test with real invoices. Stories are listed by epic; where a story was moved, its ID stays the same and a pointer is left behind.

---

## E0 · Spikes and setup (owner + dev)
| ID | Story | Size | Done when | Reqs |
|---|---|---|---|---|
| S0.1 | Collect **≥ 30 invoice photos** with an expected JSON for each → `tests/golden/`. While the repo is public, only synthetic or anonymised images (no EXIF, synthetic RUCs) are committed; real photos stay outside git (R-9.17) | S | Dataset committed; CI's EXIF/RUC check passes | R-2.9, R-9.17 |
| S0.2 | Spike: QR cascade hit rate per decoder on the golden set | S | `specs/spikes/qr.md` | R-2.1 |
| S0.3 | Spike: Tesseract field accuracy on the no-QR cases | S | Report | R-2.3 |
| S0.4 | Manual check: can SOL "Consulta de comprobantes" download the XML of **received** invoices? **Still unverified (2026-11-10); blocks `sunat_sol` (S10.6)** | S | Yes/no plus screenshots | R-5.2 |
| S0.5 | Smoke test: the SUNAT validez API answers for a *work* invoice (a different buyer RUC). Research says the caller needn't be the buyer (verified 2026-11-10) | S | One work invoice validated with the owner's credentials | R-4.1 |
| S0.6 | List the top 10–20 issuers (RUC, link, fields, captcha type) → seed `portals.yaml` | S | File committed | R-5.8 |
| S0.7 | Meta app, a **dedicated** number (a Cloud API number can't stay on the normal WhatsApp app) and a payment method (templates are charged; free-tier service messages are not); webhook through a local Cloudflare quick tunnel (D31). Submit the **utility templates** for Meta approval: `invoice_done`, `manual_required`, `resume_notice`, `pending_user_reminder`, `batch_preview`, `admin_alert` | S | An echo bot running in local Docker receives an image; all six templates approved | R-1.1, R-1.10 |
| S0.8 | Entra app; device-code login with the personal **and** work accounts; if the work tenant blocks device code, try auth-code + PKCE on a loopback redirect | S | Learn whether the work tenant allows consent and which login works (Q2) | R-7.1, R-7.9 |
| S0.9 | Memory spike: Chromium headless-shell plus one page, with Postgres running, under a 2 GB Docker memory limit | S | Peak RSS measured | R-9.1 |
| S0.10 | Create the private ops repo (`owner/mk-invoice-bot-ops` placeholder), a **GitHub App** with the Issues permission only, install it on the ops repo only, and create the `gap` labels there (D45) | S | An installation token obtained from the App key opens and closes an issue in the ops repo, and is refused on the main repo | R-11.10 |

## E1 · Foundation
**S1.1 · Project scaffold** · S · Reqs R-9.14
- *As the developer, I want a scaffolded repo with uv, ruff, mypy, pytest, pre-commit and a Dockerfile, so that every later story starts from green.*
- **AC:**
  - `GET /health` returns 200 when the DB answers and 503 when it doesn't.
  - The Docker image builds, and `docker compose up` starts `app` + `postgres` locally.
  - Idle RSS of `app` is < 80 MB.
  - Pre-commit runs ruff and mypy.

**S1.2 · Typed config + `check_config.py`** · M · Reqs R-9.6, R-11.6 · Deps S1.1
- *As the owner, I want my YAML validated at startup and in CI, so that a typo never reaches production.*
- **AC:**
  - Pydantic models for every config section.
  - Unknown keys are rejected.
  - `{owner.ruc}` placeholders are resolved.
  - `check_config.py` exits ≠ 0 on an invalid config, and lists the problems.
  - With `--pr`, it also exits ≠ 0 on any portal with `status: todo|broken`; without it (on `main`), it only lists them.
  - A `python:` adapter outside `invoicebot.fetchers.portals.*` is rejected (R-5.8).
  - `llm_fallback` enabled for `work_microsoft` is rejected while the provider is `deepseek` (D43).
  - YAML is loaded only with `yaml.safe_load`.

**S1.3 · Postgres store + migrations** · S · Reqs R-9.4 · Deps S1.1
- *As the system, I want a Postgres schema with versioned Alembic migrations, so that state survives restarts.*
- **AC:**
  - All the spec 03 §3 tables exist.
  - `alembic upgrade head` runs on an empty DB and is a no-op on a migrated one; `downgrade -1` works for the latest revision.
  - Tests run against a real Postgres container, not SQLite.

**S1.4 · Job queue with lease and backoff** · M · Reqs R-9.4, R-1.11 · Rules BR-S03 · Deps S1.3
- *As the system, I want durable jobs with leases and exponential backoff, so that a crash never loses work.*
- **AC:**
  - Jobs are claimed with `FOR UPDATE SKIP LOCKED`; two workers never take the same job.
  - A killed worker's job is re-leased after `lease_until`.
  - The worker wakes on `LISTEN/NOTIFY` and doesn't busy-poll.
  - After 5 failures the job goes to `FAILED` (BR-S03).
  - A job whose worker died twice (lease expired twice without completion) goes to `FAILED` (crash-loop guard, R-1.11, BR-S03).
  - `HEAVY` semaphore = 1.

**S1.5 · Outbox** · S · Reqs R-1.8, R-11.5 · Deps S1.4
- *As the system, I want outgoing chat messages and GitHub calls persisted before sending, so that they are retry-safe.*
- **AC:**
  - The outbox row is written in the same transaction as the state change.
  - Its sender retries with backoff.
  - It supports the `whatsapp`, `telegram`, `github` and `event` channels.

**S1.6 · CI workflow** · S · Reqs R-11.6, R-9.17 · Deps S1.2
- *As the owner, I want CI on every push and PR, so that a red build signals that something needs fixing.*
- **AC:**
  - `ci.yml` runs lint, mypy, tests and `check_config.py`.
  - A deliberately added `status: todo` fails a pull request, and is only reported on `main`.
  - gitleaks, pip-audit and bandit run on every push and PR (PT-8); a planted fake secret fails the build.
  - No workflow uses `pull_request_target` or a self-hosted runner (a lint step checks); fork PR runs need approval (repo setting, documented).
  - A committed image with EXIF data, or a valid RUC outside the synthetic allowlist, fails CI.

**S1.7 · Channel port + canonical inbound/outbound message (PT-1)** · M · Reqs R-1.9, R-1.8, R-1.5 · Deps S1.3
- *As the developer, I want every channel behind one port with one message model, so that WhatsApp and Telegram share the pipeline and an invoice's replies go back where it came from.*
- **AC:**
  - Pydantic `InboundMessage` / `OutboundMessage` models carry channel, external id, profile id, attachments by reference, and sender role.
  - `messages` has PK `(channel, external_id)`; the same id on two channels is two messages.
  - Profiles from config link one WhatsApp number and one Telegram user id; both resolve to the same `profile_id`.
  - Invoices store `profile_id` and `origin_channel`; replies go to the origin channel; admin alerts go to the owner profile's preferred channel.
  - Attachment matching for R-1.5 looks across all of the profile's channels (test: invoice from WhatsApp, PDF from the fake Telegram channel).

**S1.8 · Encrypted secrets + per-adapter scoping** · M · Reqs R-9.9, R-9.10 · Deps S1.2
- *As the owner, I want secrets encrypted at rest and handed out only to the adapter that needs them, so that a leak or bug in one adapter exposes as little as possible.*
- **AC:**
  - Secrets load from an `age`-encrypted file (or `systemd-creds`) into memory at start; nothing is written back in plain text.
  - `.env` with a secret-looking key (`*_TOKEN`, `*_SECRET`, `*_PASSWORD`, `*_KEY`) fails startup.
  - An adapter that asks for a secret it didn't declare gets an error; only `sunat_sol` can read SOL credentials.
  - A missing secret disables its adapter or endpoint (fails closed).
  - `pg_dump` as configured for backups excludes the `kv` table.

**S1.9 · Observability: JSON logs, step timings, redaction** · S · Reqs R-9.5, R-9.16 · Deps S1.1
- *As the owner, I want structured logs with timings and no secrets, so that I can see where an invoice spent its time without leaking anything.*
- **AC:**
  - Every log line is one JSON object with `ts`, `level`, `event`, `invoice_id`, `job_id`, `step`, `rule_id`, `duration_ms`.
  - Each pipeline step records its duration; `/estado` can show the slowest step.
  - A test configures fake secrets (including a Telegram bot token inside a request URL), runs a conversation, and asserts none appears in captured logs.

**S12.2 · CI-gated deploy (moved from E12; D37, D46)** · M · Reqs R-11.9, R-9.13 · Deps S1.6, S1.3
- *As the owner, I want a green `main` to deploy code and config by itself, with a way back, so that fixes go live without manual steps and auto-resume can rely on it.*
- **AC:**
  - `deploy.yml` runs only after a green CI on `main`: build image, `pg_dump`, copy config, migrate, start, `/health`.
  - A failing health check rolls back to the previous image (tested with a deliberately broken image).
  - A config-only change is copied and applied with `SIGHUP`; an invalid config is refused and the old one stays.
  - Migrations follow expand/contract; a CI check runs the previous image against the migrated schema.
  - No pull timer exists on the host.
  - Compose (`app` + `postgres` + `cloudflared`) and a swapfile; a fresh 2 GB VPS (D42) is running in < 30 min following the runbook.

**S5.1 · Catalog + parity test (moved from E5)** · S · Reqs R-11.1
- *As the owner, I want every message to come from one catalog keyed by rule ID, so that wording is consistent and nothing is silent.*
- Moved here because E2 and E4 already emit messages by rule ID.
- **AC:**
  - `rules/catalog.py` maps BR-* → (state, message key); `rules/messages_es.py` holds the es-PE texts.
  - A test parses `specs/06-business-rules.md` and fails on any ID that is missing on either side.

**S1.10 · Zero-consumer boundary** · S · Reqs R-12.7 · Deps S1.1
- *As the owner, I want the bot to work with no Jarvis, n8n or other consumer, so that integrations can never break invoice processing.*
- **AC:**
  - An import-boundary check (e.g. import-linter) fails if core packages import anything consumer-specific.
  - The end-to-end conversation suite passes with no event subscribers configured, and with one unreachable subscriber.

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
  - The download is keyed by media id and streams to `/data/tmp/{id}/`; 60 s after the webhook is a soft target (logged, not enforced).
  - Every attempt, including retries, requests a fresh media URL (test: a retry after 6 minutes succeeds with a new URL).
  - The SHA-256 is stored.
  - Oversized files are rejected (S2.9).

**S2.4 · Send text, buttons and lists** · S · Reqs R-3.2
- **AC:**
  - The payload builders for text, reply buttons (≤ 3) and lists are unit-tested against the Meta schema.
  - Button IDs encode `invoice_id:action`.
  - Button titles over 20 characters or more than 3 buttons fail the builder.

**S2.5 · Interactive reply handling** · S · Reqs R-3.2, R-3.5 · Rules BR-S02 · Deps S2.4
- **AC:**
  - Button and list replies map to `(invoice_id, action)`.
  - A stale button (the invoice has moved past the state the button was for) gets BR-S02 and changes nothing.

**S2.6 · Fake channel for tests** · S · Deps S2.4
- *As the developer, I want an in-memory channel, so that end-to-end conversation tests run without Meta.*
- **AC:** Conversation scripts can assert the exact messages sent, by rule ID.

**S2.7 · Burst intake (FIFO) + back-pressure** · S · Reqs R-1.6 · Rules BR-S01 · Deps S2.2, S1.4
- *As a sender, I want to send several photos in a row and have each one handled, so that I don't wait between photos.*
- **AC:**
  - 5 photos in a row create 5 independent jobs, processed in arrival order per profile (per-profile advisory lock, PT-2).
  - One failing photo doesn't block the others.
  - Over `max_queued_per_sender`, the message is stored, not enqueued, and BR-S01 is sent.

**S2.8 · 24 h window + utility templates** · M · Reqs R-1.10 · Rules BR-S04 · Deps S1.5, S2.4, S0.7
- *As the owner, I want messages outside WhatsApp's 24 h window to still arrive, so that late completions and admin alerts aren't lost.*
- **AC:**
  - The outbox tracks the last inbound time per recipient and picks free-form or template accordingly.
  - The six templates (`invoice_done`, `manual_required`, `resume_notice`, `pending_user_reminder`, `batch_preview`, `admin_alert`) are mapped from their rule IDs.
  - A recorded 131047 error maps to `WINDOW_EXPIRED` and the message is resent as its template (PT-4).
  - A missing template keeps the message queued and alerts the owner (BR-S04).
  - The happy path sends no template (test counts template sends = 0).

**S2.9 · Media limits** · S · Reqs R-1.11 · Rules BR-E07 · Deps S2.3
- **AC:**
  - An image header declaring > 40 megapixels is rejected before decoding (test with a crafted header, memory stays flat).
  - Byte caps per media type from `limits.media` are enforced while streaming.
  - A zip with too many entries or too high a compression ratio is rejected.
  - Each rejection sends BR-E07.

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

**S4.3 · Unknown buyer → ask (only case with a menu)** · S · Rules BR-R03, BR-R07 · Reqs R-3.2
- **AC:**
  - A DNI, a missing buyer or an unmatched RUC gets one message with buttons for the flows allowed for the sender, plus Cancelar.
  - The choice resumes the pipeline.
  - With `unmatched: reject`, the invoice is cancelled with BR-R07.

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

**S4.8 · Timeouts → PENDING_USER** · S · Reqs R-3.7 · Rules BR-S08
- **AC:**
  - An unanswered question in `CONFIRMING`, `EXTRACT_FAILED` or `CONFIRM_INVALID` parks the invoice after 24 h with BR-S08, storing the waiting state.
  - Answering (directly or from `/pendientes`) returns the invoice to the stored state.
  - `/pendientes` lists it.

**S4.9 · Basic commands** · M · Reqs R-10
- **AC:**
  - `/ayuda`, `/estado <id>` (shows state **and the rule ID and message that explain it**), `/pendientes`, `/cancelar <id>` and `/reprocesar <id>` all work.

**S4.10 · Command scoping** · S · Reqs R-10.1 · Rules BR-S07 · Deps S4.9, S1.7
- **AC:**
  - A non-admin profile can see or change only its own invoices.
  - An unknown id and another profile's id get the same BR-S07 answer.
  - An admin can act on any invoice.

## E5 · Business-rule catalog
*S5.1 moved to E1.*

**S5.2 · Rule ID on every transition** · S · Reqs R-11.1 · Deps S5.1
- **AC:**
  - `invoices.rule_id` is set on every non-happy transition.
  - Rendering a message with a missing placeholder fails the test suite.

**S5.3 · Spanish user text, English logs** · S · Reqs R-9.8 · Deps S5.1
- **AC:**
  - Every user-facing string is sent from `rules/messages_es.py`; a test fails on string literals passed to channel send calls.
  - Log event names are English `snake_case` (checked in the log tests of S1.9).

## E6 · SUNAT validation
| ID | Story | Size | Key AC | Reqs / rules |
|---|---|---|---|---|
| S6.1 | OAuth client_credentials + token cache in `kv` | S | Refreshes 60 s before expiry; works across restarts | R-4.3 |
| S6.2 | `validarcomprobante` + BR-V01..V06 handling | M | `estadoCp` 0–4, RUC estado and outage cases each map to their rule and message (1 → continue, 0 → V01, 2 → V02, 3 → V05, 4 → V06); the V01 re-check is scheduled | R-4.1, R-4.2 |

## E7 · Fetcher framework
| ID | Story | Size | Key AC | Reqs / rules |
|---|---|---|---|---|
| S7.1 | Chain of responsibility + `FetchResult` + `fetch_attempts` stats | M | Per-flow order from config; stops at the first valid XML; a fetcher's final failure passes to the next fetcher; retries are scheduled jobs; every attempt is recorded with an enum reason | R-5.1, R-5.7 |
| S7.2 | `qr_url` fetcher | S | Follows the QR or printed URL through the request guard (S7.6); httpx streaming; size limits | R-5.2 |
| S7.3 | UBL 2.1 parser + XML-matches-invoice check | M | Wrong XML → BR-F06; the XML values become authoritative, and discrepancies are reported | R-5.6, R-2.7 |
| S7.4 | `manual` terminal step (BR-F07 message, built from the attempt reasons) | S | One `•` line per fetcher, using the BR-F texts (F01 for flow B) | R-5.5 |
| S7.5 | Accept user-sent PDF/XML for `MANUAL_REQUIRED` | S | A quoted or most-recent open invoice of the same profile (any channel) is matched; the XML is checked (S7.7); delivery resumes | R-1.5 |
| S7.6 | Outbound request guard (`net/guard.py`) for httpx and Chromium | M | Blocks non-https, `file:`/`data:`, private/loopback/link-local/CGNAT IPs, and redirects into them (table-driven tests, including DNS rebinding); pins the resolved IP; failures are enum codes (BR-F08); the browser container has internet-only egress | R-5.10 |
| S7.7 | Hardened XML parsing + XMLDSig check | M | `DOCTYPE`/entities/oversize rejected (BR-F09, billion-laughs and XXE fixtures); email/upload XML is authoritative only with a valid signature from `ruc_emisor`, otherwise a hint (BR-F10); SOL XML trusted by origin | R-2.12 |

## E8 · Gaps, GitHub and admin alerts
**S8.1 · Gap registry + dedupe** · S · Reqs R-11.2
- *As the owner, I want 10 invoices from the same unknown store to create one gap, not ten, so that alerts stay useful.*
- **AC:**
  - `raise_gap()` upserts by fingerprint in the same transaction.
  - `invoice_gaps` links the invoices.
  - The fetcher chain keeps running.

**S8.2 · Teach the portal link via chat** · M · Rules BR-F02, BR-S06 · Reqs R-11.4, R-10.2 · Deps S8.1
- *As a sender, I want the bot to ask me for the store's download link when it doesn't know it, so that the fix is mostly done by the time someone looks at it.*
- **AC:**
  - The question is asked only when the chain ended in `MANUAL_REQUIRED` with no portal entry, once per issuer, not per invoice.
  - The answer is validated (https, public host, ≤ 300 characters, no credentials, request guard).
  - "no sé" is accepted.
  - `/portal <ruc> <url>` does the same thing.
  - `printed_urls` are offered as a suggestion.
  - A taught link is stored as `proposed` and used only after the owner taps [Aprobar] (BR-S06).

**S8.3 · GitHub issue in the ops repo (idempotent)** · M · Reqs R-11.5, R-11.10 · Rules BR-D05 · Deps S1.5, S8.1, S0.10
- **AC:**
  - Created through the outbox in `gaps.github.repo` from `gaps/templates/gap_issue.md`, with labels `gap` and `gap:<kind>`, using a GitHub App installation token.
  - A hidden `<!-- gap:fp -->` marker prevents duplicates even after a DB loss.
  - Respects the daily budget.
  - A verified fix closes the issue with a comment (spec 06 §3).
  - Token failures (bad key, App uninstalled, missing permission) raise BR-D05 and leave the rows in the outbox.

**S8.5 · Owner chat alert + digest** · M · Reqs R-11.8, R-11.11
- **AC:**
  - One alert per new fingerprint, with the issue link, on each admin profile's preferred channel.
  - Re-alerts are rate-limited to `realert_days`.
  - Digest kinds go into one daily message.
  - The `admin_alert` utility template is used outside the 24 h window (S2.8).
  - The alert is still sent if GitHub is down.

*S8.4 and S8.6 moved to E16 (post-MVP, D44).*

**S8.7 · Config reload + auto-resume** · M · Reqs R-11.9, R-9.6 · Deps S1.2, S8.1, S12.2
- *As a sender, I want my stuck invoice to finish by itself once the portal is added, so that I don't resend anything.*
- **AC:**
  - `SIGHUP` reloads the config, which is validated **before** it is swapped in (a bad config keeps the old one and alerts the owner).
  - `reevaluate()` marks fixed gaps, re-enqueues the linked invoices and notifies their senders (`resume_notice`).
  - End-to-end: a CI deploy that adds an `active` portal entry resumes a `MANUAL_REQUIRED` invoice.

**S8.8 · PORTAL_BROKEN detection** · S · Rules BR-F03 · Deps S7.1, S8.3
- **AC:**
  - `broken_after` consecutive *technical* failures (not "not found") raise the gap and its issue.
  - The owner sets `status: broken` by hand (D44); the bot-made flip PR is S16.1 (post-MVP).

**S8.9 · Admin commands + GitHub App health** · S · Rules BR-D05 · Reqs R-10, R-11.10
- **AC:**
  - `/gaps` lists open gaps with their issue links.
  - `/gap ignorar <id>` silences one.
  - A daily check obtains an installation token and raises BR-D05 if it fails.

**S8.10 · `{gap_line}` in user messages** · S · Reqs R-11.3 · Deps S8.1, S7.4
- **AC:**
  - When a gap is linked, BR-F07 and BR-D02 messages include `{gap_line}`, saying what is missing and that the bot will retry by itself.
  - With no linked gap, the line is omitted (snapshot tests for both).

**S8.11 · Gap issue text safety** · S · Reqs R-11.12 · Deps S8.3
- **AC:**
  - The issue is rendered only from typed fields: an 11-digit RUC, the padrón business name, CPE type, reason codes, and a URL reduced to `https://host/path` matching `[A-Za-z0-9._~/-]`, in a code fence.
  - `@` is stripped; OCR text, sender text, counts and dates never appear (snapshot test with hostile inputs: markdown, mentions, `javascript:` URLs, control characters).

## E9 · Flow A (Google)
| ID | Story | Size | Key AC | Reqs |
|---|---|---|---|---|
| S9.1 | Google OAuth bootstrap script + refresh | S | OAuth app in **production** status; scopes `drive.file` (+ `spreadsheets` only for an existing sheet, + `gmail.readonly` only with the inbox fallback); a one-time consent stores the refresh token in the encrypted store; BR-D01 on revocation → `PENDING_AUTH` | R-6.4, R-6.5 |
| S9.2 | Drive sink: folders + upload + links | M | Path and filename templates; returns refs for the later sinks | R-6.1 |
| S9.3 | Sheets sink: column mapping + upsert by natural key | M | A re-run updates the row and never duplicates it; writes use `valueInputOption=RAW`; a business name like `=HYPERLINK(…)` is stored as text | R-6.2, R-6.3, R-6.6 |
| S9.4 | Completion message | S | ✅/⚠️/❌ + key fields + links in one message, on the origin channel | R-8.1, R-8.2 |
| S9.5 | `email_inbox` (Gmail, read-only, preferably a dedicated mailbox) with a retry schedule | M | Finds the XML/PDF by serie-número; 5m/1h/6h/24h retries as scheduled jobs; the invoice shows "esperando correo"; after the last retry the chain moves to the next fetcher | R-5.2, R-5.1 |

## E10 · Browser fetchers
| ID | Story | Size | Key AC | Reqs |
|---|---|---|---|---|
| S10.1 | BrowserPool (lazy, idle close, resource blocking) | M | Chromium is never resident while idle; peak RSS < budget (S0.9) | R-9.1 |
| S10.2 | `generic_form` adapter driven by `portals.yaml` | M | Fills, submits and downloads using only YAML; ≥ 3 seeded portals pass `portal-test` | R-5.8 |
| S10.3 | Captcha: local OCR attempts | S | ≤ 2 attempts, then hand over to S10.4 | R-5.3 |
| S10.4 | Captcha: ask the user in chat | M | Sends the image, waits up to 5 min, BR-F05 on timeout; reCAPTCHA → BR-F04 | R-5.3, R-5.4 |
| S10.5 | `invoicebot portal-test <key>` CLI | S | Runs one portal end to end against a sample; used in the runbook's "add a portal" steps | R-5.7 |
| S10.6 | `sunat_sol` fetcher (flow A only; blocked by S0.4) | M | Secondary SOL user; selectors in a file; credentials typed only when `page.url` matches the hard-coded SUNAT origins (test with a look-alike page); skipped in flow B with BR-F01 | R-5.2, R-9.10 |
| S10.7 | Custom adapters for the 2–3 portals the generic adapter can't handle | M each | Each passes `portal-test` | R-5.8 |

## E11 · Flow B (Microsoft)
| ID | Story | Size | Key AC | Reqs |
|---|---|---|---|---|
| S11.1 | MSAL device-code bootstrap + token cache, with auth-code + PKCE loopback fallback | S | Personal account works; minimal Graph scopes; if device code is blocked, PKCE login works; the work account result is documented (Q2) | R-7.1, R-7.9 |
| S11.2 | OneDrive sink (upload, folders, links) | M | Path templates; upload sessions for > 4 MB | R-7.1 |
| S11.3 | Excel template sink (openpyxl, mapped cells, If-Match) | M | Formatting kept; text written as strings with the leading-character escape; template SHA-256 checked; `.xlsm` only with opt-in; a missing, changed or badly mapped template → BR-D02 + `TEMPLATE_INVALID` gap | R-7.2, R-7.10 |
| S11.4 | Batch model + `BATCHED` state + `/lote` | S | Open batch per flow; count and total; invoices are `BATCHED` until the batch is sent, then `DONE`; files purged only after `sent`; a failed send keeps them `BATCHED` (BR-D03) | R-7.3, R-9.7 |
| S11.5 | `per_invoice` trigger *(post-MVP)* | S | One email per invoice | R-7.3 |
| S11.6 | `on_command` trigger + `/enviar` + preview/confirm | M | Preview with recipients, count and total; [Enviar] [Cancelar]; BR-D04 on an empty batch | R-7.3, R-7.6 |
| S11.7 | `scheduled` trigger (cron) *(post-MVP)* | S | Sends the open batch on schedule; combinable with on_command | R-7.3 |
| S11.8 | Mailer (Graph) + templates + large attachments | M | Zip or upload when over 3 MB; BR-D03 on failure | R-7.4, R-7.5, R-7.7 |
| S11.9 | Outlook variant of `email_inbox` | S | Same contract as S9.5 | R-5.2 |
| S11.10 | Gmail mailer *(post-MVP)* | S | Same contract as S11.8 through the Gmail API | R-7.7 |

## E12 · Hardening and operations
| ID | Story | Size | Key AC | Reqs |
|---|---|---|---|---|
| S12.1 | `memprofile.py` + latency report in CI | M | CI fails if peak > 800 MB or latency targets are missed | R-9.1, R-9.2 |
| S12.2 | *Moved to E1 (D46).* | | | |
| S12.3 | *Moved to E15 (M2).* | | | |
| S12.4 | Housekeeping (temp purge, stale jobs, padrón refresh) | S | Disk stays bounded; the padrón ZIP is deleted after import | R-9.7 |
| S12.5 | Runbook (re-auth tokens, add a portal, set a portal to `broken`, rotate secrets and the GitHub App key, handle a gap issue, restore a backup, Postgres down → webhook 503 and Meta retries) | S | Reviewed by the owner | R-9.15 |
| S12.6 | Optional: paid CPE API and captcha-solver adapters | M each | Off by default | R-5.2 |
| S12.7 | **Last step (D27):** on the owner's word, make the GitHub repo private and confirm the CI deploy still works; then reconsider the E16 gap automation (D44) | S | Repo private; deploy green | — |

## E3b · LLM fallback extraction (optional, off by default)
| ID | Story | Size | Key AC | Reqs |
|---|---|---|---|---|
| S3b.1 | `LLMExtractor` port + OpenAI-compatible adapter (DeepSeek `deepseek-flash` default, D43) | M | Off unless the flow opts in; never flow B; runs only after QR, XML and OCR fail; the image is downscaled to ≤ 1024 px; output passes the R-2.4 validators; fields tagged `source=llm`, confidence ≤ 0.5; always forces confirmation | R-2.11 |
| S3b.2 | `llm_usage` log and monthly cap | S | Each call records tokens and cost; at the cap the step is skipped with BR-E08 | R-2.11 |

## E13 · Integration contract (v1.1)
| ID | Story | Size | Key AC | Reqs |
|---|---|---|---|---|
| S13.1 | API tokens: create, list and revoke (CLI), hashed storage, `read`/`write` scopes | S | 401 without a token; a read token can't call write endpoints | R-12.3 |
| S13.2 | REST `/api/v1`: invoices list/get, spend summary | M | OpenAPI published; filters by date, flow, issuer, state | R-12.1 |
| S13.3 | MCP server: read tools | M | An MCP client (e.g. Claude Code or Jarvis) lists and queries invoices with a read token | R-12.2 |
| S13.4 | MCP and REST write tools through the state machine, confirmed by the owner | M | `reprocess`, `cancel` and `attach_document` create a pending action; the owner approves in chat (BR-S05); only then they behave exactly like the chat commands, with the same BR ids; the token name is recorded | R-12.2, R-12.8 |
| S13.5 | Event publisher: subscribers, HMAC over `timestamp.body`, outbox retries | M | A test subscriber receives `invoice.delivered` (flow A at `DONE`, flow B when the batch is sent) with a valid signature; a replay older than 5 min or a repeated event id is rejected by the reference subscriber; a down subscriber doesn't block the pipeline | R-12.4, R-12.5, R-12.8 |
| S13.6 | Contract tests: OpenAPI snapshot, MCP tool schemas, event schema | S | CI fails on an unversioned breaking change | R-12.6 |
| S13.7 | Untrusted text + exposure hardening | M | `issuer_name`, `printed_urls` and OCR text appear only under `untrusted` with `trust` and `source`, ≤ 120 characters, control characters and markdown stripped; no raw OCR or LLM text; MCP rejects a foreign `Origin`; the runbook sets up the tunnel access policy | R-12.8 |

## E14 · Telegram channel (v1.1)
| ID | Story | Size | Key AC | Reqs |
|---|---|---|---|---|
| S14.1 | Telegram adapter on the `Channel` port: webhook with secret token, photos and documents, inline keyboards | M | The same conversation tests that pass on the fake channel pass on Telegram fixtures | R-1.7 |
| S14.2 | Cross-channel use of a profile (data model from S1.7) | S | An invoice started on one channel can be queried with `/estado` on the other; replies still go to the origin channel | R-1.7, R-1.9 |

## E15 · Operations baseline (M2)
| ID | Story | Size | Key AC | Reqs / rules |
|---|---|---|---|---|
| S12.3 | *(moved from E12)* Nightly encrypted `pg_dump` (without `kv`) + config backup, encrypted to an `age` public key, stored off-site (Drive, OneDrive or object storage) | S | RPO ≤ 24 h; `restore_drill.sh` restores into an empty Postgres and passes smoke queries; drill run before go-live; a failed backup raises BR-O04 | R-9.11 |
| S15.1 | Retention per table | S | `messages.payload_json` nulled after 30 days; `outbox`, `events`, `jobs` deleted after 90 days; `invoices` kept; logs about non-allowlisted senders carry only a hash | R-9.12, R-1.2 |
| S15.2 | Health, uptime and self-alerts | M | `/health` returns 503 with the DB down; an external uptime check is configured; failed jobs (BR-S03), outbox backlog (BR-O01), disk usage (BR-O02) and channel token problems (BR-O03) alert the owner, rate-limited; with Postgres stopped the webhook returns 503 and a replayed delivery succeeds after restart | R-9.14, R-9.15 |

## E16 · Gap automation (post-MVP, D44)
To be reconsidered after the main repo goes private (S12.7). Needs the GitHub App on the main repo with Contents and Pull requests (D45).
| ID | Story | Size | Key AC | Reqs |
|---|---|---|---|---|
| S8.4 | *(moved from E8)* Draft PR with portal stub | M | Deterministic branch `gap/<slug>`; YAML stub appended through a serializer with a parse-back equality check, `status: todo`, plus the approved URL; draft PR with "Closes #n" and a checklist; never touches the default branch; re-running duplicates nothing | R-11.5 |
| S8.6 | *(moved from E8)* `gap-watch` scheduled workflow | S | The workflow fails while a `gap` issue is older than `gap_sla_days`, and passes otherwise | R-11.7 |
| S16.1 | `PORTAL_BROKEN` flip PR | S | A PR flips the entry to `status: broken`, with the same serializer and parse-back check | R-11.5 |
