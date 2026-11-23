"""Tests for the QR document URL allowlist and its fetcher (R-5.2, R-5.6, R-5.10)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from invoicebot.config.models import MediaMaxBytes, Portal, StoreDirectory
from invoicebot.fetchers.base import FetchInvoice, FetchReason
from invoicebot.fetchers.qr_url import MAX_REDIRECTS, QrUrlFetcher, allowed_hosts, is_allowed

# From tests/synthetic_rucs.txt.
SYNTHETIC_RUC = "20000000001"
SERIE = "F001"
NUMERO = "123"
OTHER_NUMERO = "999"
TOTAL = Decimal("118.50")
FECHA = date(2026, 11, 13)
START_URL = "https://portal.example/start"
DOCUMENT_URL = "https://portal.example/invoice"
EVIL_URL = "https://evil.example/invoice"
HOSTS = frozenset({"portal.example"})
PDF = b"%PDF-1.4 synthetic"
MEDIA_MAX_BYTES = MediaMaxBytes(
    image=10 * 1024 * 1024,
    pdf=1024,
    xml=2048,
    zip=20 * 1024 * 1024,
)

XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>F001-{numero}</cbc:ID>
  <cbc:IssueDate>2026-11-13</cbc:IssueDate>
  <cbc:InvoiceTypeCode>01</cbc:InvoiceTypeCode>
  <cac:AccountingSupplierParty>
    <cac:Party>
      <cac:PartyIdentification>
        <cbc:ID schemeID="6">20000000001</cbc:ID>
      </cac:PartyIdentification>
      <cac:PartyLegalEntity>
        <cbc:RegistrationName>COMERCIO DE PRUEBA S.A.C.</cbc:RegistrationName>
      </cac:PartyLegalEntity>
    </cac:Party>
  </cac:AccountingSupplierParty>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="PEN">100.42</cbc:LineExtensionAmount>
    <cbc:PayableAmount currencyID="PEN">118.50</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
</Invoice>
"""
DOCTYPE = '<!DOCTYPE Invoice [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'

Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


def _portal(key: str, url: str | None) -> Portal:
    return Portal.model_validate({"key": key, "adapter": "generic_form", "url": url})


def _directory(*portals: Portal) -> StoreDirectory:
    return StoreDirectory(stores={}, portals={portal.key: portal for portal in portals})


def _invoice(document_url: str | None = START_URL, numero: str = NUMERO) -> FetchInvoice:
    """Return the invoice the QR URL should carry, with synthetic values."""
    return FetchInvoice(
        ruc_emisor=SYNTHETIC_RUC,
        tipo="01",
        serie=SERIE,
        numero=numero,
        fecha=FECHA,
        total=TOTAL,
        document_url=document_url,
    )


def _xml(numero: str = NUMERO, *, doctype: bool = False) -> bytes:
    """Return a UBL invoice of the configured shape, optionally with a DOCTYPE line."""
    document = XML_TEMPLATE.format(numero=numero)
    if doctype:
        document = document.replace("\n", f"\n{DOCTYPE}\n", 1)
    return document.encode("utf-8")


@contextlib.asynccontextmanager
async def _make_fetcher(
    handler: Handler,
    *,
    hosts: frozenset[str] = HOSTS,
    max_bytes: MediaMaxBytes = MEDIA_MAX_BYTES,
    timeout_s: float = 5.0,
) -> AsyncIterator[QrUrlFetcher]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        yield QrUrlFetcher(http, hosts, max_bytes, timeout_s)


@pytest.mark.parametrize(
    ("url", "hosts", "expected"),
    [
        ("https://e-factura.sunat.gob.pe/x", frozenset(), True),
        ("https://sunat.gob.pe.evil.test/", frozenset(), False),
        ("https://evilsunat.gob.pe/", frozenset(), False),
        ("https://portal.example/consulta", frozenset({"portal.example"}), True),
        ("http://portal.example/consulta", frozenset({"portal.example"}), False),
        ("https://user@e-factura.sunat.gob.pe/x", frozenset(), False),
        ("https://[::1", frozenset(), False),
    ],
)
def test_is_allowed_url_decides_by_scheme_and_host(url: str, hosts: frozenset[str], expected: bool) -> None:
    """R-5.2, R-5.10: only https URLs on SUNAT or an allowed host pass."""
    assert is_allowed(url, hosts) is expected


