# Spike: the PEBSA portal form (9t)

Date: 2026-11-25. Read-only GETs with `curl`; no form was submitted and no invoice data was sent.

- The taught link redirects to `frmInicio.aspx`, with the session id in the path (`/(S(...))/`). A link copied from the
  browser carries an expired session, so a portal must store the base URL and reach the form from there.
- That page has two buttons: user and password, or "Autenticación Datos del Comprobante" (`btnEstandar`), whose menu
  link is `frmAutentificacionEstandarConvivencia.aspx`. A GET of that page in the new session answers the form.

| Form field (`ctl00$bodyContentPlace$`) | Invoice value                   |
| -------------------------------------- | ------------------------------- |
| `cboEmisor` (one option)               | fixed                           |
| `txtSerie`                             | serie                           |
| `txtCorrelativo`                       | número (zero padding not known) |
| `cboTipoDoc` (eight options)           | tipo, by label                  |
| `txtFecha` (date picker, `yy-mm-dd`)   | fecha                           |
| `txtMonto`                             | total (number format not known) |
| `txtCaptcha` + `CaptchaImage.axd`      | none: a person must read it     |
| `btnBuscar`                            | submit                          |

- An image captcha of 200×50 is the only field the bot cannot fill. Not known: the result page and its downloads.
- `lalucha-ddns` has the same fields and nearly the same page name (`frmAutentificacionEstandar.aspx`), so it is
  probably the same software. D99 deferred captchas while that was the only image-captcha site; this is the second.
- The last fallback, `sunat_sol`, has code (S10.1) but is in no chain: S0.4 has not shown that SOL gives a received
  invoice's files (D153).

## Owner decisions (2026-11-25)

- 9t runs next, ahead of 9w and 9x. On a captcha the bot sends the owner its image, the owner types the answer, and
  the session goes on, inside the site too, to find how its file is downloaded.
- What the first visit learns (landing steps, field mapping, captcha, result page, downloads) is registered in the
  site's settings and in the issue, so the next invoice of that site reuses it.
- SUNAT: no scraping of SUNAT pages. The owner has tokens for an official API; reviewing what it returns comes later.
