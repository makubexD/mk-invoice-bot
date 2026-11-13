# Spike S0.2 · QR hit rate

Run on 2026-11-13 against `read_invoice_qr` as of step 12 of `docs/plan-mvp-read.md` (zxing, then OpenCV's detector, over the image variants). The script was throwaway and stayed outside the repo, and the images stayed in the owner's private folder. Only aggregates are recorded here: no RUC, store name or amount.

A **hit** is a decoded code that `parse_qr_payload` accepts. Each hit was also compared with the invoice's own XML through `matches_invoice` when that XML could be read.

## Interim, from rendered PDFs (D77)

The 10 PDFs of the owner's 2026 sample folders, rendered at 150 dpi in grayscale, put through each distortion, then downscaled to 1600 px on the long side and saved as JPEG at quality 75 (WhatsApp size).

| Distortion | Hits |
|---|---|
| downscale only | 7 / 10 |
| 5–15° tilt | 7 / 10 |
| uneven light | 7 / 10 |
| Gaussian blur (7×7, σ 2) | 1 / 10 |
| tilt + light + blur | 0 / 10 |
| **All** | **22 / 50 (44 %)** |

- Decoder: zxing for all 22 hits; OpenCV added none. Variant: `original` 21, `upscaled_3x` 1.
- Median time per image 0.29 s, maximum 0.90 s.
- 19 of the 22 hits match their XML. The other 3 have no XML to compare with, because the parser rejected it (see "Findings").
- The 3 misses with downscale only:
  - one PDF carries no QR at all;
  - one QR puts a date and time in the date part (`YYYY-MM-DD HH:MM:SS`), which the payload parser rejects;
  - one QR is a 200 px embedded image on a narrow ticket page; it decodes only when rendered at 600 dpi.
- At this resolution the synthetic blur is harsher than any real photo below: it erases the modules. The blur rows measure the distortion more than the decoder.

## Real, WhatsApp photos

6 photos of paper receipts sent through WhatsApp (900 × 1600 px). That is short of the 10 that S0.2 asks for, so these numbers are provisional.

| | |
|---|---|
| Images | 6 |
| Hits | 3 (50 %) |
| Decoder | zxing 3, OpenCV 0 |
| Variant | `original` 2, `upscaled_2x` 1 |
| Median time per image | 0.31 s (maximum 1.36 s) |

The 3 misses:

- **Decoded, but the parser rejects the payload (1):** the text starts with a label before the RUC (`XXX - <ruc>|…`), the amounts use a decimal comma, and the buyer-type part is a word instead of a code. Each is a repair of the kind D79 already makes.
- **Not decoded (2):** a sharp, well-lit QR fills about a third of the receipt's width, yet zxing (with every binarizer, crop and upscale tried), OpenCV's detector and OpenCV's WeChat detector all return nothing. pyzbar was not tried, because it needs the system `libzbar` library.

## Findings

1. **Hit rate is 50 % on real photos, far below the 95 % that S3.3 asks for.** The threshold stays at 95 %; zxing alone does not ship.
2. **WeChat adds nothing here.** On the 6 photos it decoded the same codes as zxing and missed the same two. Adding it next, as the plan said, would not move the rate.
3. **The cheapest gains are in the payload parser, not the decoder:** accepting a date with a time, a label before the RUC, a decimal comma and a word buyer type turns one rendered-PDF miss and one photo miss into hits.
4. **2 of the 10 real XMLs fail `parse_invoice_xml` as `malformed`.** Both declare `xmlns:schemaLocation` with a relative Windows path as its value, and libxml2 rejects that namespace URI. The XML fallback (S7.7) needs to read these files.
5. **Two photos decode with nothing we tried.** Next tries: pyzbar with `libzbar`, perspective correction on the detected QR area, or a check with a phone's camera app that the printed code decodes at all.
