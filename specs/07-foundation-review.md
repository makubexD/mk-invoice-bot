# 07 · Foundation review (2026-11-10)

Done while Jarvis was briefly scoped into this repo (D22, later superseded by D39). Result: **D38, patterns only.** No third-party code is copied. Patterns PT-1, PT-2, PT-4 and PT-8 also apply to mk-invoice-bot; the full list applies to Jarvis.

## Candidates

| Repo | License | Activity | Verdict | Main reasons |
|---|---|---|---|---|
| [gerfru/NilesAI](https://github.com/gerfru/NilesAI) | AGPL-3.0 | 1 author; mostly bot commits since July | Patterns only | Unofficial WhatsApp (Evolution/Baileys, no HMAC); Ollama-bound; memory is a plain key-value table; approvals in process memory; no permission levels or audit log; ~3.8 GB Compose; AGPL would infect this repo |
| [chasqui-stack/chasqui](https://github.com/chasqui-stack/chasqui) | Apache-2.0 | 1 author; no commits since 2026-09-24; WhatsApp gateway untouched since June | Patterns only | LangChain `create_agent` (clashes with D29); memory has no provenance and does destructive nearest-neighbour updates; retrieval silently off without a Google key; no durable webhook idempotency or retry; no MCP |
| [FiveTechSoft/FiveAgent](https://github.com/FiveTechSoft/FiveAgent) | MIT | 1 author, active | Not reviewed | Go; outside the Python stack |
| [Chessing234/w-agent](https://github.com/Chessing234/w-agent) | MIT | 3 commits | Rejected | Too immature; TypeScript |

Full reviews were run read-only. The key claims were spot-checked in the source.

## Patterns to re-implement (no code copied)

| ID | Pattern | Source | Where it lands in Jarvis |
|---|---|---|---|
| PT-1 | Canonical message contract (inbound and outbound Pydantic models) between channel adapters and the core | Chasqui `core/app/schemas/ingest.py` | `core` ↔ `channels` boundary. We add the inbound message id (idempotency), attachments by reference, and the sender role |
| PT-2 | `FOR UPDATE SKIP LOCKED` queue plus a per-identity advisory lock to merge bursts of messages | Chasqui `coalesce_worker.py` | Postgres job queue (D33). Don't hold the row lock during LLM calls |
| PT-3 | Tool-module registry where each module adds its own system-prompt fragment | Chasqui `core/app/modules/*` | Tool registry, extended with permission level, scope and audit |
| PT-4 | Mapping WhatsApp error 131047 to `WINDOW_EXPIRED` | Chasqui `sender.py` | WhatsApp adapter. We also add template fallback |
| PT-5 | Wrapping untrusted content in delimiters before it reaches the LLM | NilesAI `prompts.py` | Prompt builder. One of several injection defences, not the only one |
| PT-6 | MCP client manager: tool-name prefixing and schema simplification | NilesAI `mcp/client.py` | MCP client. We also add an HTTP transport and per-server allowlists |
| PT-7 | Parser that recovers tool calls a model writes as plain text (json-repair) | NilesAI `agent/text_tool_parser.py` | DeepSeek robustness, if Pydantic AI doesn't already cover it |
| PT-8 | CI security stage: gitleaks, pip-audit, bandit, semgrep; mypy strict | NilesAI `.github/workflows/ci.yml` | Our CI from Phase 1 |

## Anti-patterns to avoid (seen in both)
- Memory without provenance or confidence; memory edits or deletes that silently match the wrong fact.
- Approvals held only in memory; no audit trail.
- Webhook dedup held only in memory; acknowledging Meta before the message is safely stored.
- Advertised provider swaps whose dependencies aren't actually installed.
- Defaults that quietly turn off security (empty API key skips the auth check, empty JWT secret).
