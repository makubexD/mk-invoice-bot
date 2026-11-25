"""The issue comment a portal probe leaves: outcome, field table and a draft portal YAML (D193, R-11.12).

Only typed report fields reach the text: the probe's selectors, labels and option
names are already reduced to an allow-list, and the invoice data appears by name
(``serie``), never by value.
"""

from __future__ import annotations

from typing import Any

import yaml

from invoicebot.fetchers.browser.probe_protocol import ProbedField, ProbeReport
from invoicebot.github.issue_text import safe_url

#: The template each invoice value name is typed from (spec 03 §6). ``tipo`` is the portal's own label.
_TEMPLATES = {
    "ruc_emisor": "{ruc_emisor}",
    "tipo": "{tipo_label}",
    "serie": "{serie}",
    "numero": "{numero}",
    "fecha": "{fecha:%d/%m/%Y}",
    "total": "{total:.2f}",
}
#: Characters a table cell must not carry: they would break the row, open code or mention someone.
_CELL_STRIP = str.maketrans("", "", "|`@")
_NO_DATA = "sin dato"


def render_probe(url: str, report: ProbeReport) -> str | None:
    """Render the probe comment, or None when the URL or the YAML fails R-11.12.

    Fields the probe could not tie to an invoice value are listed in the table
    as ``sin dato`` and left out of the YAML draft, which would need a template
    for them that nobody can guess.
    """
    reduced = safe_url(url)
    if not reduced:
        return None
    draft = _draft(reduced, report)
    dumped = yaml.safe_dump(draft, sort_keys=False)
    if yaml.safe_load(dumped) != draft:
        return None
    lines = [f"**Sonda del portal:** {report.outcome.value}", ""]
    if report.fields:
        lines += ["| Etiqueta | Tipo | Dato |", "| --- | --- | --- |"]
        lines += [_row(field) for field in report.fields]
        lines.append("")
    lines += ["Borrador de `config/portals/<clave>.yaml`:", "", "```yaml", dumped.rstrip("\n"), "```"]
    return "\n".join(lines)


def _row(field: ProbedField) -> str:
    """Return the table row of one field, its label stripped of ``|``, backticks and ``@``."""
    label = field.label.translate(_CELL_STRIP).strip()
    return f"| {label} | {field.kind} | {field.value_name or _NO_DATA} |"


def _draft(url: str, report: ProbeReport) -> dict[str, Any]:
    """Return the portal draft as plain data; ``submit`` and ``downloads`` appear only when known."""
    draft: dict[str, Any] = {
        "url": url,
        "adapter": "generic_form",
        "captcha": report.captcha,
        "status": "todo",
        "fields": {
            field.selector: _field_value(field) for field in report.fields if field.value_name is not None
        },
    }
    if report.submit is not None:
        draft["submit"] = report.submit
    downloads = {"pdf": report.pdf, "xml": report.xml}
    present = {kind: selector for kind, selector in downloads.items() if selector is not None}
    if present:
        draft["downloads"] = present
    return draft


def _field_value(field: ProbedField) -> str | dict[str, str]:
    """Return the Portal field entry: a template, or ``{select: template}`` for a select."""
    template = _TEMPLATES[field.value_name or ""]
    return {"select": template} if field.kind == "select" else template
