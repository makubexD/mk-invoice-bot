# Spike: store lookup sites (2026-11-12)

Each site was opened in an automated browser. A real lookup was made on La Leña's and Shimaya's sites; the captcha box was tried on KFC's and Popeyes'; the rest were only read. Counts are the owner's 35 work invoices of 2026.

| Site (`config/portals/`) | Stores | Invoices | Form asks for | Captcha | Result |
|---|---|---|---|---|---|
| `facturactiva-la-lena` | La Leña | 15 | tipo, serie, correlativo, fecha, monto total | none | **Works.** A real lookup returned buttons for PDF, XML and CDR |
| `apisunat` | PizzaDePieri | 1 | RUC emisor, tipo, serie, número, buyer's document type and number | none | Looks automatable; not submitted |
| `infofact` | Huancahuasi | 1 | RUC emisor, tipo, número de serie, importe total | none | Looks automatable; not submitted |
| `lalucha-ddns` | La Lucha | 1 | emisor, serie, correlativo, tipo, fecha, monto, captcha text | image | Solvable kind (local OCR, then ask the sender). The site is plain `http`, allowed for this host only (D76) |
| `paperless-kfc`, `paperless-popeyes` | KFC, Popeyes | 3 | tipo, folio (serie-correlativo), fecha `DD-MM-AAAA`, monto total | reCAPTCHA checkbox | **Not reliable.** Three tries: the box passed once and was challenged twice; the one result page could not be read |
| `pse-idbiperu` (and the same platform at `practifact.pse.pe`) | Shimaya, BesoFrances | 2 | RUC emisor, tipo, serie, número, total | invisible reCAPTCHA | **Blocked.** A real lookup got an image challenge |
| `miwally` | ElCedroFit, CrepeBike | 5 | RUC emisor, tipo, serie, correlativo, fecha de emisión, importe total (read from its code); offers PDF, XML, CDR | reCAPTCHA Enterprise | **Unresolved.** The page stayed blank in the automated browser, so it is unknown whether a lookup passes |
| none known | PanYFuego, Intiwarma, Umari | 6 | — | — | The owner will provide the page; until then `PORTAL_MISSING` |
| no website | El Enano | 1 | — | — | `portal: manual`: files are asked from the store |

**Reading:** 15 invoices (43%) are confirmed automatable, 2 more look automatable, 1 needs a captcha answer, 5 are behind a reCAPTCHA that can't be relied on, 5 are unresolved, 6 have no known page and 1 has no site. For the reCAPTCHA sites the paths are the email fallback (Q5) or the sender sending the PDF and XML (R-1.5).

BesoFrances has no store file yet: its 2026 folder held another store's XML, so its legal name is unverified.
