"""Integration tests for the ``portal_registry`` fetcher and its browser helper (S10.2, R-5.6)."""

from __future__ import annotations

import hashlib
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from invoicebot.config.models import Brand, MediaMaxBytes, Portal, Store, StoreDirectory
from invoicebot.fetchers.base import FetchInvoice, FetchReason
from invoicebot.fetchers.portal import PortalFetcher

# From tests/synthetic_rucs.txt; the other two have a deliberately wrong check digit.
SYNTHETIC_RUC = "20000000001"
MANUAL_RUC = "20000000002"
TODO_RUC = "20000000003"
UNKNOWN_RUC = "51900000000"
SERIE = "F001"
NUMERO = "123"
OTHER_NUMERO = "999"
TOTAL = Decimal("118.50")
FECHA = date(2026, 11, 13)
HELPER_TIMEOUT_S = 5.0
SLEEP_TIMEOUT_S = 1.0
OUT_DIR_NAME = "out"
XML_NAME = "invoice.xml"
PDF_NAME = "invoice.pdf"
MEDIA_MAX_BYTES = MediaMaxBytes(
    image=10 * 1024 * 1024,
    pdf=10 * 1024 * 1024,
    xml=2 * 1024 * 1024,
    zip=20 * 1024 * 1024,
)

#: A fake helper: it reads the request, writes chosen files and prints one result line.
#: The Playwright module is never imported, so the test needs no browser (R-5.6).
HELPER_SCRIPT = '''\
import hashlib
import json
import sys
import time
from pathlib import Path

MODE = sys.argv[1]
request = json.load(sys.stdin)
out_dir = Path(request["out_dir"])
out_dir.mkdir(parents=True, exist_ok=True)

INVOICE = """<?xml version="1.0" encoding="UTF-8"?>
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


def xml_bytes(numero="123", *, doctype=False, utf16=False):
    document = INVOICE.format(numero=numero)
    if doctype:
        document = '<!DOCTYPE Invoice [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>' + document
    if utf16:
        return document.replace('encoding="UTF-8"', 'encoding="UTF-16"').encode("utf-16-le")
    return document.encode("utf-8")


def write(kind, name, data):
    path = out_dir / name
    path.write_bytes(data)
    return {
        "kind": kind,
        "path": str(path),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def report(reason, files):
    print(json.dumps({"reason": reason, "files": files}))


if MODE == "sleep":
    time.sleep(10)
elif MODE == "match":
    report("ok", [
        write("xml", "invoice.xml", xml_bytes()),
        write("pdf", "invoice.pdf", b"%PDF-1.4 synthetic"),
    ])
elif MODE == "other":
    report("ok", [
        write("xml", "invoice.xml", xml_bytes("999")),
        write("pdf", "invoice.pdf", b"%PDF-1.4 synthetic"),
    ])
elif MODE == "doctype":
    report("ok", [write("xml", "invoice.xml", xml_bytes(doctype=True))])
elif MODE == "utf16":
    report("ok", [write("xml", "invoice.xml", xml_bytes(utf16=True))])
elif MODE == "outside":
    path = out_dir.parent / "outside.xml"
    path.write_bytes(b"<Invoice/>")
    report("ok", [{
        "kind": "xml",
        "path": str(path),
        "size": len(path.read_bytes()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }])
else:
    report("technical", [])
'''


def _store(ruc: str, portal: str | None) -> Store:
    """Return one synthetic store pointing at ``portal``."""
    return Store(
        ruc=ruc,
        razon_social="COMERCIO DE PRUEBA S.A.C.",
        tipo="restaurante",
        brands=[Brand(comercio="Principal")],
        portal=portal,
    )


def _directory() -> StoreDirectory:
    """Return the synthetic store directory: one active portal, one todo portal."""
    return StoreDirectory(
        stores={
            SYNTHETIC_RUC: _store(SYNTHETIC_RUC, "synthetic"),
            MANUAL_RUC: _store(MANUAL_RUC, "manual"),
            TODO_RUC: _store(TODO_RUC, "todo"),
        },
        portals={
            "synthetic": Portal(key="synthetic", adapter="generic_form", url="https://synthetic.example/"),
            "todo": Portal(key="todo", adapter="generic_form", url="https://todo.example/", status="todo"),
        },
    )


def _invoice(ruc: str = SYNTHETIC_RUC) -> FetchInvoice:
    """Return the invoice the fake helper's XML matches."""
    return FetchInvoice(
        ruc_emisor=ruc,
        tipo="01",
        serie=SERIE,
        numero=NUMERO,
        fecha=FECHA,
        total=TOTAL,
    )


