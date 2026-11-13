"""Integration tests for the generic form filler (R-5.8, S10.2)."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from playwright.async_api import Browser, Page, Route, async_playwright

from invoicebot.config.models import (
    CalendarField,
    CalendarSpec,
    MediaMaxBytes,
    PickField,
    Portal,
    PortalDownloads,
    SelectField,
)
from invoicebot.fetchers.base import FetchInvoice, FetchReason
from invoicebot.fetchers.browser.form import fill_and_download, render_fields

PDF_BYTES = b"%PDF-1.4 synthetic invoice\n"
XML_BYTES = b"<invoice><id>F001-123</id></invoice>\n"
LARGE_XML_BYTES = b"x" * (3 * 1024 * 1024)
MAX_BYTES = MediaMaxBytes(
    image=10 * 1024 * 1024,
    pdf=1024 * 1024,
    xml=2 * 1024 * 1024,
    zip=10 * 1024 * 1024,
)

_RESULTS = {
    "match": '<a id="pdf" href="/files/doc.pdf">PDF</a><a id="xml" href="/files/doc.xml">XML</a>',
    "no_xml": '<a id="pdf" href="/files/doc.pdf">PDF</a>',
    "not_found": '<div id="nf">No encontrado</div>',
    "large_xml": '<a id="pdf" href="/files/doc.pdf">PDF</a><a id="xml" href="/files/doc.xml">XML</a>',
}

_FORM_HTML = """<!doctype html>
<html>
<body>
<form id="form">
  <input id="total" />
  <select id="serie">
    <option value="F001">F001</option>
    <option value="F002">F002</option>
  </select>
  <div id="tipo">Tipo</div>
  <div id="tipo-list" style="display: none">
    <div class="opt" title="Factura">Factura</div>
  </div>
  <input id="fecha" readonly />
  <div id="cal" style="display: none">
    <button type="button" id="prev">prev</button>
    <span id="cal-month"></span>
    <table><tbody id="days"></tbody></table>
  </div>
  <button id="submit" type="submit">Buscar</button>
</form>
<template id="results">__RESULTS__</template>
<script>
let shownMonth = new Date(new Date().getFullYear(), new Date().getMonth(), 1);
let pickedTipo = "";
let pickedFecha = "";

function pad(value) {
  return String(value).padStart(2, "0");
}

function renderCalendar() {
  document.getElementById("cal-month").textContent =
    shownMonth.getFullYear() + "-" + pad(shownMonth.getMonth() + 1);
  const days = document.getElementById("days");
  days.innerHTML = "";
  const lastDay = new Date(shownMonth.getFullYear(), shownMonth.getMonth() + 1, 0).getDate();
  for (let day = 1; day <= lastDay; day++) {
    const iso = shownMonth.getFullYear() + "-" + pad(shownMonth.getMonth() + 1) + "-" + pad(day);
    const cell = document.createElement("td");
    cell.className = "day";
    cell.title = iso;
    cell.textContent = String(day);
    cell.addEventListener("click", () => {
      pickedFecha = iso;
      document.getElementById("fecha").value = iso;
      document.getElementById("cal").style.display = "none";
    });
    days.appendChild(cell);
  }
}

document.getElementById("tipo").addEventListener("click", () => {
  document.getElementById("tipo-list").style.display = "block";
});
document.querySelectorAll("#tipo-list .opt").forEach((option) => {
  option.addEventListener("click", () => {
    pickedTipo = option.title;
    document.getElementById("tipo").textContent = pickedTipo;
    document.getElementById("tipo-list").style.display = "none";
  });
});
document.getElementById("fecha").addEventListener("click", () => {
  renderCalendar();
  document.getElementById("cal").style.display = "block";
});
document.getElementById("prev").addEventListener("click", () => {
  shownMonth = new Date(shownMonth.getFullYear(), shownMonth.getMonth() - 1, 1);
  renderCalendar();
});
document.getElementById("form").addEventListener("submit", (event) => {
  event.preventDefault();
  window.received = {
    total: document.getElementById("total").value,
    serie: document.getElementById("serie").value,
    tipo_label: pickedTipo,
    fecha: pickedFecha,
  };
  document.body.innerHTML = document.getElementById("results").innerHTML;
});
</script>
</body>
</html>
"""


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def browser() -> AsyncIterator[Browser]:
    """A headless Chromium for this module's tests."""
    async with async_playwright() as playwright:
        launched = await playwright.chromium.launch()
        yield launched
        await launched.close()


@pytest_asyncio.fixture(loop_scope="session")
async def page(browser: Browser) -> AsyncIterator[Page]:
    """A fresh page with a short timeout and downloads enabled."""
    fresh = await browser.new_page(accept_downloads=True)
    fresh.set_default_timeout(3000)
    yield fresh
    await fresh.close()


def _today() -> date:
    """Today's date in UTC, which also drives the page's month widget."""
    return datetime.now(tz=UTC).date()


def _two_months_ago(today: date) -> date:
    """The 15th of the month two months before ``today``."""
    month_index = today.year * 12 + today.month - 1 - 2
    year, month = divmod(month_index, 12)
    return date(year, month + 1, 15)


def _invoice(tipo: str = "01", fecha: date | None = None) -> FetchInvoice:
    """The synthetic invoice every test fills in."""
    return FetchInvoice(
        ruc_emisor="20000000001",
        tipo=tipo,
        serie="F001",
        numero="123",
        fecha=_two_months_ago(_today()) if fecha is None else fecha,
        total=Decimal("118.5"),
    )


