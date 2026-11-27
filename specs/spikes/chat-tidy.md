# Spike: chat-tidy live run (9z12, D206)

Status: waiting for the deploy of 9z12 and a live receipt.

## Setup

- `main` with 9z12 deployed with `deploy/deploy-live.sh <tag>`.
- The live config adds `chat_documents`: `work_microsoft` `[pdf, xml, excel]`, `personal_google` `[pdf]`, then
  reloads (runbook §14).
- The earlier test invoice of the La Leña receipt forgotten (runbook §13), so it is accepted again.

## What to record

| Check                                                                           | Result |
| ------------------------------------------------------------------------------- | ------ |
| One summary, after the fetch, with no `⚠️ Revisa` line and BR-F16's lines in it |        |
| No separate BR-F16 message                                                      |        |
| The final message carries [📎 Ver documentos]                                   |        |
| The press posts the three files (PDF, XML, Excel), each captioned with its name |        |
