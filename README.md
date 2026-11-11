# mk-invoice-bot

WhatsApp and Telegram bot that takes a photo of a Peruvian electronic invoice (SUNAT CPE). It:

1. extracts the key data deterministically (QR, then XML, then OCR); an optional vision-LLM fallback is opt-in per flow and never used for work invoices,
2. picks the workflow automatically from the buyer RUC (your company → Google, your employer → Microsoft); it asks only when the RUC is unknown,
3. fetches the official PDF/XML,
4. delivers the result, either to **Google Drive + Sheets** or to **OneDrive + Excel template + email**,
5. replies in the chat with the outcome. When something is missing (for example, an unknown store portal), it explains why in the chat, opens an issue in a private ops repo and alerts the owner.

It is a standalone app. Other systems, such as the personal agent Jarvis (separate repo) or n8n, integrate only through its REST API, MCP server and signed event webhooks.

The project uses **spec-driven development**. The specs below are the source of truth, and the code follows them.

| # | Document | Purpose |
|---|----------|---------|
| 0 | [specs/00-constitution.md](specs/00-constitution.md) | Non-negotiable principles (cost, lean runtime, deterministic first, security, integration contract) |
| 1 | [specs/01-research.md](specs/01-research.md) | What exists, what to reuse, with sources |
| 2 | [specs/02-requirements.md](specs/02-requirements.md) | User stories plus testable acceptance criteria (EARS) |
| 3 | [specs/03-design.md](specs/03-design.md) | Architecture, components, data model, state machine, resource budget |
| 4 | [specs/04-tasks.md](specs/04-tasks.md) | Backlog of small/medium user stories (epics → stories → acceptance), traced to requirements |
| 5 | [specs/05-open-questions.md](specs/05-open-questions.md) | Decisions taken and questions still pending from the owner |
| 6 | [specs/06-business-rules.md](specs/06-business-rules.md) | Business rules (BR-*), user messages, capability gaps and owner alerts |
| 7 | [specs/07-foundation-review.md](specs/07-foundation-review.md) | Review of existing open-source agents; patterns reused (no code copied) |
| – | [config/example.yaml](config/example.yaml) | Shape of the user-editable configuration |

Status: **planning**. No code yet.
