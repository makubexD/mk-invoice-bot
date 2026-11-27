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

| Row                                       | Result                                                                                          |
| ----------------------------------------- | ----------------------------------------------------------------------------------------------- |
| (a) replay in a new browser after 12 s    | pass: `No hay registros` (captcha accepted)                                                     |
| (c) wrong answer through replay           | pass: `Texto no coincide con la imagen.`                                                        |
| (b) replay after 240 s and after 290 s    | fail: both `Texto no coincide`; the captcha expires before 240 s                                |
| (d) the live #52 ticket                   | `No hay registros` for correlativo plain and 8-digit padded; the portal has no record of it yet |
| (d) result page, downloads, value formats | open: needs an invoice the portal knows                                                         |
| Live #52 OCR                              | bug: serie and número were read off the date line (0.43); the ticket's number differs           |

## Open before step 2

- The captcha lifetime between 12 s and 240 s (MSCaptcha's default is about 120 s): replay at 60, 120 and 180 s.
  `ask_user_timeout_s` must sit under it, so the 300 s default of D209 does not hold for PEBSA.
- A PEBSA invoice the portal knows, to learn the result page and its download buttons.
