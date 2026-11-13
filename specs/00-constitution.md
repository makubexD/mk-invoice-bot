# 00 · Constitution

These principles govern every spec, design decision and pull request. A change that breaks one of them needs an explicit amendment here first.

**Scope (D39):** this repo builds **mk-invoice-bot**, a standalone bot that captures Peruvian invoices (SUNAT CPE) over WhatsApp and Telegram, validates them, and delivers them to the configured sinks. It works fully on its own. Other systems, such as the personal agent **Jarvis** (separate repo), integrate only through the bot's public contract (P10). The bot never depends on them.

*Amended 2026-11-10: P1, P2 and P3 were revised (D24, D31, D40), P10 was added (D39). P6 and P9 were extended. Later the same day: P1 (D42), P3 (D43), P6 (secrets, untrusted text), P9 (D44, D46) and P10 (write confirmation).* *2026-11-12: P6 (store directory, D47) and P9 (gap PR from a template as the last v1 step, D52).* *Later on 2026-11-12: P3 reworded (D57, D58).*

## P1 · Near-zero cost
- Free tiers and open-source software. A paid service is allowed only when it is the official path (Meta Cloud API) or as an **optional, disabled-by-default** adapter (LLM fallback, paid CPE APIs, captcha solvers).
- Target: **2 GB RAM host, about US$10–12/month** (D42), with room to share the host with other apps later. Development runs locally in Docker (D31).

## P2 · Lean runtime, provider-neutral deployment
- **Postgres** is the database, the job queue (`FOR UPDATE SKIP LOCKED`) and the scheduler (D40). No Redis, Celery or Kafka.
- One async app process. **At most one heavy job at a time** (OCR or browser), enforced by a semaphore.
- Headless Chromium starts **on demand** and closes after an idle timeout. It is never kept running.
- Stream media to disk, downscale images before processing, and delete temporary files right away.
- Prefer thin REST calls (`httpx`) over heavy SDKs.
- Everything runs in containers, configured only by environment variables and config files (12-factor). Nothing may assume a specific VPS or cloud provider.

## P3 · Deterministic extraction first
- Order of precedence for reading an invoice: **QR decode → OCR + regex → vision LLM (optional fallback, per-flow opt-in)** (D24, D43). The official XML is not an input (D58): once the bot downloads it, it is authoritative for the amounts (R-2.7).
- The LLM runs only when every deterministic method fails, and only for flows that opt in. **Flow B (work invoices) never goes to the LLM** (D43). It is called through a provider-neutral port, its output is re-checked by the same validators (R-2.4), and its fields carry `source=llm` with low confidence and **always need user confirmation**.
- Rules that code can decide (RUC check digit, routing, totals, duplicates) are never delegated to an LLM.
- Every extracted field carries its `source` (qr|xml|ocr|padron|llm|user) and a `confidence`.

## P4 · Human in the loop over silent failure
- The user always sees the parsed data and can **confirm, edit or cancel** it before delivery (`confirm_mode`, D21).
- When automation can't get a document (captcha, portal down, no access), the bot tries the next fallback. When every fallback is exhausted, it sets **`MANUAL_REQUIRED`** and tells the user exactly what is missing.

## P5 · Pluggable everything
- Channels (WhatsApp, Telegram), extractors, document fetchers, delivery sinks (Google, Microsoft, email, webhooks) and the LLM fallback are **ports with adapters**, chosen by configuration.
- Flows are **declarative config** (YAML), not code. Adding a third flow needs no core change.

## P6 · Security and privacy
- Accept messages only from allowlisted senders (WhatsApp numbers, Telegram user ids). Unknown senders are ignored and logged.
- Verify every webhook (Meta `X-Hub-Signature-256`, Telegram secret token). A webhook is acknowledged only after the inbound message is durably stored.
- API and MCP clients authenticate with **scoped tokens**: read-only by default, with explicit write scopes (P10).
- Secrets (OAuth refresh tokens, SUNAT client secret, SOL credentials, API tokens) never enter git. They are **encrypted at rest** (`age` or `systemd-creds`) and decrypted into memory at start; `.env` holds only non-secret settings. They are never logged, and each adapter receives only the secrets it declares (R-9.9, R-9.10). A missing secret **fails closed**: no default ever turns a check off.
- Text from senders, OCR, LLMs and third-party sites is **untrusted**. It never reaches GitHub (R-11.12), every outbound fetch passes one request guard (R-5.10), and the contract marks it `trust: untrusted` (R-12.8).
- Use a **secondary SOL user** with query-only permissions. Scrape only the owner's own invoices, rate-limited, and respect the target sites' terms.
- While the repo is public (D27), no real RUCs, phone numbers, personal data or secrets in git, including tests and fixtures. The one exception is the store directory (`config/stores/`), committed by the owner's decision (D47).

## P7 · Idempotent and resumable
- Each inbound message is idempotent on its channel message id. Each invoice has a natural key `(ruc_emisor, tipo, serie, numero)`. Duplicates are detected and never processed twice.
- Every step is a persisted state transition, so a crash or restart resumes where it stopped.

## P8 · Spec-first and test-first
- Every requirement has an ID (`R-x.y`), and every task and test references one.
- A golden dataset of invoice photos with expected JSON is the acceptance test for extraction. While the repo is public, it is synthetic or anonymised.

## P9 · Fail loud, never silent
- Every stop, warning or reroute has a **business rule ID** (`BR-*`) and a clear Spanish message telling the user what happened and what comes next (spec 06).
- When the cause is a missing capability in the repo, the system raises a **gap** (D44):
  - an issue in the private ops repo;
  - a chat alert to the owner.
  - At the end of development, once the repo is private, a gap also opens a PR from a template that stays red until the owner completes it (S16.2, D52). Red-CI watches and other bot-made PRs are post-MVP.
- Deploys have **one path** (D37, D46): a green CI run on `main` deploys code and config, with `pg_dump`, migrations, a health check and automatic rollback. When the fix is deployed, the affected invoices resume automatically.
- The UX stays smooth: no menus when the code can decide (the flow comes from the buyer RUC). Questions are asked only when data is missing or ambiguous.

## P10 · Integration through a contract, never a dependency
- The bot exposes a **versioned public surface**:
  - a REST API (OpenAPI);
  - an **MCP server** with tools such as listing and querying invoices, spend summaries, reprocessing and pending manual items;
  - **outbound event webhooks** (`invoice.delivered`, `invoice.manual_required`, `gap.raised`, …) signed with HMAC.
- Consumers (Jarvis, n8n, future apps) use only this surface. No shared libraries and no shared database.
- A write through the contract creates a pending action that the **owner confirms in chat** (R-12.8).
- The bot must work fully with zero consumers. Breaking changes to the contract need a new version.
