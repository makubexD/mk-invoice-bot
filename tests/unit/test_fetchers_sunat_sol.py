"""Tests for the ``sunat_sol`` fetcher (R-5.1, R-5.2, R-9.10)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from invoicebot.config.models import MediaMaxBytes, SunatSolSite
from invoicebot.fetchers.base import FetchInvoice, FetchReason
from invoicebot.fetchers.sunat_sol import SECRETS, SunatSolFetcher
from invoicebot.secrets import Secrets, SecretStore
from invoicebot.sunat_origins import SUNAT_SOL

CONFIG = Path(__file__).resolve().parents[2] / "config" / "sunat_sol.yaml"
SOL_RUC = "20000000001"
SOL_USER = "USUARIO1"
SOL_PASSWORD = "synthetic-sol-password"  # noqa: S105  # a synthetic test value
TIPO = "01"
SERIE = "F001"
NUMERO = "123"
TOTAL = Decimal("118.50")
FECHA = date(2026, 11, 13)
HELPER_TIMEOUT_S = 5.0
SLEEP_TIMEOUT_S = 0.5
OUT_DIR_NAME = "out"
XML_NAME = "invoice.xml"
MEDIA_MAX_BYTES = MediaMaxBytes(
    image=10 * 1024 * 1024,
    pdf=10 * 1024 * 1024,
    xml=2 * 1024 * 1024,
    zip=20 * 1024 * 1024,
)

#: A fake SOL helper: it records what it received, writes a chosen file and prints one result.
#: Playwright is never imported, so the test needs no browser (R-5.2).
HELPER_SCRIPT = '''\
import hashlib
import json
import os
import sys
import time
from pathlib import Path

MODE = sys.argv[1]
RECORD = Path(sys.argv[2]) if len(sys.argv) > 2 else None
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


def report(reason, files):
    print(json.dumps({"reason": reason, "files": files}))


if RECORD is not None:
    RECORD.write_text(json.dumps({"request": request, "argv": sys.argv, "env": dict(os.environ)}))

if MODE == "sleep":
    time.sleep(10)
elif MODE == "not_found":
    report("not_found_yet", [])
elif MODE == "match":
    data = INVOICE.format(numero="123").encode("utf-8")
    path = out_dir / "invoice.xml"
    path.write_bytes(data)
    report("ok", [{
        "kind": "xml",
        "path": str(path),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }])
else:
    report("technical", [])
'''


def _site() -> SunatSolSite:
    """The SOL site from the shipped ``config/sunat_sol.yaml``, which validates (R-5.2)."""
    return SunatSolSite.model_validate(yaml.safe_load(CONFIG.read_text(encoding="utf-8")))


def _secrets(values: dict[str, str] | None = None) -> Secrets:
    """A ``sunat_sol`` view over a synthetic in-memory store (R-9.10)."""
    if values is None:
        values = dict(zip(SECRETS, (SOL_RUC, SOL_USER, SOL_PASSWORD), strict=True))
    return SecretStore(values).scoped(SUNAT_SOL, set(SECRETS))


def _invoice() -> FetchInvoice:
    """The invoice the fake helper's XML matches."""
    return FetchInvoice(
        ruc_emisor=SOL_RUC,
        tipo=TIPO,
        serie=SERIE,
        numero=NUMERO,
        fecha=FECHA,
        total=TOTAL,
    )


def _fetcher(
    tmp_path: Path,
    mode: str,
    *,
    secrets: Secrets | None = None,
    timeout_s: float = HELPER_TIMEOUT_S,
    record: Path | None = None,
) -> SunatSolFetcher:
    """Return a fetcher whose helper is the fake script in its ``mode``."""
    script = tmp_path / "sol_helper.py"
    script.write_text(HELPER_SCRIPT, encoding="utf-8")
    argv = [sys.executable, str(script), mode]
    if record is not None:
        argv.append(str(record))
    return SunatSolFetcher(
        _site(),
        _secrets() if secrets is None else secrets,
        MEDIA_MAX_BYTES,
        timeout_s,
        helper_command=argv,
    )


async def test_fetch_sends_the_credentials_on_stdin_only(tmp_path: Path) -> None:
    """R-9.10: the three real values reach the helper on stdin, not in its argv or environment."""
    record = tmp_path / "record.json"

    await _fetcher(tmp_path, "not_found", record=record).fetch(_invoice(), tmp_path / OUT_DIR_NAME)

    seen = json.loads(record.read_text(encoding="utf-8"))
    assert seen["request"]["credentials"] == {
        "ruc": SOL_RUC,
        "user": SOL_USER,
        "password": SOL_PASSWORD,
    }
    argv = " ".join(seen["argv"])
    environment = "\n".join(seen["env"].values())
    for value in (SOL_RUC, SOL_USER, SOL_PASSWORD):
        assert value not in argv
        assert value not in environment


async def test_fetch_matching_xml_is_ok(tmp_path: Path) -> None:
    """R-5.2: an ``ok`` output with a matching XML returns OK and the file on disk."""
    out_dir = tmp_path / OUT_DIR_NAME

    result = await _fetcher(tmp_path, "match").fetch(_invoice(), out_dir)

    assert result.reason is FetchReason.OK
    assert result.portal_key == SUNAT_SOL
    assert [file.kind for file in result.files] == ["xml"]
    file = result.files[0]
    content = (out_dir / XML_NAME).read_bytes()
    assert file.path == out_dir / XML_NAME
    assert file.size == len(content)
    assert file.sha256 == hashlib.sha256(content).hexdigest()


async def test_fetch_not_found_yet_passes_through(tmp_path: Path) -> None:
    """R-5.1: a helper that has not found the invoice yet reports not_found_yet."""
    result = await _fetcher(tmp_path, "not_found").fetch(_invoice(), tmp_path / OUT_DIR_NAME)

    assert result.reason is FetchReason.NOT_FOUND_YET
    assert result.files == ()
    assert result.portal_key == SUNAT_SOL


async def test_fetch_helper_outliving_its_timeout_reports_timeout(tmp_path: Path) -> None:
    """R-5.1: a helper that outlives ``timeout_s`` reports timeout, as the portal fetcher does."""
    result = await _fetcher(tmp_path, "sleep", timeout_s=SLEEP_TIMEOUT_S).fetch(
        _invoice(), tmp_path / OUT_DIR_NAME
    )

    assert result.reason is FetchReason.TIMEOUT
    assert result.files == ()
    assert result.portal_key == SUNAT_SOL


def test_importing_the_fetcher_does_not_load_playwright() -> None:
    """R-9.1: the bot imports ``sunat_sol`` without loading a browser."""
    probe = "import sys, invoicebot.fetchers.sunat_sol; sys.exit(1 if 'playwright' in sys.modules else 0)"

    completed = subprocess.run(  # noqa: S603 - fixed argv: this interpreter and a constant probe
        [sys.executable, "-c", probe],
        check=False,
    )

    assert completed.returncode == 0


def test_fetcher_without_sol_password_raises_at_construction() -> None:
    """R-9.10: a view without ``sol_password`` fails at construction, naming the key only."""
    view = _secrets({"sol_ruc": SOL_RUC, "sol_user": SOL_USER})

    with pytest.raises(ValueError, match="sol_password") as excinfo:
        SunatSolFetcher(_site(), view, MEDIA_MAX_BYTES, HELPER_TIMEOUT_S)

    message = str(excinfo.value)
    assert SOL_RUC not in message
    assert SOL_USER not in message
