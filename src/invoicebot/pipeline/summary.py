"""Builds the routed invoice summary text, its flow line and today's date."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any, cast

from psycopg import AsyncConnection

from invoicebot.config.checks import ISSUER_NAME_MAX
from invoicebot.config.models import Extraction, Flow
from invoicebot.extract.fields import REQUIRED, Reading
from invoicebot.extract.validate import check_date, doubtful_fields
from invoicebot.routing.router import RULE_ASK
from invoicebot.rules.messages_es import (
    ISSUER_NAMED,
    ISSUER_RUC,
    MESSAGES,
    SOURCE_DESC,
    SUMMARY,
    SUMMARY_DATE_FORMAT,
    SUMMARY_INFERRED,
    SUMMARY_REVIEW,
    SUMMARY_SOURCES,
    TIPO_DESC,
    fields_desc,
)

DATE_FIELD = "fecha_emision"


def summary_text(
    reading: Reading,
    flow_line: str,
    today: date,
    extraction: Extraction,
    extra_lines: Sequence[str] = (),
) -> str:
    """Return the routed-invoice summary (spec 06 §1 "Summary message", D79, D80)."""
    fecha = reading.fecha()
    tipo = reading.value("tipo") or ""
    lines = [
        SUMMARY.format(
            tipo_desc=TIPO_DESC.get(tipo, tipo),
            serie=reading.value("serie") or "",
            numero=reading.value("numero") or "",
            fecha=(reading.value(DATE_FIELD) or "") if fecha is None else fecha.strftime(SUMMARY_DATE_FORMAT),
            issuer=issuer_desc(reading),
            total=reading.value("total") or "",
            sources_line=SUMMARY_SOURCES.format(sources_desc=sources_desc(reading)),
            flow_line=flow_line,
        )
    ]
    inferred = [name for name, field in reading.fields.items() if field.flag == "inferred"]
    if inferred:
        lines.append(SUMMARY_INFERRED.format(fields_desc=fields_desc(inferred)))
    doubtful = list(doubtful_fields(reading, extraction.review_threshold, extraction.igv_rates))
    date_is_doubtful = fecha is None or check_date(fecha, today, extraction.max_invoice_age_days) is not None
    if date_is_doubtful and DATE_FIELD not in doubtful:
        doubtful.append(DATE_FIELD)
    if doubtful:
        lines.append(SUMMARY_REVIEW.format(fields_desc=fields_desc(doubtful)))
    lines.extend(extra_lines)
    return "\n".join(lines)


def issuer_desc(reading: Reading) -> str:
    """Return the summary's ``{issuer}``: the padrón's name and the RUC, or just the RUC (D188).

    A name longer than :data:`ISSUER_NAME_MAX` is cut with ``…``.
    """
    ruc = reading.value("ruc_emisor") or ""
    name = reading.value("razon_social")
    if name is None:
        return ISSUER_RUC.format(ruc=ruc)
    if len(name) > ISSUER_NAME_MAX:
        name = name[: ISSUER_NAME_MAX - 1] + "…"
    return ISSUER_NAMED.format(name=name, ruc=ruc)


def sources_desc(reading: Reading) -> str:
    """Return the summary's ``{sources_desc}``: where the required fields came from (D188).

    Sources are ordered by their first field in :data:`REQUIRED`; the source holding most
    fields is named alone first, the rest follow as ``{desc}: {fields_desc}``.
    """
    sources: dict[str, list[str]] = {}
    for name in REQUIRED:
        field = reading.fields.get(name)
        if field is None or reading.value(name) is None:
            continue
        sources.setdefault(field.source, []).append(name)
    ordered = sorted(sources, key=lambda source: REQUIRED.index(sources[source][0]))
    if not ordered:
        return ""
    primary = max(ordered, key=lambda source: len(sources[source]))
    parts = [SOURCE_DESC[primary]]
    parts.extend(
        f"{SOURCE_DESC[source]}: {fields_desc(sources[source])}" for source in ordered if source != primary
    )
    return "; ".join(parts)


def flow_line(flow: Flow, rule_id: str) -> str:
    """Return the summary's flow line for a routed invoice (spec 06 §1).

    A rule-matched flow shows that rule's message. A flow the sender picked in the BR-R03
    menu has no matched rule, so it shows the same ``➡️ {flow.label}`` line BR-R01 and
    BR-R02 carry.
    """
    if rule_id == RULE_ASK:
        return MESSAGES["BR-R02"].format(flow=flow)
    return MESSAGES[rule_id].format(flow=flow)


async def summary_today(conn: AsyncConnection[Any], timezone: str) -> date:
    """Return today's date in the owner's timezone, from the database clock."""
    cur = await conn.execute("SELECT (now() AT TIME ZONE %s)::date", (timezone,))
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("the database returned no date")
    return cast(date, row[0])