def _fetcher(tmp_path: Path, mode: str, *, helper_timeout_s: float = HELPER_TIMEOUT_S) -> PortalFetcher:
    """Return a fetcher whose helper is the fake script in its ``mode``."""
    script = tmp_path / "helper.py"
    script.write_text(HELPER_SCRIPT, encoding="utf-8")
    return PortalFetcher(
        _directory(),
        MEDIA_MAX_BYTES,
        helper_timeout_s,
        helper_command=(sys.executable, str(script), mode),
    )


async def test_fetch_matching_xml_and_pdf_is_ok(tmp_path: Path) -> None:
    """R-5.6: a matching XML and a PDF come back as ok with both files and their digests."""
    out_dir = tmp_path / OUT_DIR_NAME

    result = await _fetcher(tmp_path, "match").fetch(_invoice(), out_dir)

    assert result.reason is FetchReason.OK
    assert result.portal_key == "synthetic"
    assert [file.kind for file in result.files] == ["xml", "pdf"]
    for file, name in zip(result.files, (XML_NAME, PDF_NAME), strict=True):
        content = (out_dir / name).read_bytes()
        assert file.path == out_dir / name
        assert file.size == len(content)
        assert file.sha256 == hashlib.sha256(content).hexdigest()


async def test_fetch_xml_for_another_numero_is_mismatch_and_removes_files(
    tmp_path: Path,
) -> None:
    """BR-F06: an XML for a different numero is xml_mismatch and every file is removed."""
    out_dir = tmp_path / OUT_DIR_NAME

    result = await _fetcher(tmp_path, "other").fetch(_invoice(), out_dir)

    assert result.reason is FetchReason.XML_MISMATCH
    assert list(out_dir.iterdir()) == []


async def test_fetch_xml_with_a_doctype_is_invalid_and_removes_files(tmp_path: Path) -> None:
    """BR-F09: an XML carrying a DOCTYPE is rejected as xml_invalid and removed."""
    out_dir = tmp_path / OUT_DIR_NAME

    result = await _fetcher(tmp_path, "doctype").fetch(_invoice(), out_dir)

    assert result.reason is FetchReason.XML_INVALID
    assert list(out_dir.iterdir()) == []


async def test_fetch_utf16_xml_without_a_bom_is_invalid(tmp_path: Path) -> None:
    """BR-F09: a UTF-16 XML without a BOM is rejected as xml_invalid and removed."""
    out_dir = tmp_path / OUT_DIR_NAME

    result = await _fetcher(tmp_path, "utf16").fetch(_invoice(), out_dir)

    assert result.reason is FetchReason.XML_INVALID
    assert list(out_dir.iterdir()) == []


async def test_fetch_for_a_ruc_without_a_store_has_no_portal(tmp_path: Path) -> None:
    """R-5.8: an issuer with no store in the directory has no portal."""
    result = await _fetcher(tmp_path, "match").fetch(_invoice(UNKNOWN_RUC), tmp_path / OUT_DIR_NAME)

    assert result.reason is FetchReason.NO_PORTAL
    assert result.portal_key is None


async def test_fetch_for_a_manual_store_has_no_portal(tmp_path: Path) -> None:
    """R-5.8: a store whose portal is manual has no portal."""
    result = await _fetcher(tmp_path, "match").fetch(_invoice(MANUAL_RUC), tmp_path / OUT_DIR_NAME)

    assert result.reason is FetchReason.NO_PORTAL
    assert result.portal_key is None


async def test_fetch_for_a_todo_portal_is_inactive(tmp_path: Path) -> None:
    """R-5.8: a store pointing at a todo portal reports portal_inactive."""
    result = await _fetcher(tmp_path, "match").fetch(_invoice(TODO_RUC), tmp_path / OUT_DIR_NAME)

    assert result.reason is FetchReason.PORTAL_INACTIVE
    assert result.portal_key == "todo"
    assert result.files == ()


async def test_fetch_helper_outliving_its_timeout_reports_timeout(tmp_path: Path) -> None:
    """R-5.6: a helper that outlives helper_timeout_s reports timeout."""
    result = await _fetcher(tmp_path, "sleep", helper_timeout_s=SLEEP_TIMEOUT_S).fetch(
        _invoice(), tmp_path / OUT_DIR_NAME
    )

    assert result.reason is FetchReason.TIMEOUT
    assert result.files == ()


async def test_fetch_file_outside_out_dir_is_technical(tmp_path: Path) -> None:
    """R-5.6: a helper reporting a path outside out_dir is technical and that file is kept."""
    out_dir = tmp_path / OUT_DIR_NAME

    result = await _fetcher(tmp_path, "outside").fetch(_invoice(), out_dir)

    assert result.reason is FetchReason.TECHNICAL
    assert (tmp_path / "outside.xml").is_file()
