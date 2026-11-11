"""es-PE chat messages for the business rules (spec 06).

Every text is copied from ``specs/06-business-rules.md`` exactly as written
there: emoji, accents, punctuation and line breaks are preserved, and the spec
placeholders stay in ``{name}`` form. ``tests/unit/test_rules_parity.py``
compares each entry with the spec row it came from.

Keys are rule IDs for a rule's primary message. A row that carries a second
message for a different recipient adds a ``_<role>`` suffix
(``_admin``, ``_owner`` or ``_sender``); the primary is the sender's message.
"""

MESSAGES: dict[str, str] = {
    "BR-R01": "🏢 Mi empresa → Google",
    "BR-R02": "💼 Trabajo → Microsoft",
    "BR-R03": (
        "Este comprobante está a nombre de {buyer_desc}, que no es tu empresa ni "
        "tu trabajo. ¿Dónde lo registro?"
    ),
    "BR-R04": (
        "No pude leer bien el RUC del cliente ({num_doc_adq}?). Lo registro en {flow.label}. ¿Correcto?"
    ),
    "BR-R05": (
        "No tienes permiso para registrar comprobantes de {flow.label}. "
        "Pídele al administrador que te habilite."
    ),
    "BR-R07": (
        "Este comprobante está a nombre de {buyer_desc}. Solo registro comprobantes "
        "de tu empresa o de tu trabajo, así que no lo proceso."
    ),
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
        "o el PDF/XML del comprobante, de hasta {max_mb} MB."
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
        "Puedes enviarme el PDF/XML aquí y lo completo. {gap_line}"
    ),
    "BR-D01": (
        "Tu comprobante quedó en cola; hay un problema de conexión con {provider}. Te aviso al terminar."
    ),
    "BR-D01_admin": "🔑 Reautoriza {provider}: ejecuta scripts/auth_{provider}.py",
    "BR-D02": (
        "No pude llenar la plantilla de Excel (configuración incompleta). "
        "Ya avisé para corregirlo; reintento automáticamente."
    ),
    "BR-D03": "No pude enviar el correo del lote #{batch}. Reintenta con /enviar.",
    "BR-D04": "No hay comprobantes pendientes de envío.",
    "BR-D05": (
        "🔑 La GitHub App no puede abrir issues en {repo} ({reason_desc}). Revisa la instalación o la clave."
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
    "BR-O01": "📮 Hay {n} mensajes sin enviar (el más antiguo de hace {age}).",
    "BR-O02": "💾 El disco está al {pct}%.",
    "BR-O03": "🔑 El token de {channel} no funciona. Renuévalo.",
    "BR-O04": "🗄️ Falló la copia de seguridad de anoche ({reason_desc}).",
}