def test_allowed_hosts_lowercases_and_skips_portals_without_url() -> None:
    """R-5.2, R-5.10: every portal URL contributes its lowercased host, if any."""
    directory = _directory(
        _portal("upper", "HTTPS://Portal.Example/consulta"),
        _portal("missing", None),
    )

    assert allowed_hosts(directory) == frozenset({"portal.example"})


async def test_fetch_without_document_url_is_no_url(tmp_path: Path) -> None:
    """R-5.2, R-5.6: an invoice with no QR URL is refused before any request."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=PDF)

    async with _make_fetcher(handler) as fetcher:
        result = await fetcher.fetch(_invoice(document_url=None), tmp_path)

    assert result.reason is FetchReason.NO_URL
    assert requested == []


async def test_fetch_disallowed_host_is_guard_blocked_without_a_request(tmp_path: Path) -> None:
    """R-5.2, R-5.6: a URL outside the allowlist is refused and never requested."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=PDF)

    async with _make_fetcher(handler) as fetcher:
        result = await fetcher.fetch(_invoice(document_url=EVIL_URL), tmp_path)

    assert result.reason is FetchReason.GUARD_BLOCKED
    assert requested == []


async def test_fetch_pdf_writes_the_file_and_its_sha256(tmp_path: Path) -> None:
    """R-5.2, R-5.6: a PDF body comes back as one file under its kind with its digest."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=PDF)

    async with _make_fetcher(handler) as fetcher:
        result = await fetcher.fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.OK
    assert result.portal_key is None
    (file,) = result.files
    assert file.kind == "pdf"
    assert file.path == tmp_path / "pdf.pdf"
    assert file.path.read_bytes() == PDF
    assert file.size == len(PDF)
    assert file.sha256 == hashlib.sha256(PDF).hexdigest()


async def test_fetch_matching_xml_is_ok(tmp_path: Path) -> None:
    """R-5.2, R-5.6: an XML that matches the invoice comes back as one file."""
    body = _xml()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with _make_fetcher(handler) as fetcher:
        result = await fetcher.fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.OK
    (file,) = result.files
    assert file.kind == "xml"
    assert file.path == tmp_path / "xml.xml"
    assert file.sha256 == hashlib.sha256(body).hexdigest()


async def test_fetch_mismatching_xml_is_xml_mismatch(tmp_path: Path) -> None:
    """R-5.2, R-5.6: an XML for another numero is reported as a mismatch."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_xml(OTHER_NUMERO))

    async with _make_fetcher(handler) as fetcher:
        result = await fetcher.fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.XML_MISMATCH


async def test_fetch_xml_with_a_doctype_is_xml_invalid(tmp_path: Path) -> None:
    """R-5.2, R-5.6: a non-HTML DOCTYPE keeps the body an XML and the parser rejects it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_xml(doctype=True))

    async with _make_fetcher(handler) as fetcher:
        result = await fetcher.fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.XML_INVALID


async def test_fetch_html_page_is_not_found(tmp_path: Path) -> None:
    """R-5.2, R-5.6: an HTML answer means the source holds no document."""
    body = b"<!DOCTYPE html><html><body>No hay comprobante</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with _make_fetcher(handler) as fetcher:
        result = await fetcher.fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.NOT_FOUND


async def test_fetch_body_over_the_cap_is_technical(tmp_path: Path) -> None:
    """R-5.2, R-5.6: a body past the larger kind cap is refused while reading."""
    body = b"%PDF-" + b"x" * (max(MEDIA_MAX_BYTES.pdf, MEDIA_MAX_BYTES.xml) + 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with _make_fetcher(handler) as fetcher:
        result = await fetcher.fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.TECHNICAL


async def test_fetch_redirect_to_an_allowed_host_is_followed(tmp_path: Path) -> None:
    """R-5.2, R-5.6: a redirect on an allowed host is re-checked and followed."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if str(request.url) == START_URL:
            return httpx.Response(302, headers={"location": DOCUMENT_URL})
        return httpx.Response(200, content=PDF)

    async with _make_fetcher(handler) as fetcher:
        result = await fetcher.fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.OK
    assert requested == [START_URL, DOCUMENT_URL]


