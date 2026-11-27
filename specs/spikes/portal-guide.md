# Spike: PEBSA captcha save and replay (9t4, step 1)

Date: 2026-11-27. Plan: `docs/plan-mvp-portal-guide.md`. Headless Playwright from the project's environment (script in
the session scratchpad, not the repo) and the owner's Chrome, with the PEBSA ticket from live #52. No invoice values,
RUCs or images are recorded here.

## Findings

- **The working form is `frmAutentificacionEstandar.aspx`.** The menu's `frmAutentificacionEstandarConvivencia.aspx`
  (the URL in the live portal file) answers every search with the bare form and a new captcha, with no message, in
  headless Chromium and in Chrome alike. The portal file's `url` must change.
- The session is cookieless (id in the URL path, `/(S(...))/`); the captcha is MSCaptcha, `CaptchaImage.axd?guid=`.
  The saved form is 14 fields (`__VIEWSTATE` and `__EVENTVALIDATION` included), about 2.8 KB of JSON; the captcha PNG
  is about 21–23 KB.
- Markers: a wrong captcha shows `Texto no coincide con la imagen.`; an accepted captcha with no match shows
  `No hay registros` in a table cell. Both pages show the captcha input again with a new image.
- `hdfFecha` is filled by the server only; `txtFecha` takes `yyyy-mm-dd` (`dd/mm/yyyy` also gives `No hay registros`).

| Row                                       | Result                                                                                      |
| ----------------------------------------- | ------------------------------------------------------------------------------------------- |
| (a) replay in a new browser after 12 s    | pass: `No hay registros` (captcha accepted)                                                 |
| (c) wrong answer through replay           | pass: `Texto no coincide con la imagen.`                                                    |
| (b) replay after 60, 120 and 180 s        | pass: all three `No hay registros` (captcha accepted)                                       |
| (b) replay after 240 s and after 290 s    | fail: both `Texto no coincide`; the captcha lives between 180 s and 240 s                   |
| (d) the live #52 ticket                   | `No hay registros`; the values match the printed ticket and its QR, so the portal lacks it  |
| (d) result page, downloads, value formats | deferred to the live check: no invoice the portal knows, and replays need the owner (below) |
| Live #52 QR                               | zxing reads the ticket's QR on the full photo, yet the reading came from OCR at 0.43: a bug |

## Left for the live check

- The result page of an invoice the portal knows: its download buttons (PDF, XML), their selectors and whether a
  download is a postback. Step 2 must treat a page with neither marker as unknown and say so, not guess.
- The QR on the ticket is the SUNAT payload (no document link), so the portal stays the only route to the PDF.
- The captcha lives between 180 s and 240 s. `ask_user_timeout_s` must sit under 180 s (with margin for the replay
  itself), so the 300 s default of D209 does not hold for PEBSA; step 2 sets it lower.
- Two of three forms opened in parallel showed the same captcha text; each answer is still checked against its own
  session.
- The auto-mode classifier refuses the replay POST from the assistant's shell; the owner ran the timed replays.
