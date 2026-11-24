# Spike: message-review live run (9u, D190)

Date: 2026-11-24 (morning, Lima). Host: the local live stack `invoicebot-live` (D141). Image `4a303372413c`.
Channel: the owner's Telegram. Ticket: the PEBSA ticket (QR), sent as #24.

## What each item showed

| Item | Result |
|---|---|
| PEBSA ticket → BR-F13 names the next and the last look | As specified: `Próxima revisión: hoy a las 06:43. Si no llega hasta mañana a las 13:43, te aviso para que me lo envíes.`, with [📎 Te envío el PDF] [🔗 Enseñar el portal] [⏳ Espero el correo]. |
| [🔗] → BR-F02 with [No sé] | As specified: `No conozco el portal de descarga de PEBSA COMBUSTIBLES S.A. (RUC …)`, one [No sé] button. BR-F14 was not offered: OCR still loses the printed URL (see guided-docs). |
| [No sé] → the fetching text | As specified: `Entendido. Sigo buscando el PDF en tu correo y te aviso cuando llegue.` |
| `/cancelar 999999` → BR-S07 with [📋 Mis comprobantes] | As specified: `No encontré el comprobante #999999.` with the button; the button listed #24–#20 with issuer, amount and state, then `Para ver uno: /estado 24`. |

## The owner's notes

1. **The summary contradicts BR-F13.** The summary ends `⏳ Lo estoy archivando.` and the next message says
   `me falta el PDF para archivarlo`. The summary must not claim filing while a document is missing.
2. **BR-F13 does not read well.** `Portal del comercio: no lo conozco` while the portal is printed on the ticket;
   `Próxima revisión: hoy a las 06:43` is five minutes away and means nothing to the sender;
   `Si no llega hasta mañana a las 13:43` is awkward; `Si lo tienes, me ayudas:` is clumsy; [⏳ Espero el correo]
   adds nothing, since waiting is what the bot does anyway.
3. **No portal guess.** The owner expected the bot to propose the URL printed on the ticket. BR-F14 exists but needs
   a clean `printed_url`; repairing the OCR result is 9v `portal-guess`.
4. **Guided input, not free text.** BR-F02 invites a typed answer, yet only one `https` URL is taken (and goes to
   the owner through BR-S06); anything else falls through to other paths without a word. The sender is never told
   what is accepted. The owner wants guided answers: buttons for each guess, typed text only after [✏️ Corregir],
   the bot echoing what it understood for a [Sí] [Corregir] confirmation, and a clear retry on text it cannot read.
   This goes to 9v with BR-F14.
