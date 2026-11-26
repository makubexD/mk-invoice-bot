# Live run: open links and review before send (9z, D194)

Date: 2026-11-26. Live image `c8676a169670`, flow B on `send: review` with `modes: [graph]`, Telegram, invoice #34
(a dry_run batch `prepared` before review existed).

| Check                                   | Result                                                                                              |
| --------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `/revisar 34`                           | the Excel, PDF and XML arrive as documents, then BR-D19 with [Enviar] [Cancelar] [📂 Abrir carpeta] |
| `/revisar 34` again                     | BR-D22: the batch is `review`, no longer `prepared`                                                 |
| [📂 Abrir carpeta] on the phone         | the OneDrive app answers "can't be opened in the app" and offers the browser                        |
| ... in the browser                      | opens the invoice folder                                                                            |
| [Cancelar]                              | BR-D20 with [Enviar]                                                                                |
| [Enviar]                                | BR-D21, then BR-D10 with the folder link; the batch is `sent`, the folder renamed `_Sent`           |
| The mail                                | Graph accepted it, then the work tenant bounced it: `550 5.7.501 Spam abuse detected from IP range` |
| A manual mail and a manual Resend of it | both delivered from the same personal account, the Resend with its three attachments                |
| dry_run `.eml` on the phone             | deferred: no live flow is on dry_run; the integration tests cover the document row                  |

Findings:

- **`/revisar` answered nothing** until the documents arrived. Fixed in `cec89a1`: it answers at once.
- **The admin menu** lists a new command only after `invoicebot telegram commands` runs again; the runbook says so.
- **The folder link opens in the browser, not the OneDrive app.** Graph's `webUrl` for a personal drive item
  (`onedrive.live.com/?cid=…&id=…`) is not a form the Android app opens. A share link (`1drv.ms`) would, but D194
  never creates one, and Telegram URL buttons take no app scheme. Kept as a known limit.
- **The bounce is the sending IP, not the mail.** 5.7.501 is the recipient tenant refusing an Outlook.com outbound
  range; the same mail resent by hand passed. Outlook.com picks the outbound IP per mail, so a bounce can come back at
  any send, and the bot cannot see it: BR-D10 says "enviada" once Graph accepts.
