"""Integration tests for the required-field flag and the invoice values the probe types (D193)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from playwright.async_api import Browser, Page, Route, async_playwright

from invoicebot.fetchers.base import FetchInvoice
from invoicebot.fetchers.browser.probe_controls import read_page
from invoicebot.fetchers.browser.probe_form import read_form
from invoicebot.fetchers.browser.probe_protocol import MAX_REPORT_FIELDS, ProbedField
from invoicebot.fetchers.browser.probe_values import Fill, apply_fills, plan_fills

_CORE = """
  <label for="serie">Serie</label><input id="serie" name="serie" />
  <label for="numero">N° Correlativo</label><input id="numero" name="numero" />
"""
_TIPO = """
  <label for="tipo">Tipo de Documento</label>
  <select id="tipo" name="tipo">
    <option>Seleccione</option><option>FACTURA ELECTRONICA</option><option>BOLETA DE VENTA</option>
  </select>
"""
_FULL = f"""<form>{_TIPO}{_CORE}
  <label for="fecha">Fecha de Emisión</label><input id="fecha" name="fecha" type="date" />
  <label for="total">Monto Total</label><input id="total" name="total" />
</form>"""


def _page(extra: str) -> str:
    """A form with two matched fields plus *extra* controls."""
    return f"<!doctype html><html><body><form>{_CORE}{extra}</form></body></html>"


def _invoice() -> FetchInvoice:
    return FetchInvoice(
        ruc_emisor="20123456789",
        tipo="01",
        serie="F001",
        numero="123",
        fecha=date(2026, 11, 5),
        total=Decimal("118.5"),
    )


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def browser() -> AsyncIterator[Browser]:
    """A headless Chromium for this module's tests."""
    async with async_playwright() as playwright:
        launched = await playwright.chromium.launch()
        yield launched
        await launched.close()


@pytest_asyncio.fixture(loop_scope="session")
async def page(browser: Browser) -> AsyncIterator[Page]:
    """A fresh page with a short timeout."""
    fresh = await browser.new_page()
    fresh.set_default_timeout(3000)
    yield fresh
    await fresh.close()


async def _flag(page: Page, extra: str) -> bool:
    await page.set_content(_page(extra))
    return (await read_page(page)).required_unmatched


async def _fields(page: Page, html: str) -> tuple[ProbedField, ...]:
    await page.set_content(html)
    return (await read_page(page)).fields


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    "extra",
    [
        '<textarea name="obs" required></textarea>',
        '<input type="radio" name="opt" value="a" required />',
        '<input type="checkbox" name="ok" required />',
        '<input name="extra" aria-required="true" />',
        '<input name="extra" required />',
        '<select name="s" required><option>Seleccione</option><option>A</option><option>B</option></select>',
    ],
)
async def test_required_unmatched_control_sets_the_flag(page: Page, extra: str) -> None:
    """D193: a required control the probe cannot fill sets required_unmatched."""
    assert await _flag(page, extra) is True


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    "extra",
    [
        '<input name="extra" />',
        '<textarea name="obs"></textarea>',
        '<input name="extra" required disabled />',
        '<input name="extra" required style="display:none" />',
        (
            '<select name="s" required><option value="">Seleccione</option>'
            '<option value="1">Unica</option></select>'
        ),
    ],
)
async def test_other_controls_leave_the_flag_down(page: Page, extra: str) -> None:
    """D193: non-required, hidden, disabled and one-real-option controls do not count."""
    assert await _flag(page, extra) is False


@pytest.mark.asyncio(loop_scope="session")
async def test_required_matched_field_is_fine(page: Page) -> None:
    """D193: a required control that maps to an invoice value does not set the flag."""
    await page.set_content(
        '<form><label>Serie<input name="serie" required /></label><input name="numero" required /></form>'
    )

    assert (await read_page(page)).required_unmatched is False


@pytest.mark.asyncio(loop_scope="session")
async def test_required_control_without_selector_sets_the_flag(page: Page) -> None:
    """D193: a required control with no id or name was dropped, so it counts."""
    assert await _flag(page, "<input required />") is True


@pytest.mark.asyncio(loop_scope="session")
async def test_required_control_past_the_field_cap_sets_the_flag(page: Page) -> None:
    """D193: a required control after MAX_REPORT_FIELDS members counts."""
    filler = "".join(f'<input name="f{n}" />' for n in range(MAX_REPORT_FIELDS))

    assert await _flag(page, filler + '<input name="late" required />') is True
    assert await _flag(page, filler + '<input name="late" />') is False


@pytest.mark.asyncio(loop_scope="session")
async def test_unread_types_are_not_fields(page: Page) -> None:
    """D193: textarea, radio and checkbox never show up as report fields."""
    fields = await _fields(page, _page('<textarea name="obs"></textarea><input type="checkbox" name="ok" />'))

    assert [field.selector for field in fields] == ["#serie", "#numero"]


@pytest.mark.asyncio(loop_scope="session")
async def test_read_form_carries_the_flag(page: Page) -> None:
    """D193: read_form copies required_unmatched from the form it ends on."""

    async def handle(route: Route) -> None:
        body = _page('<textarea name="obs" required></textarea>')
        await route.fulfill(body=body.encode(), content_type="text/html; charset=utf-8")

    await page.route("**/*", handle)

    found = await read_form(page, "https://portal.example.test/consulta")

    assert found.outcome is None
    assert found.required_unmatched is True