def _portal(**overrides: object) -> Portal:
    """A portal file covering every field kind the filler supports."""
    fields: dict[str, object] = {
        "#total": "{total:.2f}",
        "#serie": SelectField(select="{serie}"),
        "#tipo": PickField(pick="#tipo-list .opt[title='{tipo_label}']"),
        "#fecha": CalendarField(calendar=CalendarSpec(cell=".day[title='{fecha:%Y-%m-%d}']", prev="#prev")),
    }
    defaults: dict[str, object] = {
        "key": "test-portal",
        "adapter": "generic_form",
        "status": "active",
        "url": "https://portal.test/form",
        "tipo_labels": {"01": "Factura"},
        "fields": fields,
        "submit": "#submit",
        "downloads": PortalDownloads(pdf="#pdf", xml="#xml"),
        "not_found": "#nf",
    }
    return Portal.model_validate({**defaults, **overrides})


async def _serve(page: Page, mode: str) -> None:
    """Serve the synthetic form and files for one test's outcome."""

    async def handle(route: Route) -> None:
        await _fulfill(route, mode)

    await page.route("https://portal.test/**", handle)


async def _fulfill(route: Route, mode: str) -> None:
    """Answer one request from the synthetic portal."""
    path = urlparse(route.request.url).path
    if path == "/form":
        body = _FORM_HTML.replace("__RESULTS__", _RESULTS[mode]).encode()
        await route.fulfill(status=200, body=body, content_type="text/html")
        return
    if path == "/files/doc.pdf":
        await _serve_bytes(route, PDF_BYTES, "doc.pdf", "application/pdf")
        return
    if path == "/files/doc.xml" and mode != "no_xml":
        body = LARGE_XML_BYTES if mode == "large_xml" else XML_BYTES
        await _serve_bytes(route, body, "doc.xml", "application/xml")
        return
    await route.fulfill(status=404, body=b"not found")


def _files_in(directory: Path) -> list[Path]:
    """The files left in a directory (run off the event loop)."""
    return list(directory.iterdir())


async def _serve_bytes(route: Route, body: bytes, name: str, content_type: str) -> None:
    """Answer one request as a downloadable file."""
    await route.fulfill(
        status=200,
        body=body,
        headers={
            "Content-Type": content_type,
            "Content-Disposition": f'attachment; filename="{name}"',
        },
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_fields_are_filled_as_the_portal_says(page: Page, tmp_path: Path) -> None:
    """R-5.8: every rendered field reaches the page with its portal value."""
    invoice = _invoice()
    await _serve(page, "match")

    result = await fill_and_download(page, _portal(), invoice, tmp_path, MAX_BYTES)

    assert result.reason is FetchReason.OK
    received = await page.evaluate("() => window.received")
    assert received == {
        "total": "118.50",
        "serie": "F001",
        "tipo_label": "Factura",
        "fecha": invoice.fecha.isoformat(),
    }


@pytest.mark.asyncio(loop_scope="session")
async def test_unlabelled_tipo_is_technical(page: Page, tmp_path: Path) -> None:
    """R-5.8: a tipo the portal has no label for is a technical outcome."""
    await _serve(page, "match")

    result = await fill_and_download(page, _portal(tipo_labels={}), _invoice(tipo="03"), tmp_path, MAX_BYTES)

    assert result.reason is FetchReason.TECHNICAL
    assert result.portal_key == "test-portal"


@pytest.mark.asyncio(loop_scope="session")
async def test_match_downloads_pdf_and_xml(page: Page, tmp_path: Path) -> None:
    """R-5.8: a match saves each document with its size and SHA-256."""
    await _serve(page, "match")

    result = await fill_and_download(page, _portal(), _invoice(), tmp_path, MAX_BYTES)

    assert result.reason is FetchReason.OK
    assert [file.kind for file in result.files] == ["pdf", "xml"]
    for file, body in zip(result.files, (PDF_BYTES, XML_BYTES), strict=True):
        assert file.path == tmp_path / file.kind
        assert file.size == len(body)
        assert file.sha256 == hashlib.sha256(body).hexdigest()


@pytest.mark.asyncio(loop_scope="session")
async def test_not_found_page_is_not_found_yet(page: Page, tmp_path: Path) -> None:
    """R-5.8: the portal's not-found marker means not found yet, not a failure."""
    await _serve(page, "not_found")

    result = await fill_and_download(page, _portal(), _invoice(), tmp_path, MAX_BYTES)

    assert result.reason is FetchReason.NOT_FOUND_YET
    assert result.files == ()


@pytest.mark.asyncio(loop_scope="session")
async def test_missing_xml_control_is_technical(page: Page, tmp_path: Path) -> None:
    """R-5.8: a results page without the XML control is a technical outcome."""
    await _serve(page, "no_xml")

    result = await fill_and_download(page, _portal(), _invoice(), tmp_path, MAX_BYTES)

    assert result.reason is FetchReason.TECHNICAL
    assert await asyncio.to_thread(_files_in, tmp_path) == []


@pytest.mark.asyncio(loop_scope="session")
async def test_oversized_xml_is_technical_and_leaves_no_files(page: Page, tmp_path: Path) -> None:
    """R-5.8: a file over its kind's limit is technical and leaves nothing behind."""
    await _serve(page, "large_xml")

    result = await fill_and_download(page, _portal(), _invoice(), tmp_path, MAX_BYTES)

    assert result.reason is FetchReason.TECHNICAL
    assert await asyncio.to_thread(_files_in, tmp_path) == []


def test_render_fields_rejects_attribute_access() -> None:
    """R-5.8: a template with attribute access is rejected, naming the template."""
    portal = _portal(fields={"#fecha": "{fecha.year}"})

    with pytest.raises(ValueError, match=r"\{fecha\.year\}"):
        render_fields(portal, _invoice())
