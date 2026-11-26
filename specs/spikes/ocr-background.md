# Spike · OCR on a receipt over a background (D198)

Run on 2026-11-26 for `docs/archive/plan-mvp-ocr-background.md`, step 16. The photos stayed outside the repo: the owner's 6
WhatsApp receipt photos plus the live photo of invoice #37 (a receipt on a wooden table, sent through Telegram at
1080 px). Each one went through the extract helper (`python -m invoicebot.extract`) in a throwaway container with no
network. Only aggregates are recorded here: no RUC, store name or amount.

## Private photos, before and after

The before image is `abfe468f2c9e` (main before this phase). The after image is `42b2820e0f51`, which has the full
retry cascade: plain, ×2 upscale, ×2 upscale in block mode (psm 6), binarized crop, and plain crop. It also has the
buyer-label guard and the wider RUC separators.

| | Before | After |
|---|---|---|
| Every required field read | 4 / 7 | 6 / 7 |
| Issuer RUC right | 5 / 7 | 7 / 7 |
| Issuer RUC wrong (the buyer's) | 1 / 7 | 0 / 7 |
| Photo #37 | nothing read | every field read |
| Run time, 7 photos | — | ~36 s |

- No photo whose issuer RUC was right before is wrong after, so the bar is met.
- The one photo still short is missing its date. An earlier reading had the date, but the reading kept is the one
  with the fewest missing fields, and that one lacks it. Merging fields across readings would recover it; that is
  left for a later phase.
- Without the step 10–11 fix, a retry replaced the QR's correct issuer with the buyer's RUC. Counting the QR's fields
  before a retry stopped that.

## Live

| Invoice | Image | Result |
|---|---|---|
| #37 | before this phase | `EXTRACT_FAILED` |
| #38 (same receipt) | `7b3c6a32d83b` (crop + buyer guard) | 4 / 6 fields; issuer and date missing |
| #39 (same receipt) | `42b2820e0f51` | every field read and right, from text (QR not decoded); serie and number flagged for review by their confidence |

The header print is too small for Tesseract at 1080 px. The ×2 upscale reads the issuer, and the block-mode read
recovers the date line that psm 4 drops.
