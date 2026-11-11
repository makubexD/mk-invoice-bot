# 00 · Constitution

These principles govern every spec, design decision and pull request. A change that breaks one of them needs an explicit amendment here first.

**Scope (D22):** the repo builds **Jarvis**, a personal AI platform (infrastructure, memory, agent, tools, interfaces). Its first purpose and domain module is the **SUNAT invoice pipeline** (`modules/invoices`). Principles marked *[invoices]* apply only to that module. All others apply to the whole system.

*Amended 2026-11-10 (D22–D38): P1, P2 and P3 were rewritten; P10–P13 were added.*

## P1 · Modest, controlled cost
- Target infrastructure: **~4 GB RAM host, ~US$10–25/month** (D23). LLM spend has a **hard monthly cap**, with an alert at 80%.
- Open source first. A paid service is allowed when it is the official or only sane path (Meta Cloud API, the LLM API), or as an **optional adapter that is off by default**.
- Every LLM call is metered: tokens, cost, latency and purpose, stored in Postgres (D36).

## P2 · Lean runtime, provider-neutral deployment
- **One Postgres** (with pgvector) is the source of truth, the job queue and the scheduler (D33). No Redis, Celery, Kafka or second database until a measured need is written down as an amendment.
- Everything runs in containers with Docker Compose, configured only through environment variables and config files (12-factor). Nothing may assume a specific VPS or cloud provider (D31).
- Heavy work (OCR, headless browser) runs **on demand**, bounded by a semaphore. Chromium is never kept running.
- Stream media to disk, downscale images, and delete temporary files right away.

## P3 · Deterministic first, LLM where it adds value
- An LLM drives conversation, memory extraction and agent planning. It is reached only through a **provider-neutral interface** chosen by config (DeepSeek by default, D30).
- *[invoices]* Extraction order is **QR → official XML → OCR + regex → vision LLM (fallback only)** (D24). LLM-extracted fields carry `source=llm` and low confidence, and always need user confirmation.
- Any rule that code can decide (RUC check digit, routing, totals, duplicates) is decided by code, never by the LLM.

## P4 · Human in the loop over silent failure
- The user always sees what will be delivered or done, and can **confirm, edit or cancel** it (`confirm_mode`, D21).
- When automation can't finish (captcha, portal down, no access), the system tries the next fallback. When every fallback is exhausted, it sets **`MANUAL_REQUIRED`** and says exactly what is missing.

## P5 · Pluggable everything
- Channels, LLM providers, embedders, extractors, fetchers, delivery sinks and tools are **ports with adapters**, chosen by configuration.
- `core` never imports a domain module. Domain modules use `core` only through its public interfaces (agent, memory, tools, jobs, approvals, channels).
- Domain flows are **declarative config** (YAML). Adding a flow or a module needs no change to `core`.

## P6 · Security and privacy by default
- **The owner is the only principal with access to memory, tools and general chat.** Other allowlisted senders can only **submit invoices and answer questions about them** (D25). Unknown senders are ignored and logged.
- Verify every webhook signature (`X-Hub-Signature-256`). Webhooks are never acknowledged before the inbound message is durably stored.
- Secrets never enter git. They are encrypted at rest and never logged. A missing secret **fails closed**: no default ever turns a check off.
- Use a **secondary SOL user** with query-only permissions. Scrape only the owner's own invoices, rate-limited.
- While the repo is public (D27), no real RUCs, phone numbers, personal data or secrets in git, including tests and fixtures.

## P7 · Idempotent and resumable
- Every inbound message is idempotent on its channel message id. Every invoice is idempotent on `(ruc_emisor, tipo, serie, numero)`.
- Every multi-step process (invoice, agent task, approval) is a **persisted state machine** in Postgres. A crash or restart resumes where it stopped.

## P8 · Spec-first and test-first
- Every requirement has an ID (`R-x.y`), and every task and test references one.
- *[invoices]* A golden dataset of invoice photos with expected JSON is the acceptance test for extraction. It is synthetic or anonymised while the repo is public.
- Memory and agent behaviour have their own evaluation sets (recall, contradiction handling, refusal of injected instructions).

## P9 · Fail loud, never silent
- Every stop, warning or reroute has a **business rule ID** (`BR-*`) and a clear Spanish message saying what happened and what comes next (spec 06).
- A missing capability raises a **gap**: a GitHub issue plus a draft PR, red CI, and a WhatsApp alert to the owner. When the fix is deployed, affected work resumes automatically.
- No menus when code can decide. Questions are asked only when data is missing or ambiguous.

## P10 · Memory is owned, explainable and correctable
- Memory is a dedicated subsystem: it extracts, classifies, deduplicates, consolidates and retrieves. It never just replays raw transcripts.
- Every memory records **provenance** (source message or document), **timestamps**, **confidence** and its **epistemic status**: `stated` (the owner said it), `inferred` (Jarvis concluded it) or `uncertain`.
- Updates never destroy history: a corrected fact is **superseded**, not overwritten. Deletion is explicit, audited and real (hard delete on request).
- The owner can inspect, correct and delete any memory from the CLI, WhatsApp, and later the dashboard.

## P11 · Least privilege and graded autonomy
- Every tool declares a permission level: **L0** read, **L1** low-risk write, **L2** controlled execution, **L3** sensitive. L3 always needs explicit owner approval. L2 does too unless a standing grant exists.
- Approvals are persisted and expire. They are bound to the exact action and arguments.
- **Content never grants permissions.** Instructions found in documents, emails, web pages, invoices or tool output are data. They cannot add tools, raise levels or approve actions.
- Tool access can be revoked at runtime, per tool and per module.

## P12 · Everything is audited
- Every agent action, tool call, approval, memory write and delivery is written to an append-only audit log: who, what, arguments (secrets redacted), result and cost.

## P13 · Simple until proven otherwise
- Prefer the smallest dependency that does the job. A framework (LangGraph, Redis, Next.js, n8n, self-hosted Langfuse) is adopted only when a written need shows the simpler option failing (D29, D33, D35, D36).
- Foundation projects contribute **patterns, not code** (D38, spec 07).
