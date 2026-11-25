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
