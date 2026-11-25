"""Integration tests for reaching a portal's form (D193)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from playwright.async_api import Browser, Page, Route, async_playwright

from invoicebot.fetchers.browser import probe_form
from invoicebot.fetchers.browser.probe_form import read_form
from invoicebot.fetchers.browser.probe_protocol import ProbeOutcome

_PORTAL = "https://portal.example.test"
_FORM = """<!doctype html>
<html>
<body>
<form id="consulta">
  <label for="tipo">Tipo de Documento</label>
  <select id="tipo" name="tipo">
    <option>Seleccione</option>
    <option>FACTURA</option>
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
_MENU = (
    '<div id="slot"></div><button id="menu">Consultar</button>'
    "<script>document.getElementById('menu').onclick = () => {"
    f"document.getElementById('slot').innerHTML = {json.dumps(_FORM)};"
    "};</script>"
)
_DELAYED_FORM = (
    "<!doctype html><html><body><div id='root'></div>"
    "<script>setTimeout(() => { document.getElementById('root').innerHTML = "
    f"{json.dumps(_FORM)}; }}, 100);</script></body></html>"
)
_LOGIN = (
    '<form><label for="user">Usuario</label><input id="user" />'
    '<label for="pass">Clave</label><input id="pass" type="password" /></form>'
)


@dataclass(frozen=True)
class _Served:
    """One synthetic answer the portal gives for a path (D193)."""

    body: str = ""
    status: int = 200
    redirect: str | None = None


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


@pytest.fixture(autouse=True)
def _short_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """D193: shorten the settle window so a landing page costs 300 ms, not 2 s."""
    monkeypatch.setattr(probe_form, "SETTLE_MS", 300)


async def _serve(page: Page, routes: Mapping[str, _Served], requested: list[str]) -> None:
    """Answer every request from *routes* by path and record each URL (D193)."""

    async def handle(route: Route) -> None:
        url = route.request.url
        requested.append(url)
        served = routes.get(urlsplit(url).path)
        if served is None:
            await route.fulfill(status=404, body=b"not found")
            return
        if served.redirect is not None:
            # Playwright 1.64 does not route a fulfilled 302's hop, so redirect the
            # navigation through Playwright itself; the page still lands elsewhere.
            await route._impl_obj._redirected_navigation_request(served.redirect)
            return
        if served.body:
            await route.fulfill(
                status=served.status,
                body=served.body.encode(),
                content_type="text/html; charset=utf-8",
            )
            return
        await route.fulfill(status=served.status)

    await page.route("**/*", handle)


def _paths(urls: list[str]) -> list[str]:
    """Return the paths of the requested URLs (D193)."""
    return [urlsplit(url).path for url in urls]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_direct_form_is_read(page: Page) -> None:
    """D193: a page that already shows the form gives its fields, submit and path."""
    requested: list[str] = []
    await _serve(page, {"/consulta": _Served(body=_FORM)}, requested)

    found = await read_form(page, f"{_PORTAL}/consulta")

    assert found.outcome is None
    assert found.form_path == "/consulta"
    assert found.captcha == "none"
    assert [field.value_name for field in found.fields] == ["tipo", "serie", "numero", "fecha", "total"]
    assert found.submit == "#buscar"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_menu_button_reveals_the_form(page: Page) -> None:
    """D193: a landing link then a menu button reach the form, whose path has no query."""
    requested: list[str] = []
    await _serve(
        page,
        {
            "/": _Served(body='<a href="/menu">Consulta</a>'),
            "/menu": _Served(body=_MENU),
            "/consulta": _Served(body=_FORM),
        },
        requested,
    )

    found = await read_form(page, f"{_PORTAL}/?x=1")

    assert found.outcome is None
    assert found.form_path == "/menu"


@pytest.mark.asyncio(loop_scope="session")
async def test_the_form_path_has_no_query(page: Page) -> None:
    """D193: a form reached with a query reports only its path."""
    requested: list[str] = []
    await _serve(
        page,
        {
            "/": _Served(body='<a href="/consulta?x=1">Consulta</a>'),
            "/consulta": _Served(body=_FORM),
        },
        requested,
    )

    found = await read_form(page, _PORTAL)

    assert found.outcome is None
    assert found.form_path == "/consulta"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_session_address_is_dropped_from_the_path(page: Page) -> None:
    """D193: an ASP.NET session segment is not part of the reported form path."""
    requested: list[str] = []
    await _serve(page, {"/(S(abc))/frm.aspx": _Served(body=_FORM)}, requested)

    found = await read_form(page, f"{_PORTAL}/(S(abc))/frm.aspx")

    assert found.outcome is None
    assert found.form_path == "/frm.aspx"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_form_rendered_after_load_is_found(page: Page) -> None:
    """D193: a form the page scripts in after 100 ms is still found."""
    requested: list[str] = []
    await _serve(page, {"/": _Served(body=_DELAYED_FORM)}, requested)

    found = await read_form(page, _PORTAL)

    assert found.outcome is None
    assert [field.value_name for field in found.fields] == ["tipo", "serie", "numero", "fecha", "total"]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_fourth_click_is_not_made(page: Page) -> None:
    """D193: after three landing clicks a page without a form is no_form."""
    requested: list[str] = []
    await _serve(
        page,
        {
            "/": _Served(body='<a href="/one">Consulta</a>'),
            "/one": _Served(body='<a href="/two">Consulta</a>'),
            "/two": _Served(body='<a href="/three">Consulta</a>'),
            "/three": _Served(body='<a href="/four">Consulta</a>'),
            "/four": _Served(body=_FORM),
        },
        requested,
    )

    found = await read_form(page, _PORTAL)

    assert found.outcome is ProbeOutcome.NO_FORM
    assert "/four" not in _paths(requested)


