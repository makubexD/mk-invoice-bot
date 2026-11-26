"""Unit tests for the ``invoicebot portal-test`` command (S10.5, R-5.7)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from invoicebot.cli import main
from invoicebot.config.loader import load_directory
from invoicebot.ops.forget import ForgetRefused, ForgetRequest
from invoicebot.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config" / "example.yaml"
# config/portals/facturactiva-la-lena.yaml is the only active portal.
PORTAL_KEY = "facturactiva-la-lena"
# config/portals/infofact.yaml is still todo, so ``portal-test`` must activate it for the run.
TODO_PORTAL_KEY = "infofact"
# The store's RUC lives only in config/stores/ (D47), so the test reads it from there.
(STORE_RUC,) = [
    ruc for ruc, store in load_directory(REPO_ROOT / "config").stores.items() if store.portal == PORTAL_KEY
]
UNKNOWN_KEY = "no-such-portal"
TIPO = "01"
SERIE = "F001"
NUMERO = "4321"
FECHA = "2026-11-13"
TOTAL = "118.50"

#: A fake helper: it reads the request, writes a matching XML and a PDF, and prints one result.
#: Playwright is never imported, so the test needs no browser (R-5.6).
HELPER_SCRIPT = '''\
import hashlib
import json
import sys
from pathlib import Path

request = json.load(sys.stdin)
invoice = request["invoice"]
out_dir = Path(request["out_dir"])
out_dir.mkdir(parents=True, exist_ok=True)

XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>{invoice["serie"]}-{invoice["numero"]}</cbc:ID>
  <cbc:IssueDate>{invoice["fecha"]}</cbc:IssueDate>
  <cbc:InvoiceTypeCode>{invoice["tipo"]}</cbc:InvoiceTypeCode>
  <cac:AccountingSupplierParty>
    <cac:Party>
      <cac:PartyIdentification>
        <cbc:ID schemeID="6">{invoice["ruc_emisor"]}</cbc:ID>
      </cac:PartyIdentification>
    </cac:Party>
  </cac:AccountingSupplierParty>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="PEN">100.42</cbc:LineExtensionAmount>
    <cbc:PayableAmount currencyID="PEN">{invoice["total"]}</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
</Invoice>
"""


