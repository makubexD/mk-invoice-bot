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
| after  |     7 | 71.4% (5/7) | n/a           |        7/7 |  7/7 |   7/7 |    6/7 |           7/7 |   7/7 | 5/7 |  1/7 |         6/7 |

- **OCR-only rate is n/a**: every member has a printed QR, so the script's OCR-only rate (members without a QR) has
  nothing to count and prints `0.0%`. #58, whose QR does not decode, is scored under the QR rate.
- **#58 alone**: right on `tipo`, `serie` and `total`; wrong or missing on `ruc_emisor`, `numero`, `fecha_emision`,
  `igv`, `base` and `num_doc_adq`. Apart from `base`, every other miss in the row but one `igv` is #58's.
- **`base` is 0/7 before**: no reading of the set gives the taxable base the receipt prints, the 6 clean photos included. It
  is not a required field and is outside this plan; noted for a later look.

The after run is the branch at step 7 (`D212` items 1 to 5 in). No per-field rate dropped. #58 now reads `ruc_emisor`
(repaired, flagged `inferred`), `fecha_emision` and `base` right; `numero`, `igv` and `num_doc_adq` are still wrong.
The 6 clean photos read the same as before.

## #58 after, pass by pass

The helper probe re-run on #58's photo, each pass alone (R right, W wrong, - not read; confidence; `i` inferred):

| Pass                   | ruc_emisor | tipo     | serie  | numero | fecha_emision | total  |
| ---------------------- | ---------- | -------- | ------ | ------ | ------------- | ------ |
| first (binarized)      | -          | R 0.15   | -      | -      | R 0.72        | R 0.92 |
| 1 deskewed             | -          | R 0.68   | R 0.31 | W 0.31 | -             | -      |
| 2 upscaled             | -          | R 0.18 i | R 0.18 | W 0.18 | W 0.00        | -      |
| 3 upscaled, block mode | R 0.69 i   | R 0.78   | R 0.43 | W 0.43 | W 0.00        | -      |
| 4 crop                 | -          | R 0.56   | R 0.61 | R 0.61 | -             | R 0.97 |
| 5 crop                 | -          | -        | -      | -      | W 0.00        | -      |
| helper keeps           | R 0.69 i   | R 0.78   | R 0.43 | W 0.43 | R 0.72        | R 0.92 |

- **Finding: the retries stop before the pass that reads the número right.** The retry loop runs only while a
  required field is missing (D198). Before D212 the RUC was never read, so every pass ran; now pass 3 repairs it,
  nothing is missing, and pass 4 (the only one with the right número, at 0.61) never runs. Best-of-passes keeps pass
  3's número at 0.43: wrong, but below `review_threshold`, so the invoice goes to `CONFIRMING` (BR-E09) rather than
  being filed silently. Live row (b) of step 9, "número right", would fail as the branch stands.

## Live check (step 9)

Live #59, 2026-11-27: the branch at `ebaeccf` deployed live, and the owner resent the La Choza Náutica Minka ticket on
Telegram (flow A). No QR decoded; OCR only.

| Row | Check                                            | Result                                                                         |
| --- | ------------------------------------------------ | ------------------------------------------------------------------------------ |
| a   | issuer RUC repaired, found in the padrón, named  | pass: `inferred`, confidence 0.69, razón social from the padrón                |
| b   | número and fecha right                           | fecha right (0.72); número wrong at 0.43, the step 8 finding                   |
| c   | a field below the threshold is shown as doubtful | not reached: the route asked BR-R03 first; moved to step 16                    |
| d   | routes to RK Export or asks BR-R04               | **fail**: asked BR-R03 ("boleta sin datos" on a factura); D213, steps 10 to 15 |
| e   | the fetch step and the printed consulta URL      | no printed URL read (`printed_url` empty), so nothing was fetched              |
| f   | flow B untouched                                 | pass: no flow B code changed on the branch, no flow B invoice since the deploy |

- **Why (d) failed**: every one of #59's six passes reads the buyer RUC 2 or 3 digits off, so it fails the check digit
  and no buyer is kept; with no buyer, routing is BR-R03, not BR-R04. Three passes read the buyer name close to the
  padrón's razón social for `owner.ruc`; two put the `CLIENTE:` label after other text on its line, which the
  line-anchored `buyer_label` misses. D213 (owner, 2026-11-27) infers the buyer only when a near RUC and the name
  agree.
