# 06 · Business rules, user messages and capability gaps

Every time the bot stops, warns or changes route, it does so because of **one named rule**. Each rule has:
- an ID (`BR-…`);
- a condition;
- the resulting state;
- the exact chat message (es-PE). Messages live in code as a catalog: `rules/catalog.py` maps each rule ID to its state and message key, and `rules/messages_es.py` holds the texts. Both are keyed by rule ID, so they are testable and easy to reword.

When the root cause is something **missing from the repository** (a portal, a parsing rule, a template mapping), the rule also raises a **capability gap** (§3). In the MVP the gap opens an issue in the private ops repo and alerts the owner in chat (D44). A PR from a template comes as the last v1 step (S16.2, D52); red-CI watches are post-MVP.

Placeholders like `{razon_social}` are filled from the invoice. Messages stay short: one idea, one action.

---

## 1. Routing rules (which flow handles the invoice)
Rules are evaluated **in order; the first match wins** (R-3.1). The buyer is `num_doc_adq` from the QR or OCR.

| ID | Condition | Result | Chat message |
|---|---|---|---|
| BR-R01 | Buyer RUC = `owner.ruc` | Flow A (`personal_google`), silently | *(shown inside the summary)* `➡️ {flow.label}` |
| BR-R02 | Buyer RUC = `work.ruc` | Flow B (`work_microsoft`), silently | *(inside the summary)* `➡️ {flow.label}` |
| BR-R03 | Buyer is missing, is a DNI, or is a RUC that matches no rule, and `routing.unmatched: ask` | `CONFIRMING`; ask with one button per candidate flow (its `label`) plus **Cancelar** (WhatsApp's maximum of 3; a third flow needs a list message) | `Este comprobante está a nombre de {buyer_desc}. ¿En qué flujo lo registro?` |
| BR-R04 | The buyer was read by OCR with confidence below `review_threshold` | `CONFIRMING`, with the detected flow pre-selected | `No pude leer bien el RUC del cliente ({num_doc_adq}?). Lo registro en {flow.label}. ¿Correcto?` [Sí] [Cambiar] [Cancelar] |
| BR-R05 | The routed flow isn't allowed for the sender's profile | `CANCELLED` | `No tienes permiso para registrar comprobantes de {flow.label}. Pídele al administrador que te habilite.` |
| BR-R06 | The same unknown buyer RUC is seen 3 or more times in 30 days | Raises gap `UNROUTED_BUYER`, then continues as BR-R03 | *(no extra chat message; the admin is alerted)* |
| BR-R07 | No rule matches and `routing.unmatched: reject` | `CANCELLED` | `Este comprobante está a nombre de {buyer_desc}, que no corresponde a ninguno de tus flujos, así que no lo proceso.` |
| BR-R08 | The routed flow doesn't accept the invoice's `tipo` (`accepted_tipos`, D53) | `CANCELLED` | `Este comprobante es {tipo_desc}, y {flow.label} solo acepta {accepted_desc}. No lo proceso.` |
| BR-R09 | The store has several brands and no serie prefix matches (R-7.1) | `CONFIRMING`; one button per brand, or a list message above 3 brands | `¿De qué local es esta factura de {razon_social}?` |

`{buyer_desc}`: `el RUC 20… (Razón social)`, `el DNI 4…`, or `ningún cliente (boleta sin datos)`.

## 2. Pipeline rules

### Extraction (E)
| ID | Condition | Result | Chat message |
|---|---|---|---|
| BR-E01 | No QR, and OCR is missing required fields | `EXTRACT_FAILED` (waits for the user) | `No pude leer este comprobante. Envíame una foto más cerca y con buena luz, o escribe los datos así:` + `ruc=… serie=F001 numero=123 fecha=dd/mm/aaaa total=…` |
| BR-E02 | The issuer RUC fails the check digit | Same as E01, with the field highlighted | `El RUC {ruc} no es válido (dígito verificador). Corrígelo con ruc=…` |
| BR-E03 | The date is in the future or older than `max_invoice_age_days` | `CONFIRMING` | `La fecha {fecha} parece incorrecta. ¿La corrijo? fecha=dd/mm/aaaa` |
| BR-E04 | The natural key already exists and isn't failed | Stop | `Este comprobante ya lo registré el {created_at:%d/%m} (#{id}). Si quieres repetirlo: /reprocesar {id}` |
| BR-E05 | Not a supported CPE (`tipo` ∉ config) | `CANCELLED` | `Solo proceso facturas y boletas electrónicas. Esto parece {tipo_desc}.` |
| BR-E06 | OCR fails 2 or more times on photos from the same issuer | Raises gap `PARSER_MISS` | *(no extra message; E01 already asked the user)* |
| BR-E07 | The file is too large, the image exceeds 40 megapixels, the type isn't supported, or a zip breaks the entry/ratio limits (R-1.11) | Not processed (`CANCELLED` if an invoice was created) | `No puedo procesar este archivo ({reason_desc}). Envíame una foto (JPG o PNG) o el PDF del comprobante, de hasta {max_mb} MB.` |
| BR-E08 | Deterministic extraction failed, and the LLM fallback reached its monthly cap | `EXTRACT_FAILED` (as E01); admin alert once per month | `No pude leer este comprobante. Envíame una foto más cerca y con buena luz, o escribe los datos así:` + `ruc=… serie=F001 numero=123 fecha=dd/mm/aaaa total=…` Admin: `🤖 Se alcanzó el tope mensual de lectura con IA ({cap}). Se reactiva el {date}.` |
| BR-E09 | A required field is `needs_review` (low confidence, or a failed R-2.4 check) | `CONFIRMING` | `Revisa estos datos antes de seguir: {fields_desc}. Si están bien, confírmalos; si no, corrígelos así: campo=valor.` [Confirmar] [Cancelar] |

### SUNAT validation (V)
| ID | Condition | Result | Chat message |
|---|---|---|---|
| BR-V01 | `estadoCp = 0` (doesn't exist yet) | Continue; re-check in 24 h, max 3 times | *(one line in the summary)* `⏳ SUNAT aún no lo registra; vuelvo a revisar mañana.` |
| BR-V02 | `estadoCp = 2` (annulled) | `CONFIRM_INVALID` | `⚠️ SUNAT dice que este comprobante está ANULADO. ¿Lo registro igual?` [Sí] [Cancelar] |
| BR-V03 | Issuer RUC isn't `ACTIVO`/`HABIDO` | Continue; warn | *(one line in the summary)* `⚠️ El emisor figura {estado}/{condicion} en SUNAT.` |
| BR-V04 | The SUNAT API is down or credentials are missing | Continue as "not validated"; retry later | *(one line in the summary)* `ℹ️ No pude validar con SUNAT ahora; lo intento luego.` |
| BR-V05 | `estadoCp = 3` (authorized; only for some CPE types, e.g. pre-printed receipts authorized by SUNAT) | Continue | *(one line in the summary)* `ℹ️ SUNAT lo registra como AUTORIZADO (comprobante autorizado, no electrónico).` |
| BR-V06 | `estadoCp = 4` (not authorized) | `CONFIRM_INVALID` | `⚠️ SUNAT dice que este comprobante NO está AUTORIZADO. ¿Lo registro igual?` [Sí] [Cancelar] |

`estadoCp = 1` (accepted) continues with no message.

### Document retrieval (F)
| ID | Condition | Result | Chat message |
|---|---|---|---|
| BR-F01 | Flow B: `sunat_sol` isn't applicable (not the owner's RUC) | Skipped by design; **not a gap** | *(only listed in the F07 message)* `• SUNAT: no aplica (solo para facturas de tu empresa)` |
| BR-F02 | The chain ended in `MANUAL_REQUIRED`, and the issuer RUC has no portal entry and no `qr_url` | Raises gap `PORTAL_MISSING`; asks the user to teach the link (once per issuer). A taught link is used only after the owner approves it (BR-S06) | `No conozco el portal de descarga de {razon_social} (RUC {ruc_emisor}). Si lo tienes (suele estar impreso: "Consulte su comprobante en…"), envíamelo. Si no, responde "no sé".` |
| BR-F03 | A portal adapter fails `broken_after` (default 3) times in a row for technical reasons (selectors, HTTP 4xx/5xx, timeouts; not "invoice not found") | Raises gap `PORTAL_BROKEN`; the chain continues | *(only in the F07 message)* `• Portal {portal}: con fallas, ya avisé para arreglarlo` |
| BR-F04 | reCAPTCHA or hCaptcha, and no paid solver | Skip to the next fetcher | *(only in F07)* `• Portal {portal}: captcha que no puedo resolver` |
| BR-F05 | The user doesn't answer an image captcha within `ask_user_timeout_s` | Skip to the next fetcher | `Se venció el tiempo del captcha; sigo con otras opciones.` |
| BR-F06 | The fetched XML doesn't match the invoice (RUC, serie, número or total) | Discard it; next fetcher | *(only in F07)* `• {fetcher}: el XML no coincide` |
| BR-F08 | The request guard blocks a URL (not `https`, private or reserved IP, bad redirect, blocked scheme) | Skip to the next fetcher; the reason is an enum code | *(only in F07)* `• {fetcher}: enlace no permitido` |
| BR-F09 | The XML is unsafe or invalid (`DOCTYPE`, over 2 MB, does not parse) | Discard it; next fetcher | *(only in F07)* `• {fetcher}: el XML no es válido` |
| BR-F11 | The store has no website (`portal: manual`, D73) | Skipped; the chain goes on | *(only in F07)* `• {comercio}: no tiene portal; pídele los archivos al comercio` |
| BR-F10 | *(post-MVP, D57)* An XML from email or a user upload has no valid XMLDSig signature, or the signer's RUC ≠ `ruc_emisor` | Kept as a **hint**: the file is stored and delivered, but its values don't replace the read data | *(one line in the summary)* `ℹ️ El XML no tiene una firma válida del emisor; lo guardo, pero no reemplaza los datos leídos.` |
| BR-F07 | Every fetcher failed | `MANUAL_REQUIRED` (auto-resumable when a linked gap is fixed) | `❗ No pude obtener el XML/PDF de {serie}-{numero}. Intenté:` + one `•` line per fetcher + `Puedes enviarme {manual_docs_desc} aquí y lo completo. {gap_line}` |

`{gap_line}`, when a gap is linked: `Ya avisé para agregar este portal; cuando esté listo lo reintento solo.`

### Delivery (D)
| ID | Condition | Result | Chat message |
|---|---|---|---|
| BR-D01 | A Google or Microsoft token expired or was revoked | `PENDING_AUTH` (auto-resumes after re-auth); **admin-only alert**, not a repo gap | User: `Tu comprobante quedó en cola; hay un problema de conexión con {provider}. Te aviso al terminar.` Admin: `🔑 Reautoriza {provider}: ejecuta scripts/auth_{provider}.py` |
| BR-D02 | The Excel template is missing, its SHA-256 doesn't match the pinned one, or a `cells:` expression references an unknown field | Raises gap `TEMPLATE_INVALID`; `DELIVERING` → `MANUAL_REQUIRED` | `No pude llenar la plantilla de Excel (configuración incompleta). Ya avisé para corregirlo; reintento automáticamente.` |
| BR-D03 | The email can't be prepared or sent after retries | The batch goes `failed`; its invoice stays `BATCHED` (retry with `/reprocesar {id}`); admin alert | `No pude preparar el correo de {serie}-{numero}. Reintenta con /reprocesar {id}.` |
| BR-D04 | `/enviar` with an empty batch *(post-MVP, with `on_command`)* | No-op | `No hay comprobantes pendientes de envío.` |
| BR-D05 | The GitHub App can't get an installation token (private key revoked or invalid, App uninstalled from the ops repo, Issues permission missing) | Admin-only alert, at most daily; gap issues wait in the outbox | Admin: `🔑 La GitHub App no puede abrir issues en {repo} ({reason_desc}). Revisa la instalación o la clave.` |
| BR-D06 | Flow B: the total is above `reimbursement_cap` | `CONFIRMING`, waits for [Continuar] / [Cancelar] | `Esta factura es de S/ {total} y el tope es S/ {cap}: se reembolsarán S/ {cap}. ¿Continúo?` |
| BR-D07 | Flow B: the folder is filed and an assisted mail mode prepared the email (the flow B completion message, R-8.1) | The batch is `prepared` until the tap | `✅ Archivada en {folder}. El correo está listo ({modes_desc}); cuando lo envíes, avísame con el botón.` [Ya lo envié] |
| BR-D08 | Flow B: the sender tapped [Ya lo envié] | The batch is `sent`; the invoice is `DONE`; the folder gets `_Sent` | `👍 Marcada como enviada.` |
| BR-D09 | The sender sent a document for a stuck invoice, but `required_docs` is still incomplete (R-1.5, D66) | The document is attached; the invoice stays `MANUAL_REQUIRED` | `Recibí {received_desc}. Aún falta {missing_desc} para completar {serie}-{numero}.` |
| BR-D10 | Flow B: the email was sent and confirmed (`graph` accepted it, or the laptop wrote its receipt) | The batch is `sent`; the invoice is `DONE` | `✅ Archivada en {folder} y enviada por correo.` |
| BR-D11 | Flow B: the sender tapped [No llegó], or a laptop receipt is overdue by `mail.confirm_timeout` | Waits for the sender's choice; nothing is sent again on its own | `⚠️ El correo de {serie}-{numero} no está confirmado. ¿Cómo lo envío?` [Por la laptop] [Lo envío yo] [Ya llegó] |
| BR-D12 | Flow B: the laptop agent hasn't picked up a job for `agent_stale_after` | Once per job; the job stays queued | `💻 Tu laptop no ha enviado el correo de {serie}-{numero} desde hace {hours} h. Saldrá cuando la enciendas.` |
| BR-D13 | Flow B: `inbox_rule` sent the email to the owner's inbox (trusted, D67) | The batch is `sent`; the invoice is `DONE` | `✅ Archivada en {folder}. El correo salió por tu regla de Outlook.` [No llegó] |

### Payments (P)
| ID | Condition | Result | Chat message |
|---|---|---|---|
| BR-P01 | `/pagado`: exactly one set of sent, unpaid invoices adds up to the amount | Waits for [Sí] / [No] | `Pago de S/ {monto} del {fecha}: {n} facturas ({lista}). ¿Lo registro?` |
| BR-P02 | `/pagado`: several sets add up | Up to 3 sets as buttons, oldest invoices first | `Hay varias combinaciones que suman S/ {monto}. ¿Cuál es?` |
| BR-P03 | `/pagado`: no set adds up, or the listed invoices don't sum to the amount | No change | `Ninguna combinación de facturas enviadas suma S/ {monto}. Pendientes de pago:` + `{invoice_lines}` + `Indícalas así: /pagado {fecha} #12 #14` |
| BR-P04 | `/pagado` confirmed | The payment is stored; folder jobs are queued | `✅ Pago registrado: {n} carpetas marcadas como pagadas.` |

### System and session (S)
| ID | Condition | Result | Chat message |
|---|---|---|---|
| BR-S01 | The sender profile already has `max_queued_per_sender` jobs queued (back-pressure) | The message is stored but not enqueued | `Estoy procesando {n} comprobantes tuyos. Envíame este de nuevo cuando te avise que terminé.` |
| BR-S02 | A button is pressed for an invoice that has moved past the state it was meant for (stale button) | No change | `Ese botón ya no aplica: el comprobante #{id} ya está {state_desc}.` |
| BR-S03 | A transient error repeats 5 times, or the worker dies twice on the same job (crash loop) | `FAILED`; admin alert | `❌ No pude terminar el comprobante #{id} por un error técnico. Ya avisé al administrador; puedes reintentar con /reprocesar {id}.` Admin: `⚙️ Falló el trabajo {step} del comprobante #{id}: {error_code}.` |
| BR-S04 | A WhatsApp message falls outside the 24 h window, or Meta answers error 131047 (`WINDOW_EXPIRED`) | The outbox resends it as the matching utility template; if none is approved, it stays queued and the owner is alerted | *(no extra message; the template carries the text)* Admin: `📨 Falta la plantilla aprobada «{template}» en WhatsApp; hay mensajes en espera.` |
| BR-S05 | An API or MCP client asks for a write (`cancel_invoice`, `reprocess_invoice`, `attach_document`) | Pending action; nothing changes until the owner decides (expires in 24 h) | Owner: `🔐 {client} pide {action_desc} del comprobante #{id}. ¿Lo apruebo?` [Aprobar] [Rechazar] |
| BR-S06 | A sender teaches a portal link (BR-F02 answer or `/portal`) | Stored as `proposed`; used only after the owner approves | Owner: `🔗 {sender} propone este portal para {razon_social} (RUC {ruc_emisor}): {url}. ¿Lo uso?` [Aprobar] [Rechazar]. Sender: `Gracias. Lo revisará el administrador antes de usarlo.` |
| BR-S07 | A command names an invoice id that doesn't exist or belongs to another profile (sender not admin) | No change | `No encontré el comprobante #{id}.` |
| BR-S08 | A question isn't answered within `question_timeout` (default 24 h) in `CONFIRMING`, `EXTRACT_FAILED` or `CONFIRM_INVALID` | `PENDING_USER`; answering resumes the waiting state | `Dejé el comprobante #{id} en pendientes porque no tuve respuesta. Retómalo cuando quieras con /pendientes.` |
| BR-S09 | An allowlisted sender sends a photo and it is queued (R-1.1) | Invoice created in `RECEIVED`; job enqueued | `📥 Recibido. Estoy leyendo tu comprobante #{id}.` |

### Operations (O) · admin-only
| ID | Condition | Result | Chat message (admin) |
|---|---|---|---|
| BR-O01 | The outbox has more than 20 unsent rows, or one older than 15 min | Alert, rate-limited | `📮 Hay {n} mensajes sin enviar (el más antiguo de hace {age}).` |
| BR-O02 | Disk usage on the data volume is above 80% | Alert, rate-limited | `💾 El disco está al {pct}%.` |
| BR-O03 | A channel token is rejected (WhatsApp 401/190, Telegram 401) | Alert; sends on that channel wait in the outbox | `🔑 El token de {channel} no funciona. Renuévalo.` |
| BR-O04 | The nightly backup failed or wasn't uploaded | Alert | `🗄️ Falló la copia de seguridad de anoche ({reason_desc}).` |

---

## 3. Capability gaps
A **gap** is a missing capability that only a change to the repository can fix. Gaps are deduplicated by **fingerprint**, so 10 invoices from the same unknown store make **1** issue and **1** owner alert. Issues go only to the **private ops repo** (D44).

| Kind | Fingerprint | Raised by | GitHub (ops repo) | Owner alert (chat) | Auto-resume when |
|---|---|---|---|---|---|
| `PORTAL_MISSING` | `portal_missing:{ruc_emisor}` | BR-F02, only when the chain ended in `MANUAL_REQUIRED` | Issue, with the approved taught URL if any | Yes | A deployed store directory has the RUC with an `active` portal |
| `PORTAL_BROKEN` | `portal_broken:{portal_key}` | BR-F03 | Issue; the owner sets the entry to `status: broken` | Yes | The entry is `active` again and the version has changed |
| `TEMPLATE_INVALID` | `template_invalid:{flow}:{sink}` | BR-D02 | Issue | Yes | The config reloads and passes validation |
| `PARSER_MISS` | `parser_miss:{ruc_emisor}` | BR-E06 | Issue | Digest only | — (the user already typed the data) |
| `UNROUTED_BUYER` | `unrouted:{num_doc_adq}` | BR-R06 | Issue ("add a routing rule or a new flow?") | Digest only | — |

A PR from a template that adds the missing `stores/` and `portals/` files comes at the end of development (S16.2, D52); the `gap-watch` workflow and the other draft PRs are post-MVP (D44, spec 04 E16).

**What is *not* a gap:** expected business outcomes (flow B has no SOL access, an annulled invoice, a duplicate) and operations problems (expired OAuth tokens, SUNAT down, backups). Those get a chat message and, for operations, an admin alert, but nothing in GitHub.

### Gap lifecycle
```
detected ─► OPEN (row in `gaps`, linked invoices)
         ─► REPORTED (issue opened in the ops repo, owner alerted)
         ─► FIXED (the owner's fix is merged and deployed by CI; after the reload the re-check passes
                   → linked invoices re-enqueued, senders notified)
         ─► CLOSED (verified: a linked invoice got past the failing step, or none is left;
                    the bot closes the issue with a comment, `gap.closed`)
    OPEN/REPORTED ─► IGNORED (admin replies /gap ignorar <id>: no more alerts for this fingerprint)
```
If a re-enqueued invoice fails again at the same step, the gap goes back to REPORTED and the issue stays open. There are no "Closes #n" PRs in the MVP.

### What goes into GitHub (privacy)
Only typed **public or technical** data (R-11.12):
- issuer RUC (11 digits) and razón social from the padrón;
- CPE type;
- fetcher reason codes;
- the portal URL, reduced to `https://host/path`, in a code fence, and only once the owner approved it.

**Never:**
- buyer RUC or DNI, serie or número, totals, phone numbers, photos;
- counts, dates or anything else that reveals spending habits (they stay in the bot's database);
- raw OCR text, LLM text, or any text the user typed.

Gap issues go only to the private ops repo. These rules hold anyway, so a leaked issue reveals nothing about the owner's spending.
