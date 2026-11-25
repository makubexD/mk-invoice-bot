"""Decide and type the invoice values for a form the probe has read (D193).

:func:`plan_fills` decides every value before anything is typed, so a form that cannot
be filled completely is never touched. :func:`apply_fills` then types them. No invoice
value is logged or put in an exception message here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from playwright.async_api import Page

from invoicebot.fetchers.base import FetchInvoice
from invoicebot.fetchers.browser.field_match import date_text, tipo_option
from invoicebot.fetchers.browser.probe_controls import MAX_OPTIONS
from invoicebot.fetchers.browser.probe_protocol import ProbedField, ValueName

#: What a text ``fecha`` input gets when it shows no placeholder (D193).
_DEFAULT_DATE_PLACEHOLDER = "dd/mm/yyyy"
_CENTS = Decimal("0.01")


@dataclass(frozen=True)
class Fill:
    """One control and what goes into it (D193).

    ``kind`` ``text`` types ``text``; ``select`` picks the option at ``index``.
    """

    selector: str
    kind: Literal["text", "select"]
    text: str = ""
    index: int = 0

    def __repr__(self) -> str:
        """Keep the typed value out of logs and tracebacks."""
        return f"Fill(selector={self.selector!r}, kind={self.kind!r})"


async def plan_fills(
    page: Page, fields: Sequence[ProbedField], invoice: FetchInvoice
) -> tuple[Fill, ...] | None:
    """Decide the fill of every field that has a value name, typing nothing (D193).

    Returns None when any such field cannot be filled. Fields with no value name are
    left alone, including a select with one real option.

    Raises:
        PlaywrightError: the page could not be read.
    """
    fills: list[Fill] = []
    for field in fields:
        if field.value_name is None:
            continue
        fill = await _plan_one(page, field, field.value_name, invoice)
        if fill is None:
            return None
        fills.append(fill)
    return tuple(fills)


async def apply_fills(page: Page, fills: Sequence[Fill]) -> None:
    """Type the planned values into the page, in order; nothing is submitted (D193).

    Raises:
        PlaywrightError: a control could not be filled.
    """
    for fill in fills:
        if fill.kind == "select":
            await page.locator(fill.selector).select_option(index=fill.index)
        else:
            await page.locator(fill.selector).fill(fill.text)


async def _plan_one(page: Page, field: ProbedField, name: ValueName, invoice: FetchInvoice) -> Fill | None:
    """Return the fill of one field, or None when it has no fitting value (D193)."""
    if field.kind == "select":
        return await _plan_select(page, field.selector, name, invoice)
    if name == "tipo":
        return None
    if name == "fecha":
        return Fill(field.selector, "text", await _fecha_text(page, field, invoice))
    return Fill(field.selector, "text", _plain_text(name, invoice))


async def _plan_select(page: Page, selector: str, name: ValueName, invoice: FetchInvoice) -> Fill | None:
    """Pick the select's option by index from its raw texts (D193).

    ``tipo`` uses :func:`tipo_option`. Any other value needs the option whose stripped
    text equals the value exactly (case and accents count); none equal gives None.
    """
    options = await _option_texts(page, selector)
    if name == "tipo":
        chosen = tipo_option(options, invoice.tipo)
        if chosen is None:
            return None
        return Fill(selector, "select", index=options.index(chosen))
    wanted = _plain_text(name, invoice)
    for index, text in enumerate(options):
        if text.strip() == wanted:
            return Fill(selector, "select", index=index)
    return None


async def _option_texts(page: Page, selector: str) -> list[str]:
    """Read the select's raw option texts from the page, capped (D193)."""
    texts = await page.locator(selector).locator("option").all_text_contents()
    return texts[:MAX_OPTIONS]


async def _fecha_text(page: Page, field: ProbedField, invoice: FetchInvoice) -> str:
    """Return the date as the control wants it: ISO for a date input, else by placeholder (D193)."""
    if field.kind == "date":
        return invoice.fecha.isoformat()
    placeholder = await page.locator(field.selector).get_attribute("placeholder") or ""
    return date_text(invoice.fecha, placeholder.strip() or _DEFAULT_DATE_PLACEHOLDER)


def _plain_text(name: ValueName, invoice: FetchInvoice) -> str:
    """Return the string form of a non-tipo value (D193).

    ``total`` is a plain decimal with two places (``118.00``); ``fecha`` is ISO (only
    reached for a select).
    """
    if name == "total":
        return f"{invoice.total.quantize(_CENTS):f}"
    if name == "fecha":
        return invoice.fecha.isoformat()
    texts = {
        "ruc_emisor": invoice.ruc_emisor,
        "tipo": invoice.tipo,
        "serie": invoice.serie,
        "numero": invoice.numero,
    }
    return texts[name]
