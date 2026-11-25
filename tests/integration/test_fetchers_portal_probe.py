"""Integration tests for the bot's side of the portal probe helper (D193, R-9.2)."""

from __future__ import annotations

import hashlib
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from invoicebot.config.models import MediaMaxBytes
from invoicebot.fetchers.base import FetchInvoice
from invoicebot.fetchers.browser.probe_protocol import ProbeOutcome
from invoicebot.fetchers.portal_probe import PortalProber, Probe

SYNTHETIC_RUC = "20000000001"
URL = "https://portal.example.pe/factura?token=secret"
TIMEOUT_S = 5.0
SLEEP_TIMEOUT_S = 1.0
MEDIA_MAX_BYTES = MediaMaxBytes(image=10**7, pdf=10**7, xml=2 * 10**6, zip=2 * 10**7)
INVOICE = FetchInvoice(SYNTHETIC_RUC, "01", "F001", "123", date(2026, 11, 13), Decimal("118.50"))

#: A fake probe helper: it reads the request, writes chosen files and prints one report line.
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


def write(kind, path, data):
    path.write_bytes(data)
    return {"kind": kind, "path": str(path), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def report(files):
    print(json.dumps({"outcome": "ok", "files": files}))


if MODE == "sleep":
    time.sleep(10)
elif MODE == "garbage":
    print("not json")
elif MODE == "exit1":
    sys.exit(1)
elif MODE == "match":
    xml = INVOICE.format(numero="123").encode()
    report([write("xml", out_dir / "invoice.xml", xml), write("pdf", out_dir / "invoice.pdf", b"%PDF-1.4 x")])
elif MODE == "mismatch":
    xml = INVOICE.format(numero="999").encode()
    report([write("xml", out_dir / "invoice.xml", xml)])
elif MODE == "outside":
    report([write("pdf", Path(sys.argv[2]), b"%PDF-1.4 x")])
elif MODE == "pdf_only":
    report([write("pdf", out_dir / "invoice.pdf", b"%PDF-1.4 x")])
elif MODE == "fake_pdf":
    report([write("pdf", out_dir / "invoice.pdf", b"<html>not a pdf</html>")])
'''


def _prober(
    tmp_path: Path, mode: str, *, timeout_s: float = TIMEOUT_S, extra_args: tuple[str, ...] = ()
) -> PortalProber:
    """Return a prober whose helper is the fake script in its ``mode``."""
    script = tmp_path / "helper.py"
    script.write_text(HELPER_SCRIPT, encoding="utf-8")
    return PortalProber(
        MEDIA_MAX_BYTES, timeout_s, helper_command=(sys.executable, str(script), mode, *extra_args)
    )


async def _probe(
    tmp_path: Path, mode: str, *, timeout_s: float = TIMEOUT_S, extra_args: tuple[str, ...] = ()
) -> Probe:
    """Run the fake helper in ``mode`` for the synthetic invoice."""
    prober = _prober(tmp_path, mode, timeout_s=timeout_s, extra_args=extra_args)
    return await prober.probe(URL, INVOICE, tmp_path / "out")


def _names(tmp_path: Path) -> list[str]:
    """Return the file names left in the out directory, outside the async test's loop."""
    out = tmp_path / "out"
    return sorted(path.name for path in out.iterdir()) if out.exists() else []


async def test_probe_matching_xml_returns_its_files(tmp_path: Path) -> None:
    """D193, R-9.2: an ok report with a matching XML returns both files with the bot's digests."""
    probe = await _probe(tmp_path, "match")
    assert probe.report.outcome is ProbeOutcome.OK
    assert sorted(file.kind for file in probe.files) == ["pdf", "xml"]
    for file in probe.files:
        assert file.sha256 == hashlib.sha256(file.path.read_bytes()).hexdigest()


async def test_probe_mismatching_xml_is_not_found_and_deleted(tmp_path: Path) -> None:
    """D193, R-9.2: an XML of another invoice gives not_found, no files and nothing left on disk."""
    probe = await _probe(tmp_path, "mismatch")
    assert probe.report.outcome is ProbeOutcome.NOT_FOUND
    assert probe.files == ()
    assert _names(tmp_path) == []


async def test_probe_path_outside_out_dir_is_technical_and_untouched(tmp_path: Path) -> None:
    """D193, R-9.2: a reported path outside out_dir gives technical and the file is not deleted."""
    outside = tmp_path / "elsewhere.pdf"
    probe = await _probe(tmp_path, "outside", extra_args=(str(outside),))
    assert probe.report.outcome is ProbeOutcome.TECHNICAL
    assert probe.files == ()
    assert outside.read_bytes() == b"%PDF-1.4 x"


async def test_probe_lone_pdf_is_returned(tmp_path: Path) -> None:
    """D193, R-9.2: a PDF alone passes the path, size, hash and header checks."""
    probe = await _probe(tmp_path, "pdf_only")
    assert probe.report.outcome is ProbeOutcome.OK
    assert [file.kind for file in probe.files] == ["pdf"]


async def test_probe_pdf_that_is_not_a_pdf_is_technical_and_deleted(tmp_path: Path) -> None:
    """D193, R-9.2: a file reported as a PDF without the header gives technical and is deleted."""
    probe = await _probe(tmp_path, "fake_pdf")
    assert probe.report.outcome is ProbeOutcome.TECHNICAL
    assert probe.files == ()
    assert _names(tmp_path) == []


async def test_probe_helper_outliving_its_timeout_is_busy(tmp_path: Path) -> None:
    """D193, R-9.2: a helper that outlives the timeout gives busy."""
    probe = await _probe(tmp_path, "sleep", timeout_s=SLEEP_TIMEOUT_S)
    assert probe.report.outcome is ProbeOutcome.BUSY
    assert probe.files == ()


async def test_probe_non_json_output_is_technical(tmp_path: Path) -> None:
    """D193, R-9.2: output that is not JSON gives technical."""
    assert (await _probe(tmp_path, "garbage")).report.outcome is ProbeOutcome.TECHNICAL


async def test_probe_helper_exit_1_is_technical(tmp_path: Path) -> None:
    """D193, R-9.2: a helper that exits non-zero gives technical."""
    assert (await _probe(tmp_path, "exit1")).report.outcome is ProbeOutcome.TECHNICAL
