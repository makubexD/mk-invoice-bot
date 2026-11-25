# Spike: smart-replies live run (9r, D188)

Date: 2026-11-23 (late evening, Lima). Host: the local live stack `invoicebot-live` (D141). Image `a2dffe952ac2`,
then `da107a086e03` with the list fix below. Channel: the owner's Telegram; the rehearsal clean-up and the list
re-check went through the console channel (same profile).

## What each item showed

| Item                                                           | Result                                                                                                                                                                       |
| -------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Cropped or blurry photo → BR-E01 names read and missing fields | **Not reproduced live.** Every photo kept a readable QR, so the QR gave every required field. Covered by the integration tests; repeated in the 9s live run.                 |
| [Editar] → the missing lines; answers → routed summary         | Not reached (needs BR-E01).                                                                                                                                                  |
| Routed summary                                                 | As specified: `Emisor: PEBSA COMBUSTIBLES S.A. (RUC …)`, `🔎 Leído de: QR`, `➡️ RK Export (Drive)`, `SUMMARY_MOVING`, [✏️ Corregir] [✖️ Anular].                             |
| Same invoice again → BR-E04                                    | As specified: `Este comprobante ya lo registré el 23/11 (#15): PEBSA COMBUSTIBLES S.A. (RUC …), S/ 160.41.` + [Ver #15]; the new invoice (#16) was cancelled as a duplicate. |
| [Ver #15]                                                      | Answered like `/estado 15`: state, then `Último aviso (BR-R01): …` with the summary as it was sent.                                                                          |
| `/cancelar` without an id, then with one                       | `COMMAND_NEEDS_ID`, then `🚫 Cancelado: …`                                                                                                                                   |
| `/estado` alone, `/pendientes`                                 | Five newest, then `Para ver uno: /estado 12`; `/pendientes` lists #9, #10, #13. **Finding:** every row said `emisor sin leer`, see below.                                    |
| Rehearsal clean-up                                             | Console invoices #1–#7 and #11 cancelled with `rehearse send --text "/cancelar <id>"`.                                                                                       |

## Findings

- **Fixed on the branch:** `/estado` and `/pendientes` read the issuer from `invoices.razon_social`, which only the
  importer fills; extraction keeps the padrón name in the reading. Both list queries now fall back to the padrón row
  for `ruc_emisor`. Re-checked live on `da107a086e03`: `#19 · PEBSA COMBUSTIBLES S.A. · 160.41 · cancelado`.
- **Old label in a replayed notice:** #15's summary said `Mi empresa (Google)` (the example label) because it was
  written before the live config's label changed at 22:11; [Ver] replays the notice as sent. Not a bug.

## The owner's notes (go to 9s `guided-docs` and 9t `portal-probe`, D189)

- [Corregir]/[Anular] under a summary read fully from the QR look like the bot is waiting; show them only when a
  required field came from the printed text.
- [Corregir] opens a free-text prompt for any field; ask which field with buttons, show its current value and format,
  validate, and re-ask that one field.
- `⏳ Comprobante #19: esperando correo del comercio. Lo vuelvo a buscar más tarde.` then silence: say what the flow
  still needs, where the bot looked, when it looks again, and offer [📎 Te envío el PDF] [🔗 Enseñar el portal]
  [⏳ Espero el correo]. For #17 the fetch attempts were `qr_url: no_url`, `portal_registry: no_portal`,
  `email_inbox: not_found_yet`.
- Ask which address the merchant sends to; skip the email wait when it never comes.
- Offer the URL printed on the ticket (`https://sy.ecomprobantes.pe/pebsa`) as the portal; with none printed, ask
  whether the sender knows it; "no sé" never blocks.
- Learn per issuer (QR link, portal, email arrival) so the next invoice from the same issuer skips what failed, and
  put that evidence in the gap's issue; after the owner's [Aprobar], probe the taught link and draft the portal file.
- Tapping a command in Telegram's menu sends it at once, so `/cancelar` arrives without an id; a bare `/cancelar` or
  `/reprocesar` should list the sender's invoices as buttons.
- `Último aviso (BR-R01)` shows an internal rule id to the user.
- `Para ver uno: /estado 12` uses a fixed example id; the newest listed id reads better.
