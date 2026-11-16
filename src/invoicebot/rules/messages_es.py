"""es-PE chat messages for the business rules (spec 06).

Every text is copied from ``specs/06-business-rules.md`` exactly as written
there: emoji, accents, punctuation and line breaks are preserved, and the spec
placeholders stay in ``{name}`` form. ``tests/unit/test_rules_parity.py``
compares each entry with the spec row it came from.

Keys are rule IDs for a rule's primary message. A row that carries a second
message for a different recipient adds a ``_<role>`` suffix
(``_admin``, ``_owner`` or ``_sender``); the primary is the sender's message.

``STATE_DESC`` holds the user-facing name of each invoice state, used for
BR-S02's ``{state_desc}``. The routed-invoice summary and the ``*_desc`` helpers build
the placeholders spec 06 §1 describes in prose.
"""

from collections.abc import Sequence

MESSAGES: dict[str, str] = {
    "BR-R01": "➡️ {flow.label}",
    "BR-R02": "➡️ {flow.label}",
    "BR-R03": ("Este comprobante está a nombre {buyer_desc}. ¿En qué flujo lo registro?"),
    "BR-R04": (
        "No pude leer bien el RUC del cliente ({num_doc_adq}?). Lo registro en {flow.label}. ¿Correcto?"
    ),
    "BR-R05": (
        "No tienes permiso para registrar comprobantes de {flow.label}. "
        "Pídele al administrador que te habilite."
    ),
    "BR-R07": (
        "Este comprobante está a nombre {buyer_desc}, que no corresponde a ninguno "
        "de tus flujos, así que no lo proceso."
    ),
    "BR-R08": "Este comprobante es {tipo_desc}, y {flow.label} solo acepta {accepted_desc}. No lo proceso.",
    "BR-R09": "¿De qué local es esta factura de {razon_social}?",
    "BR-E01": (
        "No pude leer este comprobante. Envíame una foto más cerca y con buena luz, "
        "o escribe los datos así:\n"
        "ruc=… serie=F001 numero=123 fecha=dd/mm/aaaa total=…"
    ),
    "BR-E02": "El RUC {ruc} no es válido (dígito verificador). Corrígelo con ruc=…",
    "BR-E03": "La fecha {fecha} parece incorrecta. ¿La corrijo? fecha=dd/mm/aaaa",
    "BR-E04": (
        "Este comprobante ya lo registré el {created_at:%d/%m} (#{id}). Mira cómo va con /estado {id}."
    ),
    "BR-E05": "Solo proceso facturas y boletas electrónicas. Esto parece {tipo_desc}.",
    "BR-E07": (
        "No puedo procesar este archivo ({reason_desc}). Envíame una foto (JPG o PNG) "
        "o el PDF del comprobante, de hasta {max_mb} MB."
    ),
    "BR-E09": (
        "Revisa estos datos antes de seguir: {fields_desc}. Si están bien, confírmalos; "
        "si no, corrígelos así: campo=valor."
    ),
    "BR-E08": (
        "No pude leer este comprobante. Envíame una foto más cerca y con buena luz, "
        "o escribe los datos así:\n"
        "ruc=… serie=F001 numero=123 fecha=dd/mm/aaaa total=…"
    ),
    "BR-E08_admin": ("🤖 Se alcanzó el tope mensual de lectura con IA ({cap}). Se reactiva el {date}."),
    "BR-V01": "⏳ SUNAT aún no lo registra; vuelvo a revisar mañana.",
    "BR-V02": "⚠️ SUNAT dice que este comprobante está ANULADO. ¿Lo registro igual?",
    "BR-V03": "⚠️ El emisor figura {estado}/{condicion} en SUNAT.",
    "BR-V04": "ℹ️ No pude validar con SUNAT ahora; lo intento luego.",
    "BR-V05": "ℹ️ SUNAT lo registra como AUTORIZADO (comprobante autorizado, no electrónico).",
    "BR-V06": "⚠️ SUNAT dice que este comprobante NO está AUTORIZADO. ¿Lo registro igual?",
    "BR-F01": "• SUNAT: no aplica (solo para facturas de tu empresa)",
    "BR-F02": (
        "No conozco el portal de descarga de {razon_social} (RUC {ruc_emisor}). "
        'Si lo tienes (suele estar impreso: "Consulte su comprobante en…"), envíamelo. '
        'Si no, responde "no sé".'
    ),
    "BR-F02_dont_know": "Gracias. Ya avisé al administrador para agregar este portal.",
    "BR-F03": "• Portal {portal}: con fallas, ya avisé para arreglarlo",
    "BR-F04": "• Portal {portal}: captcha que no puedo resolver",
    "BR-F05": "Se venció el tiempo del captcha; sigo con otras opciones.",
    "BR-F06": "• {fetcher}: el XML no coincide",
    "BR-F08": "• {fetcher}: enlace no permitido",
    "BR-F09": "• {fetcher}: el XML no es válido",
    "BR-F10": (
        "ℹ️ El XML no tiene una firma válida del emisor; lo guardo, pero no reemplaza los datos leídos."
    ),
    "BR-F07": (
        "❗ No pude obtener el XML/PDF de {serie}-{numero}. Intenté:\n"
        "{fetcher_lines}\n"
        "Puedes enviarme {manual_docs_desc} aquí y lo completo. {gap_line}"
    ),
    "BR-F11": "• {comercio}: no tiene portal; pídele los archivos al comercio",
    "BR-D01": (
        "Tu comprobante quedó en cola; hay un problema de conexión con {provider}. Te aviso al terminar."
    ),
    "BR-D01_admin": (
        "🔑 Reautoriza {provider} ({account}): ejecuta scripts/auth_{provider}.py --account {account}"
    ),
    "BR-D02": (
        "No pude llenar la plantilla de Excel (configuración incompleta). "
        "Ya avisé para corregirlo; reintento automáticamente."
    ),
    "BR-D03": "No pude preparar el correo de {serie}-{numero}. Reintenta con /reprocesar {id}.",
    "BR-D03_admin": (
        "📮 No se pudo enviar el correo del comprobante #{id} ({serie}-{numero}). "
        "Revisa la cuenta y ejecuta /reprocesar {id}."
    ),
    "BR-D04": "No hay comprobantes pendientes de envío.",
    "BR-D05": (
        "🔑 La GitHub App no puede abrir issues en {repo} ({reason_desc}). Revisa la instalación o la clave."
    ),
    "BR-D06": "Esta factura es de S/ {total} y el tope es S/ {cap}: se reembolsarán S/ {cap}. ¿Continúo?",
    "BR-D07": (
        "✅ Archivada en {folder}. El correo está listo ({modes_desc}); "
        "cuando lo envíes, avísame con el botón."
    ),
    "BR-D08": "👍 Marcada como enviada.",
    "BR-D09": "Recibí {received_desc}. Aún falta {missing_desc} para completar {serie}-{numero}.",
    "BR-D10": "✅ Archivada en {folder} y enviada por correo.",
    "BR-D11": "⚠️ El correo de {serie}-{numero} no está confirmado. ¿Cómo lo envío?",
    "BR-D13": "✅ Archivada en {folder}. El correo salió por tu regla de Outlook.",
    "BR-D12": (
        "💻 Tu laptop no ha enviado el correo de {serie}-{numero} desde hace {hours} h. "
        "Saldrá cuando la enciendas."
    ),
    "BR-D14": (
        "Tienes varios comprobantes esperando archivos: {invoices}. "
        "Vuelve a enviar el archivo respondiendo a la foto del comprobante."
    ),
    "BR-D15": "El archivo que enviaste no es un PDF válido; no lo guardé.",
    "BR-P04": "✅ Pago registrado: {n} carpetas marcadas como pagadas.",
    "BR-P01": "Pago de S/ {monto} del {fecha}: {n} facturas ({lista}). ¿Lo registro?",
    "BR-P02": "Hay varias combinaciones que suman S/ {monto}. ¿Cuál es?\n{set_lines}",
    "BR-P03": (
        "Ninguna combinación de facturas enviadas suma S/ {monto}. Pendientes de pago:\n"
        "{invoice_lines}\n"
        "Indícalas así: /pagado {fecha} #12 #14"
    ),
    "BR-S01": ("Estoy procesando {n} comprobantes tuyos. Envíame este de nuevo cuando te avise que terminé."),
    "BR-S02": "Ese botón ya no aplica: el comprobante #{id} ya está {state_desc}.",
    "BR-S03": (
        "❌ No pude terminar el comprobante #{id} por un error técnico. Ya avisé al "
        "administrador; puedes reintentar con /reprocesar {id}."
    ),
    "BR-S03_admin": "⚙️ Falló el trabajo {step} del comprobante #{id}: {error_code}.",
    "BR-S04": "📨 Falta la plantilla aprobada «{template}» en WhatsApp; hay mensajes en espera.",
    "BR-S05": "🔐 {client} pide {action_desc} del comprobante #{id}. ¿Lo apruebo?",
    "BR-S06": "Gracias. Lo revisará el administrador antes de usarlo.",
    "BR-S06_owner": (
        "🔗 {sender} propone este portal para {razon_social} (RUC {ruc_emisor}): {url}. ¿Lo uso?"
    ),
    "BR-S06_approved": "✅ Guardé el portal de RUC {ruc_emisor}; va en el issue.",
    "BR-S06_rejected": "Descarté el portal propuesto para RUC {ruc_emisor}.",
    "BR-S07": "No encontré el comprobante #{id}.",
    "BR-S08": (
        "Dejé el comprobante #{id} en pendientes porque no tuve respuesta. "
        "Retómalo cuando quieras con /pendientes."
    ),
    "BR-S09": "📥 Recibido. Estoy leyendo tu comprobante #{id}.",
    "BR-O01": "📮 Hay {n} mensajes sin enviar (el más antiguo de hace {age}).",
    "BR-O02": "💾 El disco está al {pct}%.",
    "BR-O03": "🔑 El token de {channel} no funciona. Renuévalo.",
    "BR-O04": "🗄️ Falló la copia de seguridad de anoche ({reason_desc}).",
    "GAP_admin": "🧩 Falta configurar: {kind_desc} — {subject}.{issue_line}",
    "GAP_DIGEST_admin": "🧩 Pendientes de configuración:\n{gap_lines}",
}