@pytest.mark.parametrize(
    "link_text",
    ["Pagar", "Iniciar sesión para consultar", "Regístrate para consultar"],
)
@pytest.mark.asyncio(loop_scope="session")
async def test_a_refused_control_is_never_followed(page: Page, link_text: str) -> None:
    """D193: pagar, sesion and a bare registr keep the probe off a control."""
    requested: list[str] = []
    await _serve(
        page,
        {
            "/": _Served(body=f'<a href="/consulta">{link_text}</a>'),
            "/consulta": _Served(body=_FORM),
        },
        requested,
    )

    found = await read_form(page, _PORTAL)

    assert found.outcome is ProbeOutcome.NO_FORM
    assert "/consulta" not in _paths(requested)


@pytest.mark.asyncio(loop_scope="session")
async def test_sin_registro_is_a_way_in(page: Page) -> None:
    """D193: "Consulta sin registro" is followed and does not count as registr."""
    requested: list[str] = []
    await _serve(
        page,
        {
            "/": _Served(body='<a href="/consulta">Consulta sin registro</a>'),
            "/consulta": _Served(body=_FORM),
        },
        requested,
    )

    found = await read_form(page, _PORTAL)

    assert found.outcome is None
    assert found.form_path == "/consulta"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_login_page_is_login_only(page: Page) -> None:
    """D193: user and password without a form is login_only."""
    requested: list[str] = []
    await _serve(page, {"/": _Served(body=_LOGIN)}, requested)

    found = await read_form(page, _PORTAL)

    assert found.outcome is ProbeOutcome.LOGIN_ONLY


@pytest.mark.asyncio(loop_scope="session")
async def test_a_login_page_with_a_sin_registro_link_reaches_the_form(page: Page) -> None:
    """D193: a login page whose "Consulta sin registro" link leads to the form is followed."""
    requested: list[str] = []
    await _serve(
        page,
        {
            "/": _Served(body=_LOGIN + '<a href="/consulta">Consulta sin registro</a>'),
            "/consulta": _Served(body=_FORM),
        },
        requested,
    )

    found = await read_form(page, _PORTAL)

    assert found.outcome is None
    assert found.form_path == "/consulta"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_form_with_a_password_field_is_login_only(page: Page) -> None:
    """D193: a form that also asks for a password is login_only."""
    requested: list[str] = []
    body = _FORM.replace("</form>", '<input id="pass" type="password" /></form>')
    await _serve(page, {"/": _Served(body=body)}, requested)

    found = await read_form(page, _PORTAL)

    assert found.outcome is ProbeOutcome.LOGIN_ONLY


@pytest.mark.asyncio(loop_scope="session")
async def test_a_redirect_to_another_host_is_other_host(page: Page) -> None:
    """D193: a redirect off the approved host is other_host."""
    requested: list[str] = []
    await _serve(
        page,
        {
            "/": _Served(redirect="https://other.example.test/landing"),
            "/landing": _Served(body="<html><body>other</body></html>"),
        },
        requested,
    )

    found = await read_form(page, _PORTAL)

    assert found.outcome is ProbeOutcome.OTHER_HOST
    assert "https://other.example.test/landing" in requested


@pytest.mark.asyncio(loop_scope="session")
async def test_a_link_to_another_host_is_not_requested(page: Page) -> None:
    """D193: a "Consulta" link off the approved host is never followed."""
    requested: list[str] = []
    await _serve(
        page,
        {
            "/": _Served(body='<a href="https://other.example.test/form">Consulta</a>'),
            "/form": _Served(body=_FORM),
        },
        requested,
    )

    found = await read_form(page, _PORTAL)

    assert found.outcome is ProbeOutcome.NO_FORM
    assert not any("other.example.test" in url for url in requested)


@pytest.mark.asyncio(loop_scope="session")
async def test_a_link_to_http_is_not_requested(page: Page) -> None:
    """D193: a link that is not https is never followed."""
    requested: list[str] = []
    await _serve(
        page,
        {
            "/": _Served(body='<a href="http://portal.example.test/form">Consulta</a>'),
            "/form": _Served(body=_FORM),
        },
        requested,
    )

    found = await read_form(page, _PORTAL)

    assert found.outcome is ProbeOutcome.NO_FORM
    assert not any(url.startswith("http://") for url in requested)


