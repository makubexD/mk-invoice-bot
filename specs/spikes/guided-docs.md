# Spike: guided-docs live run (9s, D189)

Date: 2026-11-24 (early morning, Lima). Host: the local live stack `invoicebot-live` (D141). Image `8fb00b64eeae`.
Channel: the owner's Telegram. Tickets: the PEBSA ticket (QR), sent four times as #20–#23.

## What each item showed

| Item                                                 | Result                                                                                                                                                                                                                                                    |
| ---------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| QR-only ticket → summary with no buttons             | As specified: #21, #22, #23 `🧾 Factura F001-12367 …`, `🔎 Leído de: QR`, `➡️ RK Export (Drive)`, `⏳ Lo estoy archivando.`, no buttons.                                                                                                                  |
| Same invoice again → BR-E04                          | #20 named #12 and was cancelled as a duplicate; `/cancelar 12` answered that #12 is already in a batch.                                                                                                                                                   |
| First mailbox wait → BR-F13                          | As specified: `⏳ Comprobante #21: me falta el PDF para archivarlo.`, `Ya busqué: • Portal del comercio: no lo conozco`, `Vuelvo a mirar el correo a las 05:15.` (Lima time, correct), [📎 Te envío el PDF] [🔗 Enseñar el portal] [⏳ Espero el correo]. |
| [🔗] → BR-F14 with the printed URL                   | **Not reached.** Every press (#21, #22, #23) answered BR-F02: the reading had no `printed_url`, see the findings.                                                                                                                                         |
| [Sí] → the owner's BR-S06 alert                      | Not reached (needs BR-F14, or a typed answer to BR-F02, which was not sent).                                                                                                                                                                              |
| [⏳] and [📎]                                        | Not run live; covered by `tests/integration/test_pipeline_docs_help.py`.                                                                                                                                                                                  |
| OCR-filled required field → [Corregir] [Anular]      | Not run live: every ticket kept a readable QR. Covered by the integration tests.                                                                                                                                                                          |
| Bare `/cancelar`, `/reprocesar` → buttons; `/estado` | Not run live; the owner cancelled with `/cancelar <id>`. Covered by `tests/integration/test_commands.py`.                                                                                                                                                 |

## Findings

- **The printed URL is lost to OCR, not skipped.** OCR runs on every photo. Telegram delivers the photo at 598×1280,
  so the line under `ESTE COMPROBANTE PODRÁ SER CONSULTADO EN:` (`https://sv.ecomprobantes.pe/pebsa`) is about
  8 px tall and the full-page pass reads `hiips:/Is etomiprobantes, aio`. A re-read of only that strip, 3× upscaled,
  `eng`, `--psm 7`, gave `hitps.//sy.ecomprobantes pe/pehsa`: close, not exact. With no URL, [🔗] falls back to
  BR-F02 as designed.
- **WhatsApp alerts in Telegram.** The live config still had the WhatsApp channel with the expired 24 h token of
  2026-11-16, so every WhatsApp send raised BR-O03 (sent to Telegram by D185) and the unsent rows raised BR-O01
  (`Hay 27 mensajes sin enviar`). All 54 unsent WhatsApp rows were admin alerts queued before alerts moved to
  Telegram; none carried an invoice. The `whatsapp_cloud` entry is commented out of the live config until Meta
  verifies the business, and the rows are `dead` with a `discarded 2026-11-24` reason.

## The owner's notes (go to the follow-ups, in this order)

1. **Message review.** Every message says what happened, what the bot does next and when, and offers a button for
   each sensible answer; each answer teaches the bot something. BR-F02 has no buttons (add at least [No sé]).
   `Vuelvo a mirar el correo a las 05:25.` was unclear: say that the bot checks the inbox for the merchant's PDF,
   files it on its own when it arrives, and what happens when it never does.
2. **Smart portal guess.** When OCR finds `CONSULTADO EN` but no URL, re-read the strip below it, repair the result
   (scheme, a missing dot before the top-level domain, the path matched to the issuer's name, provider hosts learned
   from approved links), and offer it as BR-F14 with [Sí] [Corregir] [No sé]. A vision LLM only as a later opt-in
   (never flow B). Decide at plan time whether this folds into 9t `portal-probe`.
3. **Ops alerts.** Alerts say which channel and why and offer actions: a daily channel check, a warning before a
   token expires, Meta's quality and account webhooks, a GitHub-issue fallback when no chat channel reaches an
   admin, an admin `/canales` command whose on/off lives in the database, [Reintentar] / [Descartar] on the backlog
   alert, and token renewal through a host command, never through chat.