async def test_fetch_ok_logs_no_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """R-5.2, R-5.6, R-5.7: a successful fetch logs no warning."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=PDF)

    async with _make_fetcher(handler) as fetcher:
        with caplog.at_level(logging.WARNING, logger="invoicebot.fetchers.qr_url"):
            result = await fetcher.fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.OK
    assert [record for record in caplog.records if record.name == "invoicebot.fetchers.qr_url"] == []


async def test_fetch_redirect_to_a_disallowed_host_is_guard_blocked(tmp_path: Path) -> None:
    """R-5.2, R-5.6: a redirect outside the allowlist is refused and its target not requested."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": EVIL_URL})

    async with _make_fetcher(handler) as fetcher:
        result = await fetcher.fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.GUARD_BLOCKED
    assert requested == [START_URL]


async def test_fetch_more_than_max_redirects_is_guard_blocked(tmp_path: Path) -> None:
    """R-5.2, R-5.6: a chain longer than the limit is refused as a loop."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": f"https://portal.example/{len(requested)}"})

    async with _make_fetcher(handler) as fetcher:
        result = await fetcher.fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.GUARD_BLOCKED
    assert len(requested) == MAX_REDIRECTS + 1


async def test_fetch_server_error_is_technical_and_logs_its_status(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """R-5.2, R-5.6, R-5.7: a non-200 answer is a technical failure, logged with its status only."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with _make_fetcher(handler) as fetcher:
        with caplog.at_level(logging.WARNING, logger="invoicebot.fetchers.qr_url"):
            result = await fetcher.fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.TECHNICAL
    records = [record for record in caplog.records if record.getMessage() == "qr url fetch failed"]
    assert len(records) == 1
    assert records[0].status == 500


async def test_fetch_connect_error_logs_one_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """R-5.2, R-5.6, R-5.7: an HTTP failure logs one warning with the exception class."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic connect failure")

    async with _make_fetcher(handler) as fetcher:
        with caplog.at_level(logging.WARNING, logger="invoicebot.fetchers.qr_url"):
            result = await fetcher.fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.TECHNICAL
    records = [record for record in caplog.records if record.getMessage() == "qr url fetch failed"]
    assert len(records) == 1
    assert records[0].error == "ConnectError"


async def test_fetch_timeout_is_timeout(tmp_path: Path) -> None:
    """R-5.2, R-5.6: a timed-out request is reported as a timeout."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout")

    async with _make_fetcher(handler) as fetcher:
        result = await fetcher.fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.TIMEOUT


async def test_fetch_timeout_logs_one_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """R-5.2, R-5.6, R-5.7: a timed-out request logs one warning with the exception class."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout")

    async with _make_fetcher(handler) as fetcher:
        with caplog.at_level(logging.WARNING, logger="invoicebot.fetchers.qr_url"):
            result = await fetcher.fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.TIMEOUT
    records = [record for record in caplog.records if record.getMessage() == "qr url fetch timed out"]
    assert len(records) == 1
    assert records[0].error == "ReadTimeout"


async def test_fetch_past_the_deadline_is_timeout(tmp_path: Path) -> None:
    """R-5.1: a download that outlives the overall deadline is reported as a timeout."""

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, content=PDF)

    async with _make_fetcher(handler, timeout_s=0.01) as fetcher:
        result = await fetcher.fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.TIMEOUT