def write(kind, name, data):
    path = out_dir / name
    path.write_bytes(data)
    return {
        "kind": kind,
        "path": str(path),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


files = [
    write("xml", "invoice.xml", XML.encode("utf-8")),
    write("pdf", "invoice.pdf", b"%PDF-1.4 synthetic"),
]
print(json.dumps({"reason": "ok", "files": files}))
'''


def _write_sample(tmp_path: Path, ruc: str) -> Path:
    """Write the sample YAML whose fields match the fake helper's XML."""
    path = tmp_path / "sample.yaml"
    path.write_text(
        f'ruc_emisor: "{ruc}"\ntipo: "{TIPO}"\nserie: "{SERIE}"\n'
        f'numero: "{NUMERO}"\nfecha: "{FECHA}"\ntotal: "{TOTAL}"\n',
        encoding="utf-8",
    )
    return path


def _write_helper(tmp_path: Path) -> Path:
    """Write the fake browser helper script."""
    path = tmp_path / "helper.py"
    path.write_text(HELPER_SCRIPT, encoding="utf-8")
    return path


def _helper_command(script: Path) -> tuple[str, str]:
    """Return the fetcher's helper command for the fake script."""
    return (sys.executable, str(script))


def test_portal_test_ok_exits_zero_with_two_file_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R-5.7: a matching sample exits 0 and prints one line per downloaded file."""
    monkeypatch.setenv("CONFIG_PATH", str(CONFIG_PATH))
    sample = _write_sample(tmp_path, STORE_RUC)
    helper = _write_helper(tmp_path)

    code = main(
        ["portal-test", PORTAL_KEY, "--sample", str(sample)],
        helper_command=_helper_command(helper),
    )

    assert code == 0
    lines = capsys.readouterr().out.splitlines()
    file_lines = [line for line in lines if line.endswith(" bytes")]
    assert len(file_lines) == 2
    assert {line.split(" ", 1)[0] for line in file_lines} == {"pdf", "xml"}
    assert lines[0] == "reason: ok"
    assert lines[-1] == "xml matches: yes"


def test_portal_test_todo_portal_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R-5.7: a portal whose status is todo can be tried before it goes live."""
    monkeypatch.setenv("CONFIG_PATH", str(CONFIG_PATH))
    sample = _write_sample(tmp_path, STORE_RUC)
    helper = _write_helper(tmp_path)

    code = main(
        ["portal-test", TODO_PORTAL_KEY, "--sample", str(sample)],
        helper_command=_helper_command(helper),
    )

    assert code == 0
    assert capsys.readouterr().out.splitlines()[0] == "reason: ok"


def test_portal_test_unknown_key_exits_two_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R-5.7: an unknown portal key exits 2 and the message names the key."""
    monkeypatch.setenv("CONFIG_PATH", str(CONFIG_PATH))
    sample = _write_sample(tmp_path, STORE_RUC)

    with pytest.raises(SystemExit) as exit_info:
        main(["portal-test", UNKNOWN_KEY, "--sample", str(sample)])

    assert exit_info.value.code == 2
    assert UNKNOWN_KEY in capsys.readouterr().err


def test_portal_test_output_holds_no_sample_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R-5.7: stdout and stderr never hold an invoice field value or the sample path."""
    monkeypatch.setenv("CONFIG_PATH", str(CONFIG_PATH))
    sample = _write_sample(tmp_path, STORE_RUC)
    helper = _write_helper(tmp_path)

    code = main(
        ["portal-test", PORTAL_KEY, "--sample", str(sample)],
        helper_command=_helper_command(helper),
    )

    assert code == 0
    captured = capsys.readouterr()
    output = captured.out + captured.err
    for value in (STORE_RUC, SERIE, NUMERO, TOTAL):
        assert value not in output
    assert str(sample) not in output


class _FakePool:
    """A pool stand-in: the forget command only opens and closes it."""

    async def open(self, wait: bool = False) -> None:
        """Accept the open call without a database."""

    async def close(self) -> None:
        """Accept the close call without a database."""


def _patch_forget(
    monkeypatch: pytest.MonkeyPatch, outcome: list[str] | Exception
) -> list[tuple[bool, bool, bool, bool]]:
    """Fake the pool, the drive and ``forget``; return what each call received."""
    monkeypatch.setenv("CONFIG_PATH", str(CONFIG_PATH))
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    calls: list[tuple[bool, bool, bool, bool]] = []

    async def fake_forget(pool: object, data_dir: object, drive: object, request: ForgetRequest) -> list[str]:
        calls.append((drive is not None, request.drive, request.purge, request.apply))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("invoicebot.cli.create_pool", lambda url: _FakePool())
    monkeypatch.setattr("invoicebot.cli.personal_drive", lambda settings, cfg, http: object())
    monkeypatch.setattr("invoicebot.cli.forget", fake_forget)
    return calls


def test_invoice_forget_defaults_change_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D196: ``invoice forget 34`` asks for a dry run without the drive and prints the lines."""
    calls = _patch_forget(monkeypatch, ["would delete 2 file(s)"])

    code = main(["invoice", "forget", "34"])

    assert code == 0
    assert calls == [(False, False, False, False)]
    assert "would delete 2 file(s)" in capsys.readouterr().out


def test_invoice_forget_flags_are_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    """D196: ``--drive --purge --yes`` reach ``forget`` as true and the drive is built."""
    calls = _patch_forget(monkeypatch, [])

    code = main(["invoice", "forget", "34", "--drive", "--purge", "--yes"])

    assert code == 0
    assert calls == [(True, True, True, True)]


def test_invoice_forget_refused_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D196: a refusal exits 2 with its reason on stderr."""
    _patch_forget(monkeypatch, ForgetRefused("not terminal"))

    code = main(["invoice", "forget", "34"])

    assert code == 2
    assert "not terminal" in capsys.readouterr().err


def test_invoice_forget_needs_database_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D196: without DATABASE_URL the command is a parser error."""
    monkeypatch.setenv("CONFIG_PATH", str(CONFIG_PATH))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(SystemExit) as exit_info:
        main(["invoice", "forget", "34"], settings=Settings(database_url=None))

    assert exit_info.value.code == 2
    assert "DATABASE_URL" in capsys.readouterr().err
