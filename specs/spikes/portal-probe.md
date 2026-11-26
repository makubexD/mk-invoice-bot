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

## Live check (2026-11-25, image `ac526779d9cd`)

| Row                                   | Result                                                                                                                                                      |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| PEBSA link taught, session id cleaned | pass: `BR-S06_approved_probe`, then `captcha`, every field mapped (image `d68b2f4261dc`)                                                                    |
| La Leña ticket gives a [🔗]           | waived: no link read (anchor "ingresando a", address over two lines): 9t2                                                                                   |
| La Leña probe, unnamed submit         | pass: `unmatched_field` stopped at «Tipo comprobante», draft has `submit: button[type='submit']`, `captcha: none`, all five fields mapped to invoice values |
| La Leña configured portal files #34   | pass: `portal_registry` `facturactiva-la-lena` ok in 6.5 s, delivered by Graph, #34 `BATCHED` in place of cancelled #12                                     |
| Test invoice #32 cancelled            | pass: `/cancelar 32`                                                                                                                                        |

Findings for 9t2:

- «Tipo comprobante» is an Ant Design list: the probe reads it as a text field with no options. The site's options are
  Factura, Boleta de venta, Nota de crédito, Nota de débito, Comprobante de retención, Guía de remisión - Remitente and
  Guía de remisión - Transportista. The value must be picked by label from the invoice's SUNAT tipo (01 Factura, 03
  Boleta de venta), as the hand-written `facturactiva-la-lena` portal already does with `tipo_labels`.
- A portal added while an invoice waits on email is never tried: the chain does not go back to a fetcher that ended
  (`no_portal`), the config re-check resumes only `MANUAL_REQUIRED` invoices, and `/reprocesar` refuses
  `FETCHING_DOCS`. The live check cleared the attempt by hand. A user needs a way out from the chat.
- A press on a decided or removed proposal answers nothing (D192 keeps it silent); a user reads it as broken.