#: BR-F07's ``{gap_line}`` when a PORTAL_MISSING gap is linked to the invoice (spec 06 §1).
GAP_LINE_PORTAL = "Ya avisé para agregar este portal; cuando esté listo lo reintento solo."

#: ``{razon_social}`` in BR-F02 and BR-S06 when the issuer's name is unknown.
UNKNOWN_ISSUER = "este emisor"

#: One ``GAP_DIGEST_admin`` line per gap: its missing capability and its subject.
GAP_DIGEST_LINE = "• {kind_desc} — {subject}"

#: BR-D05 ``reason_desc``: what each GitHub token-failure reason means, keyed by reason.
TOKEN_REASON_DESC: dict[str, str] = {
    "bad_key": "la clave privada no es válida",
    "uninstalled": "la App no está instalada en el repositorio",
    "no_permission": "le falta el permiso de Issues",
    "unreachable": "GitHub no responde",
}

#: Spec 06 §3 ``kind_desc``: the capability each gap kind is missing, keyed by kind.
KIND_DESC: dict[str, str] = {
    "PORTAL_MISSING": "portal de descarga desconocido",
    "PORTAL_BROKEN": "portal con fallas",
    "TEMPLATE_INVALID": "plantilla inválida",
    "PARSER_MISS": "no puedo leer los comprobantes de un emisor",
    "UNROUTED_BUYER": "RUC comprador sin flujo",
}

