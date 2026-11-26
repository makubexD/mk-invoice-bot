# Live run: settled buttons and BR-D10's text (9z2, D195)

Date: 2026-11-26. Live image `2ac05ff7e6d7`, flow B on `send: review` with `modes: [graph]`, Telegram, invoice #39
(F014-15630), whose BR-D19 was posted at 13:52 by the previous image.

| Check                             | Result                                                                                               |
| --------------------------------- | ---------------------------------------------------------------------------------------------------- |
| [Cancelar] on BR-D19              | BR-D19 loses [Enviar] [Cancelar] and keeps [📂 Abrir carpeta]; BR-D20 arrives with one [Enviar]      |
| A message posted by the old image | settles too: the keyboard is read from the callback's own message, so nothing stored was needed      |
| [Enviar] on BR-D20                | BR-D20 loses its button; BR-D21, then BR-D10 with [📂 Abrir carpeta]                                 |
| BR-D10's text                     | `✅ F014-15630 archivada en 2026-10-Support_2_LaLena. Outlook aceptó el correo para <work address>.` |
| The mail                          | Graph accepted it (Sent folder, three attachments); the work tenant bounced it again with 5.7.501    |
| Time from [Enviar] to BR-D10      | 50.7 s: the `deliver` job (`duration_ms: 50721`), started at once after the press                    |

Findings:

- **The buttons and the text work as D195 says.** No second [Enviar] remains in the chat.
- **The bounce is unchanged** (5.7.501 from an Outlook.com outbound range, as in `specs/spikes/open-links.md`). BR-D10
  now says only that Outlook accepted the mail, which stays true. Reading the bounce is 9x.
- **The [Enviar] delivery takes about 50 s, and the log does not say where.** The re-run files the folder again (manifest
  check, PDF and XML), builds and uploads the Excel, then sends the mail, each a Graph call; only the step's total is
  logged. The owner sees BR-D21, then nothing for most of a minute. Follow-up: log each outbound Graph call's duration
  (method, host, path template, status, ms; no ids or tokens) to find the slow one, then skip what a reviewed re-run
  does not need: the folder and the Excel were already filed by the review run.
