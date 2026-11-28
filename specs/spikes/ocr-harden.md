# Spike: ocr-harden golden set (9t6, D212)

Date: 2026-11-27. Host: the owner's laptop. Branch `feat/ocr-harden` at `74c2932`, before any D212 code.

## The set

Seven photos of paper receipts, all billed to an employer (flow B), each with a printed QR. Each one's expected
reading (D127) was written by reading the photo. Photos and expected files stay in the session scratchpad, never in
the repo.

- The owner's 6 WhatsApp photos from the private samples folder: three issuers, daylight, no shadow.
- Live #58's Telegram photo (1080 × 1920): a shadow across the ticket and glare on the QR's centre.
- Live #56's photo was gone: its `/data/tmp` job directories held no file by the time this ran, so it is not in the
  set.

Expected fields per photo: `ruc_emisor`, `tipo`, `serie`, `numero`, `fecha_emision`, `total`, `igv`, `base`,
`num_doc_adq`.

## #58, pass by pass

Probing the live helper on #58's photo (from the plan, aggregates only):

- **QR**: not decoded. Glare covers its centre; CLAHE, adaptive threshold, shadow division and 2x upscaling, on the
  full photo and on a crop, decode nothing.
- **RUC emisor**: every pass reads the keyword-anchored RUC with one or two confusable characters, so `\d{11}` never
  matches. One pass's text, mapped `U/O/D/Q→0`, `I/L/|→1`, `/→7`, gives the printed RUC, which passes the check digit
  and is the only candidate in the live padrón.
- **Número**: the best pass (fewest missing fields) read it at 0.18 and wrong; another pass read it at 0.61 and right.
  D200 keeps the best pass whole, so the better value was dropped.
- **Fecha**: the first pass read it right but with spaces after the separators, which the date pattern refuses; the
  other passes misread one year digit at confidence 0.0, and that value was kept.
- **Buyer RUC**: misread by a digit swap no character map fixes (out of scope; routing asks per BR-R04).

## Results

`uv run python scripts/accuracy_report.py --set <scratch dir>` with the live extraction settings (`max_image_px: 2000`,
`psm: 4`, `timeout_s: 30`; `config/example.yaml` holds the same values).

| Run    | Pairs | QR rate     | OCR-only rate | ruc_emisor | tipo | serie | numero | fecha_emision | total | igv | base | num_doc_adq |
| ------ | ----: | ----------- | ------------- | ---------: | ---: | ----: | -----: | ------------: | ----: | --: | ---: | ----------: |
| before |     7 | 71.4% (5/7) | n/a           |        6/7 |  7/7 |   7/7 |    6/7 |           6/7 |   7/7 | 5/7 |  0/7 |         6/7 |

- **OCR-only rate is n/a**: every member has a printed QR, so the script's OCR-only rate (members without a QR) has
  nothing to count and prints `0.0%`. #58, whose QR does not decode, is scored under the QR rate.
- **#58 alone**: right on `tipo`, `serie` and `total`; wrong or missing on `ruc_emisor`, `numero`, `fecha_emision`,
  `igv`, `base` and `num_doc_adq`. Apart from `base`, every other miss in the row but one `igv` is #58's.
- **`base` is 0/7**: no reading of the set gives the taxable base the receipt prints, the 6 clean photos included. It
  is not a required field and is outside this plan; noted for a later look.