#: Spec 06 §3 subject format of each gap kind, keyed by kind.
KIND_SUBJECT: dict[str, str] = {
    "PORTAL_MISSING": "RUC {ruc_emisor}",
    "PORTAL_BROKEN": "{portal_key}",
    "TEMPLATE_INVALID": "{flow}/{sink}",
    "PARSER_MISS": "RUC {ruc_emisor}",
    "UNROUTED_BUYER": "RUC {num_doc_adq}",
}

STATE_DESC: dict[str, str] = {
    "RECEIVED": "recibido",
    "EXTRACTING": "en lectura",
    "EXTRACT_FAILED": "esperando una mejor foto",
    "ROUTING": "en clasificación",
    "CONFIRMING": "esperando tu confirmación",
    "VALIDATING": "en validación",
    "CONFIRM_INVALID": "esperando tu decisión",
    "FETCHING_DOCS": "buscando sus archivos",
    "WAITING_CAPTCHA": "esperando un captcha",
    "MANUAL_REQUIRED": "esperando sus archivos",
    "PENDING_USER": "pendiente de tu respuesta",
    "PENDING_AUTH": "esperando una autorización",
    "DELIVERING": "en entrega",
    "BATCHED": "en el lote",
    "DONE": "terminado",
    "CANCELLED": "cancelado",
    "FAILED": "con error",
}