@pytest.mark.asyncio(loop_scope="session")
async def test_read_form_without_a_form_has_no_flag(page: Page) -> None:
    """D193: a non-form outcome leaves required_unmatched False."""

    async def handle(route: Route) -> None:
        await route.fulfill(body=b"<html><body>nada</body></html>", content_type="text/html")

    await page.route("**/*", handle)

    found = await read_form(page, "https://portal.example.test/consulta")

    assert found.outcome is not None
    assert found.required_unmatched is False


@pytest.mark.asyncio(loop_scope="session")
async def test_full_form_plan(page: Page) -> None:
    """D193: every value is decided: the tipo option by index, the date for a date input, total with cents."""
    await page.set_content(_FULL)
    fields = (await read_page(page)).fields

    plan = await plan_fills(page, fields, _invoice())

    assert plan.blocked is None
    assert plan.fills == (
        Fill("#tipo", "select", index=1),
        Fill("#serie", "text", "F001"),
        Fill("#numero", "text", "123"),
        Fill("#fecha", "text", "2026-11-05"),
        Fill("#total", "text", "118.50"),
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_text_date_follows_its_placeholder(page: Page) -> None:
    """D193: a text fecha takes the placeholder's format, dd/mm/yyyy without one."""
    for attribute, expected in (('placeholder="dd-mm-yyyy"', "05-11-2026"), ("", "05/11/2026")):
        await page.set_content(
            f'<form>{_CORE}<label for="fecha">Fecha de Emisión</label><input id="fecha" {attribute} /></form>'
        )
        fields = (await read_page(page)).fields

        plan = await plan_fills(page, fields, _invoice())

        assert plan.blocked is None
        assert plan.fills[-1] == Fill("#fecha", "text", expected)


@pytest.mark.asyncio(loop_scope="session")
async def test_tipo_text_input_blocks_the_plan(page: Page) -> None:
    """D193: a tipo typed into a text input cannot be decided, so it blocks the plan."""
    await page.set_content(
        f'<form><label for="tipo">Tipo de Documento</label><input id="tipo" name="tipo" />{_CORE}</form>'
    )
    fields = (await read_page(page)).fields

    plan = await plan_fills(page, fields, _invoice())

    assert plan.fills == ()
    assert plan.blocked is not None
    assert plan.blocked.label == "Tipo de Documento"


@pytest.mark.asyncio(loop_scope="session")
async def test_tipo_select_without_a_usable_option_blocks_the_plan(page: Page) -> None:
    """D193: a tipo select with no matching option blocks the plan."""
    body = _TIPO.replace("FACTURA ELECTRONICA", "NOTA DE CREDITO") + _CORE
    await page.set_content(f"<form>{body}</form>")
    fields = (await read_page(page)).fields

    plan = await plan_fills(page, fields, _invoice())

    assert plan.fills == ()
    assert plan.blocked is not None
    assert plan.blocked.selector == "#tipo"


@pytest.mark.asyncio(loop_scope="session")
async def test_other_select_needs_an_equal_option(page: Page) -> None:
    """D193: a serie select picks the option equal to the value (stripped), else no plan."""
    base = f'{_TIPO}<label for="numero">N° Correlativo</label><input id="numero" name="numero" />'
    select = (
        '<label for="serie">Serie</label>'
        '<select id="serie" name="serie"><option>Seleccione</option>{}</select>'
    )
    await page.set_content(
        f"<form>{base}{select.format('<option> F001 </option><option>B001</option>')}</form>"
    )
    fields = (await read_page(page)).fields

    plan = await plan_fills(page, fields, _invoice())

    assert plan.blocked is None
    assert Fill("#serie", "select", index=1) in plan.fills

    await page.set_content(f"<form>{base}{select.format('<option>B001</option>')}</form>")
    fields = (await read_page(page)).fields

    blocked = (await plan_fills(page, fields, _invoice())).blocked

    assert blocked is not None
    assert blocked.selector == "#serie"


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_fills_types_and_plan_does_not(page: Page) -> None:
    """D193: plan_fills leaves the page empty; apply_fills puts the values in."""
    await page.set_content(_FULL)
    fields = (await read_page(page)).fields

    plan = await plan_fills(page, fields, _invoice())

    assert plan.blocked is None
    assert await page.locator("#serie").input_value() == ""
    assert await page.locator("#tipo").evaluate("e => e.selectedIndex") == 0

    await apply_fills(page, plan.fills)

    assert await page.locator("#serie").input_value() == "F001"
    assert await page.locator("#numero").input_value() == "123"
    assert await page.locator("#fecha").input_value() == "2026-11-05"
    assert await page.locator("#total").input_value() == "118.50"
    assert (
        await page.locator("#tipo").evaluate("e => e.options[e.selectedIndex].text") == "FACTURA ELECTRONICA"
    )


def test_fill_repr_hides_the_value() -> None:
    """D193: a Fill never shows its value in a log line or traceback."""
    assert "F001" not in repr(Fill("#serie", "text", "F001"))