@pytest.mark.asyncio(loop_scope="session")
async def test_a_subdomain_link_is_followed(page: Page) -> None:
    """D193: an https link on an approved subdomain is followed."""
    requested: list[str] = []
    await _serve(
        page,
        {
            "/": _Served(body='<a href="https://sub.portal.example.test/form">Consulta</a>'),
            "/form": _Served(body=_FORM),
        },
        requested,
    )

    found = await read_form(page, _PORTAL)

    assert found.outcome is None
    assert found.form_path == "/form"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_target_blank_link_is_opened_in_this_page(page: Page) -> None:
    """D193: a target=_blank link is opened with goto, not a new tab."""
    requested: list[str] = []
    await _serve(
        page,
        {
            "/": _Served(body='<a href="/consulta" target="_blank">Consultar</a>'),
            "/consulta": _Served(body=_FORM),
        },
        requested,
    )

    found = await read_form(page, _PORTAL)

    assert found.outcome is None
    assert found.form_path == "/consulta"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_javascript_link_is_clicked(page: Page) -> None:
    """D193: a javascript: link is clicked so its navigation runs."""
    requested: list[str] = []
    await _serve(
        page,
        {
            "/": _Served(body="""<a href="javascript:location.href='/consulta'">Consultar</a>"""),
            "/consulta": _Served(body=_FORM),
        },
        requested,
    )

    found = await read_form(page, _PORTAL)

    assert found.outcome is None
    assert found.form_path == "/consulta"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_submit_button_inside_a_form_is_not_clicked(page: Page) -> None:
    """D193: a submit control of a form with a text input is never clicked."""
    requested: list[str] = []
    await _serve(
        page,
        {
            "/": _Served(
                body=(
                    '<form><input name="q" />'
                    "<button onclick=\"location.href='/next'\">Consultar</button></form>"
                )
            )
        },
        requested,
    )

    found = await read_form(page, _PORTAL)

    assert found.outcome is ProbeOutcome.NO_FORM
    assert "/next" not in _paths(requested)


@pytest.mark.parametrize(
    ("status", "outcome"),
    [(503, ProbeOutcome.BUSY), (429, ProbeOutcome.BUSY), (404, ProbeOutcome.TECHNICAL)],
)
@pytest.mark.asyncio(loop_scope="session")
async def test_a_bad_initial_answer_sets_the_outcome(page: Page, status: int, outcome: ProbeOutcome) -> None:
    """D193: 429 and 5xx are busy; another non-2xx is technical."""
    await _serve(page, {"/": _Served(status=status)}, [])

    found = await read_form(page, _PORTAL)

    assert found.outcome is outcome


@pytest.mark.asyncio(loop_scope="session")
async def test_a_followed_link_answering_503_is_busy(page: Page) -> None:
    """D193: the status rule applies to a page reached through a link too."""
    requested: list[str] = []
    await _serve(
        page,
        {
            "/": _Served(body='<a href="/consulta">Consulta</a>'),
            "/consulta": _Served(status=503),
        },
        requested,
    )

    found = await read_form(page, _PORTAL)

    assert found.outcome is ProbeOutcome.BUSY


@pytest.mark.parametrize(
    ("markup", "captcha"),
    [
        ('<img id="captcha-image" src="/captcha.png" />', "image"),
        ('<div class="g-recaptcha"></div>', "recaptcha"),
        ('<iframe src="https://hcaptcha.com/1/api.js"></iframe>', "hcaptcha"),
    ],
)
@pytest.mark.asyncio(loop_scope="session")
async def test_a_captcha_beside_the_form_is_reported(page: Page, markup: str, captcha: str) -> None:
    """D193: the form is found and its captcha kind reported, not decided."""
    requested: list[str] = []
    body = _FORM.replace("</body>", markup + "</body>")
    await _serve(page, {"/": _Served(body=body)}, requested)

    found = await read_form(page, _PORTAL)

    assert found.outcome is None
    assert found.captcha == captcha


@pytest.mark.asyncio(loop_scope="session")
async def test_a_marked_up_label_is_cleaned(page: Page) -> None:
    """D193: markup in a label is cleaned before it reaches the report."""
    requested: list[str] = []
    body = """<form>
      <label for="serie"><b>@x</b> Serie</label><input id="serie" />
      <label for="total">Monto Total</label><input id="total" />
    </form>"""
    await _serve(page, {"/": _Served(body=body)}, requested)

    found = await read_form(page, _PORTAL)

    assert found.outcome is None
    assert found.fields[0].label == "x Serie"
    assert found.fields[0].value_name == "serie"


@pytest.mark.asyncio(loop_scope="session")
async def test_two_controls_with_the_same_id_are_left_out(page: Page) -> None:
    """D193: a control whose selector matches two elements is not a field."""
    requested: list[str] = []
    body = """<form>
      <input id="dup" aria-label="Serie" />
      <input id="dup" aria-label="Monto Total" />
      <input id="numero" aria-label="N° Correlativo" />
    </form>"""
    await _serve(page, {"/": _Served(body=body)}, requested)

    found = await read_form(page, _PORTAL)

    assert found.outcome is None
    assert [(field.selector, field.value_name) for field in found.fields] == [("#numero", "numero")]