_UNKNOWN_STATE_DESC = "en otro estado"


#: BR-E07's ``{reason_desc}`` per media rejection reason (R-1.11).
REJECTION_DESC: dict[str, str] = {
    "too_large": "es demasiado grande",
    "too_many_pixels": "la imagen es demasiado grande",
    "unsupported_type": "el tipo de archivo no es compatible",
    "zip_entries": "el archivo comprimido no es válido",
    "zip_ratio": "el archivo comprimido no es válido",
}


def state_desc(state: str) -> str:
    """Return the es-PE description of an invoice state for BR-S02's ``{state_desc}``."""
    return STATE_DESC.get(state, _UNKNOWN_STATE_DESC)


#: The routed-invoice summary (spec 06 §1 "Summary message"). ``{flow_line}`` is the
#: matched routing rule's own message, e.g. BR-R01's ``➡️ {flow.label}``.
SUMMARY = (
    "🧾 {tipo_desc} {serie}-{numero} · {fecha}\nEmisor: RUC {ruc_emisor}\nTotal: S/ {total}\n{flow_line}"
)
#: Summary line naming the fields repaired from the QR payload (D79).
SUMMARY_INFERRED = "ℹ️ Deducido del comprobante: {fields_desc}"
#: Summary line naming the doubtful fields; the invoice still goes on in this phase (D80).
SUMMARY_REVIEW = "⚠️ Revisa: {fields_desc}"
#: How the summary writes ``{fecha}``.
SUMMARY_DATE_FORMAT = "%d/%m/%Y"

#: ``{tipo_desc}`` in the summary, per SUNAT document type.
TIPO_DESC: dict[str, str] = {
    "01": "Factura",
    "03": "Boleta",
    "07": "Nota de crédito",
    "08": "Nota de débito",
}
#: ``{accepted_desc}`` words for BR-R08, per document type.
TIPO_PLURAL_DESC: dict[str, str] = {
    "01": "facturas",
    "03": "boletas",
    "07": "notas de crédito",
    "08": "notas de débito",
}
#: ``{tipo_desc}`` for a type outside :data:`TIPO_DESC` (BR-E05).
_OTHER_TIPO_DESC = "otro tipo de documento (código {tipo})"

#: ``{fields_desc}`` names, per field.
FIELD_DESC: dict[str, str] = {
    "ruc_emisor": "RUC del emisor",
    "tipo": "tipo de comprobante",
    "serie": "serie",
    "numero": "número",
    "fecha_emision": "fecha",
    "total": "total",
    "igv": "IGV",
    "tipo_doc_adq": "tipo de documento del cliente",
    "num_doc_adq": "documento del cliente",
}

