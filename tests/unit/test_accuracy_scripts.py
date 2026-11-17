"""Tests for the accuracy report and the expected-from-XML helper (R-2.9, R-9.16, D127)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

# The hardened UBL fixture; its RUCs are the synthetic ones in tests/synthetic_rucs.txt.
from test_extract_ubl import FULL_FIELDS, _invoice_xml

from invoicebot.extract.accuracy import EXPECTED_FIELDS, load_expected

ROOT = Path(__file__).resolve().parents[2]
REPORT_SCRIPT = ROOT / "scripts" / "accuracy_report.py"
EXPECTED_SCRIPT = ROOT / "scripts" / "expected_from_xml.py"
GOLDEN = ROOT / "tests" / "golden"


def _load_script(name: str, path: Path) -> ModuleType:
    """Return the module at ``path`` loaded as ``name``."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


accuracy_report = _load_script("accuracy_report", REPORT_SCRIPT)
expected_from_xml = _load_script("expected_from_xml", EXPECTED_SCRIPT)


def _set_rucs(set_dir: Path) -> set[str]:
    """Return every issuer and buyer RUC the set's expected files hold."""
    rucs: set[str] = set()
    for expected_path in set_dir.glob("*.json"):
        fields = load_expected(expected_path).fields
        rucs.update(fields[name] for name in ("ruc_emisor", "num_doc_adq") if name in fields)
    return rucs


def _set_file_names(set_dir: Path) -> set[str]:
    """Return every document and expected file name in the set."""
    return {
        path.name for path in set_dir.iterdir() if path.suffix.lower() in {".jpg", ".png", ".pdf", ".json"}
    }


def test_report_on_golden_prints_rates_and_passes(capsys: pytest.CaptureFixture[str]) -> None:
    """R-2.9: the report over the committed set prints both rates, per-field rates and passes."""
    exit_code = accuracy_report.main(["--set", str(GOLDEN)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "qr rate:" in output
    assert "ocr-only required-field rate:" in output
    assert "ruc_emisor:" in output
    assert "pass: yes" in output


def test_report_hides_rucs_and_file_names(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """R-9.16: the report and the markdown it writes name no RUC from the set and no file."""
    report_path = tmp_path / "accuracy.md"

    exit_code = accuracy_report.main(["--set", str(GOLDEN), "--write", str(report_path)])

    output = capsys.readouterr().out
    markdown = report_path.read_text(encoding="utf-8")
    assert exit_code == 0
    for text in (output, markdown):
        assert not any(ruc in text for ruc in _set_rucs(GOLDEN))
        assert not any(name in text for name in _set_file_names(GOLDEN))


def test_expected_from_xml_writes_the_d127_expected_json(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """R-2.9: a UBL invoice becomes the expected JSON, D127 fields only and has_qr true."""
    xml_path = tmp_path / "invoice.xml"
    xml_path.write_text(_invoice_xml(), encoding="utf-8")

    assert expected_from_xml.main([str(xml_path)]) == 0

    expected_path = tmp_path / "expected.json"
    expected_path.write_text(capsys.readouterr().out, encoding="utf-8")
    expected = load_expected(expected_path)
    d127_fields = {name: value for name, value in FULL_FIELDS.items() if name in EXPECTED_FIELDS}
    assert expected.has_qr is True
    assert expected.fields == d127_fields
    assert "tipo_doc_adq" not in expected.fields


def test_expected_from_xml_no_qr_sets_has_qr_false(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """R-2.9: --no-qr records a receipt whose photo carries no SUNAT QR."""
    xml_path = tmp_path / "invoice.xml"
    xml_path.write_text(_invoice_xml(), encoding="utf-8")

    assert expected_from_xml.main([str(xml_path), "--no-qr"]) == 0

    assert json.loads(capsys.readouterr().out)["has_qr"] is False
