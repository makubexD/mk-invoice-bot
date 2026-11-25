"""Integration tests for reading a portal's current form (D193)."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from playwright.async_api import Browser, Page, Route, async_playwright

from invoicebot.fetchers.browser.probe_controls import read_page
from invoicebot.fetchers.browser.probe_protocol import SAFE_TEXT_PATTERN, safe_text

_FULL_FORM = """<!doctype html>
<html>
<body>
<form id="consulta">
  <label for="tipo">Tipo de Documento</label>
  <select id="tipo" name="tipo">
    <option>Seleccione</option>
    <option>FACTURA</option>
    <option>BOLETA</option>
  </select>
  <label for="serie">Serie</label>
  <input id="serie" name="serie" />
  <label for="numero">N° Correlativo</label>
  <input id="numero" name="numero" />
  <label for="fecha">Fecha de Emisión</label>
  <input id="fecha" name="fecha" type="date" />
  <label for="total">Monto Total</label>
  <input id="total" name="total" />
  <button id="buscar" type="submit">Buscar</button>
</form>
</body>
</html>
"""

_LONG_LABEL = "Total " + "x" * 94


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


@pytest.mark.asyncio(loop_scope="session")
async def test_full_form_is_read_in_document_order(page: Page) -> None:
    """D193: the form's controls keep their order, labels, kinds, options and names."""
    await page.set_content(_FULL_FORM)

    form = await read_page(page)

    assert form.captcha == "none"
    assert form.has_password is False
    assert [field.selector for field in form.fields] == [
        "#tipo",
        "#serie",
        "#numero",
        "#fecha",
        "#total",
    ]
    assert [(field.kind, field.label, field.value_name) for field in form.fields] == [
        ("select", "Tipo de Documento", "tipo"),
        ("text", "Serie", "serie"),
        ("text", "N° Correlativo", "numero"),
        ("date", "Fecha de Emisión", "fecha"),
        ("text", "Monto Total", "total"),
    ]
    assert form.fields[0].options == ("Seleccione", "FACTURA", "BOLETA")
    assert form.fields[1].options == ()
    assert form.submit == "#buscar"


@pytest.mark.asyncio(loop_scope="session")
async def test_aspnet_names_find_fields_without_labels(page: Page) -> None:
    """D193: a label-less ASP.NET form is read from its control names."""
    await page.set_content(
        """<html><body><form id="aspnet">
          <input id="serie1" name="ctl00$txtSerie" />
          <input id="numero1" name="ctl00$txtCorrelativo" />
          <input id="total1" name="ctl00$txtMonto" />
          <input id="buscar1" name="ctl00$btnBuscar" type="submit" value="Buscar" />
        </form></body></html>"""
    )

    form = await read_page(page)

    assert [field.selector for field in form.fields] == ["#serie1", "#numero1", "#total1"]
    assert [field.value_name for field in form.fields] == ["serie", "numero", "total"]
    assert [field.label for field in form.fields] == ["", "", ""]
    assert form.submit == "#buscar1"


@pytest.mark.asyncio(loop_scope="session")
async def test_each_label_source_is_used(page: Page) -> None:
    """D193: the cell before, aria-label, placeholder and the enclosing label all label a control."""
    await page.set_content(
        """<html><body><form>
          <table>
            <tr><td>Serie</td><td><input id="serie" /></td></tr>
            <tr><td>Monto Total</td><td><input id="total" /></td></tr>
          </table>
          <input id="numero" aria-label="N° Correlativo" />
          <input id="fecha" placeholder="Fecha de Emisión" />
          <label>RUC del Emisor <input id="ruc" /></label>
        </form></body></html>"""
    )

    form = await read_page(page)

    assert [(field.selector, field.label, field.value_name) for field in form.fields] == [
        ("#serie", "Serie", "serie"),
        ("#total", "Monto Total", "total"),
        ("#numero", "N° Correlativo", "numero"),
        ("#fecha", "Fecha de Emisión", "fecha"),
        ("#ruc", "RUC del Emisor", "ruc_emisor"),
    ]


@pytest.mark.asyncio(loop_scope="session")
async def test_labels_are_reduced_to_safe_text(page: Page) -> None:
    """D193: markup in a label is cleaned and a label longer than 60 characters is cut."""
    await page.set_content(
        f"""<html><body><form>
          <label for="a"><b>@x</b> Serie</label><input id="a" />
          <label for="b">{_LONG_LABEL}</label><input id="b" />
        </form></body></html>"""
    )

    form = await read_page(page)

    assert form.fields[0].label == "x Serie"
    assert form.fields[1].label == _LONG_LABEL[:60]
    assert len(form.fields[1].label) == 60


@pytest.mark.asyncio(loop_scope="session")
async def test_duplicate_ids_without_names_leave_no_fields(page: Page) -> None:
    """D193: a control whose id and name match more than one element is left out."""
    await page.set_content(
        """<html><body><form>
          <input id="dup" aria-label="Serie" />
          <input id="dup" aria-label="Monto Total" />
        </form></body></html>"""
    )

    form = await read_page(page)

    assert form.fields == ()
    assert form.submit is None