#: ``{buyer_desc}`` per SUNAT buyer document type (catálogo 06: 6 = RUC, 1 = DNI).
BUYER_DESC: dict[str, str] = {
    "6": "del RUC {num}",
    "1": "del DNI {num}",
}
_OTHER_BUYER_DESC = "del documento {num}"
_NO_BUYER_DESC = "de ningún cliente (boleta sin datos)"


def tipo_phrase(tipo: str) -> str:
    """Return ``{tipo_desc}`` for a sentence, as in BR-R08 and BR-E05 ("una boleta").

    Every supported type is a feminine noun, so the article is always "una".
    """
    desc = TIPO_DESC.get(tipo)
    return _OTHER_TIPO_DESC.format(tipo=tipo) if desc is None else f"una {desc.lower()}"


def tipos_desc(tipos: Sequence[str]) -> str:
    """Return BR-R08's ``{accepted_desc}``: "facturas", "facturas y boletas"."""
    return _join_es([TIPO_PLURAL_DESC.get(tipo, tipo) for tipo in tipos])


def fields_desc(names: Sequence[str]) -> str:
    """Return ``{fields_desc}``: the fields' es-PE names, comma separated."""
    return ", ".join(FIELD_DESC.get(name, name) for name in names)


def buyer_desc(tipo_doc: str, num_doc: str) -> str:
    """Return ``{buyer_desc}`` for BR-R03 and BR-R07.

    shortcut: the spec adds the buyer's razón social after a RUC; that needs the padrón,
    which arrives in ``read-more``.
    """
    if not num_doc:
        return _NO_BUYER_DESC
    template = BUYER_DESC.get(tipo_doc, _OTHER_BUYER_DESC)
    return template.format(num=num_doc)


# --- Fetch texts (docs/plan-mvp-fetch.md step 14) ------------------------------------------
# These are not spec 06 rows, so they stay outside MESSAGES and the parity test does not
# see them. The copy is pending the owner's review.

#: Status line while a portal has not published the invoice yet (D65).
FETCH_WAITING = "⏳ Comprobante #{id}: esperando al portal. Lo vuelvo a buscar más tarde."
#: Status line while the invoice's mailbox has not received it yet (D100).
FETCH_WAITING_MAIL = "⏳ Comprobante #{id}: esperando correo del comercio. Lo vuelvo a buscar más tarde."
#: One BR-F07 bullet for a fetcher whose last reason has no BR-F text of its own.
FETCH_NOT_FOUND_LINE = "• {fetcher}: no encontré el comprobante"
#: BR-F07 bullet: the store has no known portal to fetch from (S7.4).
FETCH_NO_PORTAL_LINE = "• Portal del comercio: no lo conozco"
#: BR-F07 bullet: the store's portal is configured but not implemented yet (S7.4).
FETCH_PORTAL_TODO_LINE = "• Portal {portal}: todavía no sé consultarlo"
#: BR-F07 bullet: the fetcher failed for a technical reason or a timeout (S7.4).
FETCH_TECHNICAL_LINE = "• {fetcher}: falló por un error técnico"
#: BR-F07 bullet: the mailbox needs a new sign-in, which the owner was already told (S7.4).
FETCH_AUTH_LINE = "• Correo: el buzón necesita reconectarse; ya avisé al administrador"
#: BR-F11's ``{comercio}`` when the store directory does not know the issuer (BR-F07).
STORE_FALLBACK = "el comercio"
#: BR-F07's ``{portal}`` when the store has no portal to name (BR-F07).
STORE_FALLBACK_OF = "del comercio"
#: es-PE names of the fetchers, used in BR-F07's bullets.
FETCHER_DESC: dict[str, str] = {
    "qr_url": "Enlace del QR",
    "portal_registry": "Portal del comercio",
    "sunat_sol": "SUNAT",
    "email_inbox": "Correo",
}
#: es-PE names of the required document kinds, used in BR-F07's ``{manual_docs_desc}``.
DOC_DESC: dict[str, str] = {"pdf": "el PDF", "xml": "el XML"}
#: Reply to an uploaded XML that is unsafe or invalid (BR-F09, D101).
UPLOAD_XML_INVALID = "El XML que enviaste no es válido; no lo guardé."
#: Reply to an uploaded XML that is not the invoice it was sent for (BR-F06, D101).
UPLOAD_XML_MISMATCH = "El XML que enviaste no corresponde a {serie}-{numero}; no lo guardé."


