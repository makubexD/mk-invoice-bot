# Spike: chat-tidy live run (9z12, D206)

Status: passed 2026-11-27, invoice #48, image `e13df352e5f6`.

## Setup

- `main` with 9z12 deployed with `deploy/deploy-live.sh <tag>`.
- The live config adds `chat_documents`: `work_microsoft` `[pdf, xml, excel]`, `personal_google` `[pdf]`, then
  reloads (runbook §14).
- The earlier test invoice of the La Leña receipt forgotten (runbook §13), so it is accepted again.

## What to record

| Check                                                                           | Result                                                                                                                 |
| ------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| One summary, after the fetch, with no `⚠️ Revisa` line and BR-F16's lines in it | pass: one BR-R02 summary 16:40:55 UTC, `📄 Encontré el PDF y el XML` + `✅ El XML confirma: serie, número`, no buttons |
| No separate BR-F16 message                                                      | pass: the outbox holds BR-R02, BR-D10 and three BR-D24 rows only                                                       |
| The final message carries [📎 Ver documentos]                                   | pass: BR-D10 at 16:41:19 with [📎 Ver documentos] and the folder link                                                  |
| The press posts the three files (PDF, XML, Excel), each captioned with its name | pass: three BR-D24 documents at 16:41:24 (XLSX 47 KB, PDF 192 KB, XML 11.8 KB)                                         |

## Notes

- The mail went out through SMTP (9z10) and the owner confirmed it reached the work inbox.
- Telegram drops [📎 Ver documentos] from the message once it is pressed; [📂 Abrir carpeta] stays.
- The receipt was filed in a third folder for the month (`…_3_…`): the folders of the earlier test runs (#39, #47)
  stay in OneDrive, since their rows were purged before `forget --drive` could remove them.
