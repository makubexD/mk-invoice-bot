# Spike: re-reading the printed portal URL (9v, D191)

Date: 2026-11-24. Inputs: the PEBSA ticket as Telegram delivered it to the live stack (598×1280, the copy left in the
live `/data/tmp`), a second Telegram photo (720×1280) and the six WhatsApp photos in the owner's private samples folder.
A throwaway script outside the repo used the repo's `load_gray` and `preprocess` and called `tesseract` directly. Only
aggregates are recorded here; no other ticket's URL.

## Findings

- **`eng` is in the live image.** `tesseract --list-langs` in `invoicebot-live-app-1` lists `eng`, `osd`, `spa`
  (`tesseract-ocr` brings `eng`).
- **The anchor reads `CONSULTADO EN;`.** The full-page pass (`spa`, `--psm 4`) read the anchor line as
  `ESTE COMPROSANTE PODRÁ SER CONSULTADO EN;`, so the anchor pattern accepts `[:;.,]` after `EN`. One of the eight
  photos prints such a line; the other seven have none (no strip re-read, no guess).
- **The line's union box is too tall.** One anchor word's box was 41 px high against a median of 21 px, so a band
  sized from the union box (2.5×) took three lines and read garbage (edit distance 27–33 from the printed URL). The
  band is sized from the median of the anchor words' bottoms and heights instead.
- **Gray beats binarized.** Cropped from the binarized page, every setting read garbage (≥ 27). Cropped from the gray
  image, 3× `INTER_CUBIC`, `eng`, `--psm 7`:

  | Band (offset, height, in word heights) | Mean confidence | Edits from the printed URL |
  | -------------------------------------- | --------------- | -------------------------- |
  | 0, 1.4                                 | 48.9            | 10                         |
  | 0, 1.6                                 | 24.0            | 9                          |
  | 0, 1.8                                 | 27.3            | 7                          |
  | 0.15, 1.4                              | 19.3            | 6                          |
  | 0.15, 1.6                              | 48.4            | 3                          |
  | 0.15, 1.8                              | 53.8            | 5                          |
  | 0.3, 1.4                               | 31.1            | 5                          |
  | 0.3, 1.6                               | 53.8            | 5                          |
  | 0.3, 1.8                               | 26.9            | 7                          |

  `spa` and 2× or 4× were no better (best 4). A single band is fragile, so the helper reads six bands (offsets 0,
  0.15, 0.3 × heights 1.6, 1.8) and keeps the most confident read. On the PEBSA photo that read is 5 edits away
  (`htins.//sy.ecomprobantes.pe/pehsa`); the repair rules of step 8 turn it into `https://sy.ecomprobantes.pe/pebsa`,
  the same as the best band. The host's `sy` for `sv` stays until an approved link teaches the host.

- **The crop uses the deskewed gray image**, since the word boxes come from the deskewed page.

## Chosen setting

Deskewed gray, full width, six bands under the anchor (offsets 0, 0.15, 0.3 and heights 1.6, 1.8 word heights,
from the median of the anchor words), 3× `INTER_CUBIC`, `eng`, `--psm 7`, the read with the highest mean word
confidence. Plan steps 3, 4 and 6 were edited to match.

## Live check

Date: 2026-11-25 (12:57 AM, Lima). Host: `invoicebot-live`, image `c5cc7e9f0d85`. Channel: the owner's Telegram.

| Check                                               | Result                                                       |
| --------------------------------------------------- | ------------------------------------------------------------ |
| Ticket photo, BR-F13, [🔗]                          | pass: BR-F14 with the repaired URL and three buttons         |
| [✏️ Corregir]                                       | waived by the owner (2026-11-25); integration tests cover it |
| A typed non-URL gives `BR-F15_retry`                | waived by the owner (2026-11-25); integration tests cover it |
| The typed address is echoed as BR-F14               | waived by the owner (2026-11-25); integration tests cover it |
| [✅ Sí, es este] gives BR-S06 and the owner's alert | pass, on the first offer                                     |
| The owner's decision                                | [Aprobar]; the confirmation arrived                          |
| Cancel the test invoice                             | pass: #26 cancelled                                          |

Correction: the printed host is `sy`, not `sv` as the findings above assume. On 2026-11-25 `sy.ecomprobantes.pe/pebsa`
answered 200 and `sv.ecomprobantes.pe` did not resolve, so the repaired URL was exact and the findings' edit
distances count one edit too many for reads that had `sy`.

## Live check 9t1

Date: 2026-11-25 (9:28 to 11:18 AM, Lima). Host: `invoicebot-live`. Channel: the owner's Telegram. PEBSA ticket, strip
read as `htins.//sy.ecomprobantes.pe/pehsa`. The link approved in the 9v check was removed first, since an issuer with a
taught link gets no [🔗].

| Check                                         | Image          | Result                                                                             |
| --------------------------------------------- | -------------- | ---------------------------------------------------------------------------------- |
| [🔗] and the "Reviso la dirección…" notice    | `49f22dec743a` | pass                                                                               |
| BR-F14 with the address that answers          | `49f22dec743a` | fail: `BR-F14_unverified` after 5.2 s, with the right URL; no reason was logged    |
| Same, with 8 s and 5 redirects                | `2852ea97f9d4` | fail: `BR-F14_unverified` after 6.4 s, with the right URL; no reason was logged    |
| Same, with busy told from no answer (step 14) | `8c14b1f9b3f5` | pass: `BR-F14_busy` with the right URL after 3.8 s; logged `busy`, `["ReadError"]` |
| [✏️ Corregir], a typed URL echoed as BR-F14   | all three      | pass                                                                               |
| [Sí, es este], then [Aprobar]                 | all three      | pass; the link is stored and nothing else happens (9t3)                            |
| A URL typed straight over BR-F14              | none           | waived by the owner; integration tests cover it                                    |
| `total=…` while BR-F15 is open                | none           | waived by the owner; integration tests cover it                                    |
| A second link replacing the waiting one       | none           | waived by the owner; integration tests cover it                                    |
| Cancel the test invoices                      | all three      | pass: #27 to #31 cancelled                                                         |

"The address that answers" was never seen in the chat: in the live container the same check returned the URL in 2.9 s and
2.7 s on the first image, and failed in two other tries.

Findings:

- `sy.ecomprobantes.pe` is unstable: 19 of 30 spaced requests with curl (HTTP/2 and HTTP/1.1) got 503, and a traced
  request got `ReadError`. When it works it takes 3 redirects, once 4, and 2.7 to 5.1 s without connection reuse. The
  measurements ran after the two failures and may have added load.
- The sender copied an address with an ASP.NET session id, `(S(…))`, and it was approved. An approved link goes into the
  gap's public issue when the gap exists (none did). 9t3 must not publish or reuse a session address, and "va en el
  issue" is not true when there is no gap.
- `BR-F14_busy` read "S.A..": reworded after the run, not seen live.
