"""Tests for the synthetic receipt generator (S3.9, R-2.1, R-2.9, R-9.17, D127)."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import cv2

from invoicebot.extract.accuracy import QR_FIELDS, Expected, load_expected
from invoicebot.extract.merge import same_value
from invoicebot.extract.qr import read_invoice_qr

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "make_golden.py"
CHECK_FIXTURES = ROOT / "scripts" / "check_fixtures.py"
SYNTHETIC_RUCS = ROOT / "tests" / "synthetic_rucs.txt"
SEED = 1
COUNT = 4


def _load_script(name: str, path: Path) -> ModuleType:
    """Return the module at ``path`` loaded as ``name``."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


make_golden = _load_script("make_golden", SCRIPT)
check_fixtures = _load_script("check_fixtures", CHECK_FIXTURES)


def _expected_pairs(root: Path) -> list[tuple[Path, Expected]]:
    """Return each generated image with its loaded expected file, in file order."""
    return [
        (json_path.with_suffix(".jpg"), load_expected(json_path)) for json_path in sorted(root.glob("*.json"))
    ]


def test_generate_same_seed_writes_identical_files(tmp_path: Path) -> None:
    """R-2.9: the same seed writes byte-identical images and expected files."""
    first, second = tmp_path / "first", tmp_path / "second"

    make_golden.generate(SEED, COUNT, first)
    make_golden.generate(SEED, COUNT, second)

    names = sorted(path.name for path in first.iterdir())
    assert names == sorted(path.name for path in second.iterdir())
    assert names
    assert all((first / name).read_bytes() == (second / name).read_bytes() for name in names)


def test_generated_set_passes_fixture_check(tmp_path: Path) -> None:
    """R-9.17: generated images carry no EXIF and only allowlisted synthetic RUCs."""
    (tmp_path / "tests").mkdir()
    shutil.copy(SYNTHETIC_RUCS, tmp_path / "tests" / "synthetic_rucs.txt")

    make_golden.generate(SEED, COUNT, tmp_path)

    assert check_fixtures.find_problems(tmp_path) == []


def test_expected_total_equals_base_plus_igv_plus_service(tmp_path: Path) -> None:
    """R-2.9: each expected total is exactly its base, IGV and service charge."""
    make_golden.generate(SEED, COUNT, tmp_path)

    for _, expected in _expected_pairs(tmp_path):
        fields = expected.fields
        total = Decimal(fields["total"])
        service_charge = Decimal(fields.get("service_charge", "0"))
        assert total == Decimal(fields["base"]) + Decimal(fields["igv"]) + service_charge


def test_generated_qr_decodes_to_the_expected_values(tmp_path: Path) -> None:
    """R-2.1: every printed SUNAT QR decodes to the values its expected file holds."""
    make_golden.generate(SEED, COUNT, tmp_path)

    qr_pairs = [(path, expected) for path, expected in _expected_pairs(tmp_path) if expected.has_qr]
    assert qr_pairs
    for image_path, expected in qr_pairs:
        hit = read_invoice_qr(cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE))
        assert hit is not None
        for name in QR_FIELDS:
            if name in expected.fields:
                actual = hit.reading.value(name)
                assert actual is not None
                assert same_value(name, actual, expected.fields[name])
