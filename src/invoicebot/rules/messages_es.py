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
        "Este comprobante ya lo registré el {created_at:%d/%m} (#{id}). "
        "Si quieres repetirlo: /reprocesar {id}"
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
    "BR-D01_admin": "🔑 Reautoriza {provider}: ejecuta scripts/auth_{provider}.py",
    "BR-D02": (
        "No pude llenar la plantilla de Excel (configuración incompleta). "
        "Ya avisé para corregirlo; reintento automáticamente."
    ),
    "BR-D03": "No pude preparar el correo de {serie}-{numero}. Reintenta con /reprocesar {id}.",
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
    "BR-P04": "✅ Pago registrado: {n} carpetas marcadas como pagadas.",
    "BR-P01": "Pago de S/ {monto} del {fecha}: {n} facturas ({lista}). ¿Lo registro?",
    "BR-P02": "Hay varias combinaciones que suman S/ {monto}. ¿Cuál es?",
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
    words = [TIPO_PLURAL_DESC.get(tipo, tipo) for tipo in tipos]
    if len(words) < 2:
        return "".join(words)
    return f"{', '.join(words[:-1])} y {words[-1]}"


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
