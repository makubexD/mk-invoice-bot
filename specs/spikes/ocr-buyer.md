# Spike: ocr-buyer live re-read (9z6, D199, D200)

Date: 2026-11-26 (UTC 2026-11-27). Host: the owner's laptop, live stack, Telegram. Image `f2cff8a`.

## The receipt

A restaurant receipt made out to the employer (flow B), photographed on a wooden table, sent as a Telegram photo
(1080 × 1920, re-encoded by Telegram). The printed QR does not decode on Telegram's copy, even upscaled ×3, so every
field comes from OCR.

## Runs

| Invoice | Image          | Result                                                                                                                                                                                                                              |
| ------- | -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| #40     | before 9z6     | Roles swapped (employer read as issuer); BR-R03 flow question; store portal lookup on the employer. Cancelled, key released.                                                                                                        |
| #41     | `cee651b` D199 | Roles right (store issuer, employer buyer), but BR-E01 asked for the date: the first read missed only the date, the block-mode retry read it but missed serie and número, and the tie kept the first read. Cancelled.               |
| #42     | `f2cff8a` D200 | Read in full; BR-E04 duplicate of #39, the same receipt filed earlier that day. #39's OneDrive folder deleted and both keys released (owner's choice).                                                                              |
| #43     | `f2cff8a` D200 | Read in full: date 27/10/2026, store as issuer, employer as buyer, total S/ 49.90. Routed to flow B with no question (BR-R02), portal fetch `facturactiva-la-lena` ok in 8.3 s, filed, mail held for review (`send: review`, D194). |

Before the code, the current helper was run on #40's and #41's stored photos in a throwaway container: both read every
required field, the date `2026-10-27`, and after D199 the store as issuer and the employer as buyer.

## What #43 still shows

- `serie` and `numero` came from OCR at confidence 0.43, under `review_threshold` 0.8, so the summary said "Revisa:
  serie, número". The portal's XML matched the invoice (RUC, serie, número and total), but the reading kept the OCR
  values and their low confidence.
- The mail waited for [Enviar] because the live config sets `send: review` (D194, trial setting), not because a field
  was missing.

## Webhook route

Twice in the evening Telegram held updates (`pending` above 0, no `last_error`) while the app was healthy. The
Tailscale Funnel's public addresses hung from outside (a phone on mobile data, and from the host through the public
addresses), even after `tailscale funnel reset` and a `tailscaled` restart; `tailscaled` logged
`invalid-packet-filter … rejecting all packets for safety` on each netmap. From the host, MagicDNS resolves the
funnel name to the tailnet address, so a check from the host does not see the public route. `telegram webhook --url`
sends `drop_pending_updates: true`, which lost three of the owner's updates. The live bot moved to a Cloudflare quick
tunnel for the rest of the run. Follow-up: 9z7 `webhook-watch` (D201).