def fetcher_desc(name: str) -> str:
    """Return the es-PE name of a fetcher for BR-F07's bullets."""
    return FETCHER_DESC.get(name, name)


def _join_es(words: Sequence[str]) -> str:
    """Join words as "a", "a y b" or "a, b y c"."""
    if len(words) < 2:
        return "".join(words)
    return f"{', '.join(words[:-1])} y {words[-1]}"


def docs_desc(kinds: Sequence[str]) -> str:
    """Return BR-F07's ``{manual_docs_desc}``: "el PDF", "el PDF y el XML"."""
    return _join_es([DOC_DESC.get(kind, kind) for kind in kinds])


#: BR-D07's ``{modes_desc}`` per mail mode (R-7.7).
MAIL_MODE_DESC: dict[str, str] = {"dry_run": "borrador .eml"}
#: The [Ya lo envié] button's label (R-8.1).
BUTTON_SENT = "Ya lo envié"
#: BR-D13's [No llegó] button (R-7.8).
BUTTON_NOT_ARRIVED = "No llegó"
#: BR-D11's three buttons (R-7.8, D109).
BUTTON_VIA_LAPTOP = "Por la laptop"
BUTTON_SELF_SEND = "Lo envío yo"
BUTTON_ARRIVED = "Ya llegó"
#: BR-D06's two reply buttons (R-7.13).
BUTTON_CONTINUE = "Continuar"
BUTTON_CANCEL = "Cancelar"
#: The routed summary's [Editar] button (R-3.4).
BUTTON_EDIT = "Editar"
#: BR-R04's [Sí] and [Cambiar] buttons (S4.4).
BUTTON_YES = "Sí"
#: The [No] button that closes a BR-P01 or BR-P02 payment proposal (R-7.12).
BUTTON_NO = "No"
BUTTON_CHANGE = "Cambiar"
#: BR-S06's two owner buttons (D118).
BUTTON_APPROVE = "Aprobar"
BUTTON_REJECT = "Rechazar"
#: The list message's button label when more than three choices need buttons (BR-R03, BR-R09).
LIST_BUTTON = "Elegir"
#: Reply after the sender cancels the BR-D06 invoice with [Cancelar] (R-7.13).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
CANCELLED_BY_USER = "🚫 Cancelado: no registraré el comprobante #{id}."
#: One-line confirmation after a chat motivo= / descripcion= override (R-7.4, D70).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
OVERRIDES_SAVED = "📝 Anotado en el comprobante #{id}."
#: The general reply when an edit line cannot be understood (R-3.5, D97).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
EDIT_INVALID = (
    "No entendí «{line}». Escribe cada cambio así: campo=valor, por ejemplo total=45.50. "
    "Puedes cambiar ruc, serie, numero, fecha, total y cliente."
)
#: The reply when an edit arrives in a state that no longer accepts one (R-3.5, D97).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
EDIT_LATE = "El comprobante #{id} ya está {state_desc}; ya no puedo cambiar sus datos."
#: What [Editar] on the routed summary asks the sender for (R-3.4).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
EDIT_PROMPT = (
    "Envíame los cambios, uno por línea, así: campo=valor. Por ejemplo: total=45.50 o "
    "fecha=05/11/2026. Puedes cambiar ruc, serie, numero, fecha, total y cliente."
)
#: The first line of the command list (R-10.1).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
HELP_HEADER = "Envíame la foto de un comprobante y yo me encargo. También entiendo:"
#: ``/ayuda``'s own line in the command list (R-10.1).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
HELP_AYUDA = "/ayuda: esta lista"
#: ``/estado``'s own line in the command list (S4.9).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
HELP_ESTADO = "/estado 12: cómo va el comprobante #12"
#: ``/cancelar``'s own line in the command list (S4.9).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
HELP_CANCELAR = "/cancelar 12: cancela el comprobante #12"
#: ``/reprocesar``'s own line in the command list (S4.9).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
HELP_REPROCESAR = "/reprocesar 12: reintenta el comprobante #12 si falló"
#: ``/pendientes``'s own line in the command list (S4.9).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
HELP_PENDIENTES = "/pendientes: lo que espera tu respuesta"
#: ``/pagado``'s own line in the command list (R-7.12).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
HELP_PAGADO = "/pagado dd/mm monto, o /pagado dd/mm #12 #14: registra un reembolso recibido"
#: ``/portal``'s own line in the command list (R-11.4).
HELP_PORTAL = "/portal 20100000001 https://…: propone el portal de descarga de ese RUC"
#: One BR-P02 set line in the body, numbered from 1 (R-7.12, D107).
PAYMENT_SET_LINE = "Opción {n}: {fecha_min}…, {count} facturas: {ids}"
#: BR-P02's short button title for one matching set, numbered from 1 (R-7.12, D107).
BUTTON_OPTION = "Opción {n}"
#: One invoice line of BR-P03's pending list (R-7.12, BR-P03).
PAYMENT_LINE = "#{id} · {fecha} · S/ {monto}"
#: ``/pendientes``'s answer when nothing waits on the sender (S4.8, S4.9).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
PENDING_NONE = "No tienes comprobantes pendientes."
#: The first line of ``/pendientes``'s list (S4.8).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
PENDING_HEADER = "Esto espera algo de ti:"
#: One waiting invoice in ``/pendientes``'s list (S4.8, S4.9).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
PENDING_LINE = "#{id} · {issuer} · {total} · {state_desc}"
#: ``{issuer}`` for an invoice whose razón social is not read yet (S4.8).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
PENDING_NO_ISSUER = "emisor sin leer"
#: One prepared batch in ``/pendientes``'s list (S11.4).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
PENDING_BATCH_LINE = "#{id} · {issuer} · el correo está listo desde el {prepared_at:%d/%m}; falta enviarlo"
#: The last line of ``/pendientes`` when more invoices matched than it lists (S11.4).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
PENDING_MORE = "…y {count} más."
#: The first line of ``/estado``'s answer (S4.9).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
STATUS = "El comprobante #{id} está {state_desc}."
#: The second line of ``/estado``'s answer, with the last stored question text (S4.9).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
STATUS_RULE = "Último aviso ({rule_id}): {text}"
#: The second line of ``/estado``'s answer when the rule left no stored text (S4.9).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
STATUS_RULE_ONLY = "Motivo: {rule_id}."
#: The answer when a command that needs an invoice id is sent without one (R-10.1).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
COMMAND_NEEDS_ID = "Falta el número del comprobante. Por ejemplo: /{command} 12"
#: ``/cancelar``'s answer when the invoice is past CANCELLABLE (S4.9, D96).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
CANCEL_LATE = "El comprobante #{id} ya está {state_desc}; ya no se puede cancelar."
#: ``/reprocesar``'s answer when it requeued the invoice's step (S4.9, D96).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
REPROCESS_STARTED = "Vuelvo a intentar el comprobante #{id}."
#: ``/reprocesar``'s answer when the invoice's state has no step to restart (S4.9, D96).
#: Not a spec 06 row, so it stays outside MESSAGES; the copy is pending the owner's review.
REPROCESS_NOT_ALLOWED = (
    "El comprobante #{id} está {state_desc}. /reprocesar solo sirve para comprobantes "
    "que fallaron o que esperan sus archivos."
)


def modes_desc(modes: Sequence[str]) -> str:
    """Return BR-D07's ``{modes_desc}``: the es-PE name of each mail mode (R-7.7)."""
    return _join_es([MAIL_MODE_DESC.get(mode, mode) for mode in modes])
