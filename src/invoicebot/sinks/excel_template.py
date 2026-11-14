"""Fill the mapped cells of the pinned Excel template (R-7.4, R-7.10, D71).

One job: read the host template, check its pinned SHA-256, render each mapped cell and patch
the sheet, leaving every other package part untouched.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Literal

from invoicebot.config.models import ExcelTemplateSink
from invoicebot.sinks.expr import ExprError, render
from invoicebot.sinks.xlsx_patch import TemplateError, patch_cells

type TemplateReason = Literal["missing", "hash", "cells"]

_AMOUNT_FIELDS = frozenset({"base", "igv", "service_charge", "total"})
_REASON_MESSAGES: Mapping[TemplateReason, str] = {
    "missing": "the Excel template file is missing",
    "hash": "the Excel template does not match its pinned SHA-256",
    "cells": "the Excel template has cells that cannot be filled",
}


class TemplateInvalid(Exception):
    """The pinned Excel template cannot be filled (R-7.4, R-7.10, BR-D02)."""

    def __init__(self, reason: TemplateReason) -> None:
        self.reason = reason
        super().__init__(_REASON_MESSAGES[reason])


def build_excel(sink: ExcelTemplateSink, values: Mapping[str, object], overrides: Mapping[str, str]) -> bytes:
    """Return the template with the sink's cells filled from ``values`` and ``overrides`` (R-7.4).

    Raises:
        TemplateInvalid: the file is missing, its SHA-256 does not match the pinned one, or a
            cell cannot be rendered or patched.
    """
    data = _read_template(sink)
    _verify_hash(data, sink.template_sha256)
    try:
        writes = _writes(sink, values, overrides)
        return patch_cells(data, sink.sheet, writes)
    except (TemplateError, ExprError) as exc:
        raise TemplateInvalid("cells") from exc


def _read_template(sink: ExcelTemplateSink) -> bytes:
    """Return the template bytes, or raise ``TemplateInvalid("missing")`` (BR-D02)."""
    try:
        return Path(sink.template).read_bytes()
    except OSError as exc:
        raise TemplateInvalid("missing") from exc


def _verify_hash(data: bytes, expected: str) -> None:
    """Raise ``TemplateInvalid("hash")`` unless ``data`` matches ``expected`` (R-7.10, BR-D02)."""
    actual = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual, expected.lower()):
        raise TemplateInvalid("hash")


def _writes(
    sink: ExcelTemplateSink, values: Mapping[str, object], overrides: Mapping[str, str]
) -> dict[str, str | Decimal]:
    """Return the value to write into each mapped cell (R-7.4, R-7.10)."""
    motivo = overrides.get("motivo") or render(sink.defaults.motivo, values)
    descripcion = overrides.get("descripcion") or render(sink.defaults.descripcion, values)
    context = {**values, "motivo": motivo, "descripcion": descripcion}
    return {cell: _cell_value(expression, context) for cell, expression in sink.cells.items()}


def _cell_value(expression: str, context: Mapping[str, object]) -> str | Decimal:
    """Return the number or rendered text for one mapped cell (R-7.4, R-7.10)."""
    amount_field = _amount_field(expression)
    if amount_field is None:
        return render(expression, context)
    return _amount(context.get(amount_field))


def _amount_field(expression: str) -> str | None:
    """Return the amount field when ``expression`` is exactly ``{field}``, else ``None``."""
    stripped = expression.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    name = stripped[1:-1]
    return name if name in _AMOUNT_FIELDS else None


def _amount(value: object) -> Decimal:
    """Return ``value`` as a ``Decimal``, zero when missing, or raise ``TemplateInvalid``."""
    if value is None:
        return Decimal("0")
    if not isinstance(value, Decimal):
        raise TemplateInvalid("cells")
    return value