@pytest.mark.asyncio(loop_scope="session")
async def test_duplicate_id_with_a_unique_name_uses_the_name_selector(page: Page) -> None:
    """D193: a duplicated id falls back to the control's unique name."""
    await page.set_content(
        """<html><body><form>
          <input id="dup" name="serie_field" aria-label="Serie" />
          <input id="dup" aria-label="Monto Total" />
        </form></body></html>"""
    )

    form = await read_page(page)

    assert [(field.selector, field.value_name) for field in form.fields] == [
        ("[name='serie_field']", "serie"),
    ]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_single_matched_control_is_not_a_form(page: Page) -> None:
    """D193: one matched control is not enough for a form."""
    await page.set_content(
        """<html><body><form>
          <label for="serie">Serie</label><input id="serie" />
        </form></body></html>"""
    )

    form = await read_page(page)

    assert form.fields == ()
    assert form.submit is None


@pytest.mark.asyncio(loop_scope="session")
async def test_hidden_and_disabled_controls_are_not_counted(page: Page) -> None:
    """D193: a hidden or disabled control never makes a second match."""
    await page.set_content(
        """<html><body><form>
          <label for="serie">Serie</label><input id="serie" />
          <label for="total">Monto Total</label><input id="total" style="display: none" />
          <label for="numero">N° Correlativo</label><input id="numero" disabled />
          <label for="ruc">RUC del Emisor</label><input id="ruc" type="hidden" />
        </form></body></html>"""
    )

    form = await read_page(page)

    assert form.fields == ()


@pytest.mark.asyncio(loop_scope="session")
async def test_unmatched_visible_control_is_listed_without_a_value(page: Page) -> None:
    """D193: every visible control of the form is listed, even one that matches nothing."""
    await page.set_content(
        """<html><body><form>
          <label for="serie">Serie</label><input id="serie" />
          <label for="total">Monto Total</label><input id="total" />
          <label for="nota">Comentario</label><input id="nota" />
        </form></body></html>"""
    )

    form = await read_page(page)

    assert [(field.selector, field.value_name) for field in form.fields] == [
        ("#serie", "serie"),
        ("#total", "total"),
        ("#nota", None),
    ]


@pytest.mark.asyncio(loop_scope="session")
async def test_the_second_form_is_used_when_the_first_is_a_search_box(page: Page) -> None:
    """D193: the first group with two matched controls is the form, not the first form."""
    await page.set_content(
        """<html><body>
        <form id="search">
          <input id="q" name="q" placeholder="Buscar comprobante" />
          <button id="go" type="submit">Buscar</button>
        </form>
        <form id="consulta">
          <label for="serie">Serie</label><input id="serie" />
          <label for="total">Monto Total</label><input id="total" />
          <button id="submit" type="submit">Consultar</button>
        </form>
        </body></html>"""
    )

    form = await read_page(page)

    assert [field.selector for field in form.fields] == ["#serie", "#total"]
    assert form.submit == "#submit"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_visible_password_field_is_reported(page: Page) -> None:
    """D193: a page with a password input reports it, even outside a real form."""
    await page.set_content(
        """<html><body><form>
          <label for="user">Usuario</label><input id="user" />
          <label for="pass">Clave</label><input id="pass" type="password" />
        </form></body></html>"""
    )

    form = await read_page(page)

    assert form.has_password is True
    assert form.fields == ()


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        ('<img id="captcha-image" src="/captcha.png" />', "image"),
        ('<div class="g-recaptcha"></div>', "recaptcha"),
        ('<iframe src="https://www.google.com/recaptcha/api2/anchor"></iframe>', "recaptcha"),
        ('<iframe src="https://hcaptcha.com/1/api.js"></iframe>', "hcaptcha"),
    ],
)
@pytest.mark.asyncio(loop_scope="session")
async def test_captcha_kind_is_read_without_visibility(page: Page, markup: str, expected: str) -> None:
    """D193: an image, a reCAPTCHA or an hCaptcha widget names the page's captcha."""
    aborted: list[str] = []

    async def abort(route: Route) -> None:
        aborted.append(route.request.url)
        await route.abort()

    await page.route("**/*", abort)
    await page.set_content(f"<html><body>{markup}</body></html>")

    form = await read_page(page)

    assert form.captcha == expected


@pytest.mark.parametrize(
    ("buttons", "expected"),
    [
        (
            '<button id="clear" type="submit">Limpiar</button>'
            '<button id="search" type="submit">Buscar</button>',
            "#search",
        ),
        (
            '<button id="clear" type="submit">Limpiar</button>'
            '<button id="cancel" type="submit">Cancelar</button>',
            None,
        ),
    ],
)
@pytest.mark.asyncio(loop_scope="session")
async def test_the_one_submit_word_picks_the_button(page: Page, buttons: str, expected: str | None) -> None:
    """D193: with several submit controls, only the one whose text says buscar wins."""
    await page.set_content(
        f"""<html><body><form>
          <label for="serie">Serie</label><input id="serie" />
          <label for="total">Monto Total</label><input id="total" />
          {buttons}
        </form></body></html>"""
    )

    form = await read_page(page)

    assert form.submit == expected


def test_safe_text_keeps_only_the_allow_list() -> None:
    """D193: a character outside the allow-list becomes a space and the text collapses."""
    assert safe_text("Serie @#<b>") == "Serie b"
    assert safe_text("  Monto   Total  ") == "Monto Total"


def test_safe_text_is_cut_to_sixty_characters() -> None:
    """D193: a longer text keeps its first 60 characters and still fits the pattern."""
    cut = safe_text("a" * 61)

    assert cut == "a" * 60
    assert len(cut) == 60
    assert re.fullmatch(SAFE_TEXT_PATTERN, cut)


def test_safe_text_keeps_accents_and_composes_them() -> None:
    """D193: accented letters survive and decomposed text is NFC normalised."""
    assert safe_text("Emisión N°") == "Emisión N°"
    assert safe_text("Emisión") == "Emisión"
