# Spike S0.3 · OCR accuracy and preprocessing

Run on 2026-11-13 with Tesseract 5 (`-l spa --oem 1`) from the host's `tesseract-ocr-spa`. The script was throwaway and stayed outside the repo, and the images stayed in the owner's private folder. Only aggregates are recorded here: no RUC, store name or amount.

## Method

- **Images:** the 6 WhatsApp photos, and the 50 distorted renders of the S0.2 interim set, rebuilt the same way: 10 PDFs at 150 dpi in grayscale, five distortions, 1600 px on the long side, JPEG quality 75. Each image is loaded with `load_gray` (≤ 2000 px).
- **Truth:**
  - Renders: the invoice's XML through `parse_invoice_xml`. The 2 XMLs that S0.2 finding 4 rejects were read after removing the `xmlns:schemaLocation` attribute.
  - Photos: the decoded QR through `read_invoice_qr`, for 3 photos, plus 1 more after the payload repairs that S0.2 finding 3 describes. **The 2 photos whose QR does not decode have no truth and are left out, so the photo numbers cover 4 invoices (24 required fields).**
  - Neither reader gives `icbper`, so it is not measured. `base` and `service_charge` come only from the XML, so they are measured on the renders only.
- **Parser:** the keyword-anchored regexes of spec 03 §5 step 4, with the issuer as the first valid-check-digit RUC. `tipo` comes from `FACTURA`/`BOLETA ELECTRÓNICA`, or else from the serie letter. Amounts are compared as decimals, `numero` as an integer, and the date as ISO.
- **Variants:**
  - `none`: grayscale only.
  - `clahe_thr`: CLAHE (clip 2, 8×8 tiles), then an adaptive Gaussian threshold (block 31, C 15).
  - `+deskew`: the median angle of Hough lines through the dilated text, applied before CLAHE.
  - `+warp`: a 4-point perspective warp onto the largest quadrilateral covering at least 25 % of the image, applied first.
- **Time:** preprocessing plus the Tesseract call, per image, with 6 images in parallel on an 8-core laptop (`OMP_THREAD_LIMIT=1`). On a 2 GB host, expect it to be slower.

## Results, PSM 4

The first run's regexes read the serie's digits literally. Tesseract reads the digit `0` as the letter `O` in 3 of the 4 photo series (`FOO1`), so every preprocessed variant scored `serie` 0 / 4. The table below maps `O Q D → 0` and `I L → 1` in the serie's last three characters. The finding below explains it.

| Variant | Photos: required | Photos: all six | Renders: required | Median s | Max s |
|---|---|---|---|---|---|
| `none` | 16 / 24 (67 %) | 0 / 4 | 41 % | 1.6 | 2.4 |
| `clahe_thr` | 17 / 24 (71 %) | 0 / 4 | 34 % | 2.0 | 2.4 |
| **`clahe_thr` + deskew** | **19 / 24 (79 %)** | **1 / 4** | **49 %** | 5.2 | 7.2 |
| `clahe_thr` + deskew + warp | 19 / 24 (79 %) | 1 / 4 | 41 % | 3.7 | 5.7 |

Times are the photo set's. The renders took a median of 0.8 to 2.7 s, with a maximum of 4.3 s. Run to run, the medians move by about 1 s with the parallel load, so read them as rough.

**Per field**, for the chosen variant:

| Field | Photos | Renders |
|---|---|---|
| `ruc_emisor` | 3 / 4 | 21 / 50 |
| `tipo` | 4 / 4 | 39 / 50 |
| `serie` | 3 / 4 | 26 / 50 |
| `numero` | 3 / 4 | 22 / 50 |
| `fecha_emision` | 2 / 4 | 21 / 50 |
| `total` | 4 / 4 | 18 / 50 |
| `base` | – | 9 / 50 |
| `service_charge` | – | 1 / 35 |

**Renders by distortion** (required fields, chosen variant against `none`):

| Distortion | `clahe_thr` + deskew | `none` |
|---|---|---|
| downscale only | 73 % | 92 % |
| 5–15° tilt | 63 % | 13 % |
| uneven light | 68 % | 60 % |
| Gaussian blur | 18 % | 38 % |
| tilt + light + blur | 22 % | 0 % |

## PSM 6

PSM 6 is worse on the photos for every variant: 42 to 62 %, against 67 to 79 % with PSM 4. On the renders it is within 2 points of PSM 4. It is not used.

## Choice

**Preprocessing: deskew, then CLAHE and the adaptive Gaussian threshold. Tesseract `--psm 4 --oem 1 -l spa`.** This is the pipeline for plan step 5.

- **Gate:** 79 % of required fields on the real photos (19 / 24), above the 60 % stop. It is just under R-2.9's 80 %, and on only 4 invoices.
- **No perspective warp.** On the photos the quadrilateral search never fired (0 / 4), because the receipts fill the frame or have no clean edge. On the renders it fired on 7 of 50, always on an inner box, and cut the required-field rate from 49 % to 41 %. Spec 03 §5 step 4 lists the warp first; plan step 2 drops it there.
- **Deskew is the gain:** it lifts the tilted renders to 63 %, from 13 % with `none`. On the other distortions it changes little. It costs about 2 s per image, mostly from the Hough transform on the full image.

## Findings

1. **The serie needs a confusable repair.** Tesseract reads `0` as `O` in the serie's digits. Mapping `O Q D → 0` and `I L → 1` in its last three characters lifted `serie` from 0 / 4 to 3 / 4 on the photos and from 14 to 26 of 50 on the renders. The OCR rule parser (plan step 6) needs this repair.
2. **The 80 % target is not met with confidence.** 79 % on 4 photos is one field short, and no photo but one gets all six required fields right. The rest of the plan (OCR merged with the QR, D81) is what reaches the required fields, not OCR alone. Step 11 measures that on the private set.
3. **Weak fields are the date and the RUC.** On the photos the date is right on 2 of 4, and the RUC on 3 of 4. Both are digit strings that a confusable `O`/`0` or a lost digit breaks. On the renders, the low `total` (18 / 50) and `base` (9 / 50) come from small text at 1600 px. The `downscale only` renders, with no other distortion, reach 73 to 92 %.
4. **Blur defeats the binarization.** Thresholding a blurred image loses strokes. On the blurred renders, `none` beats every preprocessed variant (38 % against 18 %). Real photos were not blurred enough to show it.
5. **Two photos have no ground truth** (their QR does not decode, see S0.2). The accuracy run in step 11 needs their values from the owner, or other photos with an XML.
6. **Time:** the chosen variant takes about 5 s of the 60 s summary budget (R-9.2) on this machine. It was not measured on the 2 GB host.
