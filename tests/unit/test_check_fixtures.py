"""Tests for ``scripts/check_fixtures.py`` (R-9.17)."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_fixtures.py"
GIT = shutil.which("git")
assert GIT is not None, "git is required to test the ls-files scan"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_fixtures", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_fixtures = _load_script()


def _check_digit(first_ten: str) -> int:
    """Compute the SUNAT check digit (R-9.17) for an independent test value."""
    weights = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
    total = sum(int(digit) * weight for digit, weight in zip(first_ten, weights, strict=True))
    check = 11 - (total % 11)
    if check == 10:
        return 0
    if check == 11:
        return 1
    return check


def _synthetic_ruc(sequence: int) -> str:
    """Build a valid-check-digit synthetic RUC: 20 + 00000 + a 3-digit sequence."""
    first_ten = f"2000000{sequence:03d}"
    return first_ten + str(_check_digit(first_ten))


def _fixture_tree(tmp_path: Path) -> tuple[Path, Path]:
    """Create the ``tests/`` and ``config/`` tree the checker scans."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tmp_path / "config").mkdir()
    return tmp_path, tests_dir


def _git_tree(tmp_path: Path, tracked: str) -> None:
    """Initialise a git checkout in ``tmp_path`` and track the already-written ``tracked`` file."""
    for args in (("init", "-q"), ("add", tracked)):
        subprocess.run(  # noqa: S603 - fixed git argv, no untrusted input
            [GIT, *args], cwd=tmp_path, check=True, capture_output=True
        )


def _jpeg(*, exif: bool) -> bytes:
    """Build a minimal JPEG, with or without an EXIF-bearing APP1 segment."""
    if not exif:
        return b"\xff\xd8\xff\xd9"
    payload = b"Exif\x00\x00MM\x00*\x00\x00\x00\x08"
    length = (2 + len(payload)).to_bytes(2, "big")
    return b"\xff\xd8\xff\xe1" + length + payload + b"\xff\xd9"


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + kind + payload + b"\x00\x00\x00\x00"


def _png(*, exif: bool) -> bytes:
    """Build a minimal PNG, with or without an ``eXIf`` chunk."""
    exif_chunk = _png_chunk(b"eXIf", b"MM\x00*") if exif else b""
    return PNG_SIGNATURE + exif_chunk + _png_chunk(b"IEND", b"")


def test_image_with_exif_fails(tmp_path: Path) -> None:
    """R-9.17: a committed image with EXIF metadata is reported."""
    _, tests_dir = _fixture_tree(tmp_path)
    (tests_dir / "photo.jpg").write_bytes(_jpeg(exif=True))
    (tests_dir / "scan.png").write_bytes(_png(exif=True))

    problems = check_fixtures.find_problems(tmp_path)

    assert any("photo.jpg" in problem and "EXIF" in problem for problem in problems)
    assert any("scan.png" in problem and "EXIF" in problem for problem in problems)
    assert check_fixtures.main([str(tmp_path)]) == 1


def test_clean_image_passes(tmp_path: Path) -> None:
    """R-9.17: images without EXIF metadata are accepted."""
    _, tests_dir = _fixture_tree(tmp_path)
    (tests_dir / "photo.jpg").write_bytes(_jpeg(exif=False))
    (tests_dir / "scan.png").write_bytes(_png(exif=False))

    assert check_fixtures.find_problems(tmp_path) == []
    assert check_fixtures.main([str(tmp_path)]) == 0


def test_valid_non_allowlisted_ruc_fails(tmp_path: Path) -> None:
    """R-9.17: a valid-check-digit RUC absent from the allowlist is a failure."""
    _, tests_dir = _fixture_tree(tmp_path)
    value = _synthetic_ruc(2)
    (tests_dir / "notes.txt").write_text(f"RUC: {value}\n", encoding="utf-8")

    problems = check_fixtures.find_problems(tmp_path)

    assert len(problems) == 1
    assert value in problems[0]


def test_allowlisted_ruc_passes(tmp_path: Path) -> None:
    """R-9.17: a valid-check-digit RUC listed in tests/synthetic_rucs.txt passes."""
    _, tests_dir = _fixture_tree(tmp_path)
    value = _synthetic_ruc(2)
    (tests_dir / "notes.txt").write_text(f"RUC: {value}\n", encoding="utf-8")
    (tests_dir / "synthetic_rucs.txt").write_text(f"# synthetic\n{value}\n", encoding="utf-8")

    assert check_fixtures.find_problems(tmp_path) == []


def test_store_file_may_hold_its_own_ruc(tmp_path: Path) -> None:
    """R-5.8, D47: config/stores/<ruc>.yaml may contain the RUC it is named after."""
    _fixture_tree(tmp_path)
    value = _synthetic_ruc(4)
    stores = tmp_path / "config" / "stores"
    stores.mkdir()
    (stores / f"{value}.yaml").write_text(f'ruc: "{value}"\n', encoding="utf-8")

    assert check_fixtures.find_problems(tmp_path) == []


def test_store_file_with_another_ruc_fails(tmp_path: Path) -> None:
    """R-9.17: a store file may hold only its own RUC; any other real-looking RUC still fails."""
    _fixture_tree(tmp_path)
    own, other = _synthetic_ruc(4), _synthetic_ruc(5)
    stores = tmp_path / "config" / "stores"
    stores.mkdir()
    (stores / f"{own}.yaml").write_text(f'ruc: "{own}"\nnotes: "{other}"\n', encoding="utf-8")

    problems = check_fixtures.find_problems(tmp_path)

    assert len(problems) == 1
    assert other in problems[0]


def test_store_ruc_outside_its_file_fails(tmp_path: Path) -> None:
    """R-9.17: a store's RUC is allowed only inside its own store file, not elsewhere in tests/ or config/."""
    _, tests_dir = _fixture_tree(tmp_path)
    value = _synthetic_ruc(4)
    stores = tmp_path / "config" / "stores"
    stores.mkdir()
    (stores / f"{value}.yaml").write_text(f'ruc: "{value}"\n', encoding="utf-8")
    (tests_dir / "notes.txt").write_text(f"RUC: {value}\n", encoding="utf-8")

    problems = check_fixtures.find_problems(tmp_path)

    assert len(problems) == 1
    assert "tests/notes.txt" in problems[0]


def test_bad_check_digit_passes(tmp_path: Path) -> None:
    """R-9.17: an 11-digit number with an invalid check digit is not a RUC."""
    _, tests_dir = _fixture_tree(tmp_path)
    value = _synthetic_ruc(3)
    bad_value = value[:-1] + str((int(value[-1]) + 1) % 10)
    (tests_dir / "notes.txt").write_text(f"phone: {bad_value}\n", encoding="utf-8")

    assert check_fixtures.find_problems(tmp_path) == []


def test_ruc_in_docs_fails(tmp_path: Path) -> None:
    """R-9.17: a valid-check-digit RUC in a tracked text file under docs/ is reported."""
    docs = tmp_path / "docs"
    docs.mkdir()
    value = _synthetic_ruc(6)
    (docs / "x.md").write_text(f"RUC: {value}\n", encoding="utf-8")

    problems = check_fixtures.find_problems(tmp_path)

    assert len(problems) == 1
    assert "docs/x.md" in problems[0]
    assert value in problems[0]


def test_ruc_in_specs_spike_fails(tmp_path: Path) -> None:
    """R-9.17: a valid-check-digit RUC in a tracked text file under specs/spikes/ is reported."""
    spikes = tmp_path / "specs" / "spikes"
    spikes.mkdir(parents=True)
    value = _synthetic_ruc(6)
    (spikes / "y.md").write_text(f"RUC: {value}\n", encoding="utf-8")

    problems = check_fixtures.find_problems(tmp_path)

    assert len(problems) == 1
    assert "specs/spikes/y.md" in problems[0]
    assert value in problems[0]


def test_ruc_in_uv_lock_passes(tmp_path: Path) -> None:
    """R-9.17: uv.lock is skipped, so hash fragments that look like RUCs are not reported."""
    value = _synthetic_ruc(6)
    (tmp_path / "uv.lock").write_text(f'hash = "{value}"\n', encoding="utf-8")

    assert check_fixtures.find_problems(tmp_path) == []


def test_git_checkout_scans_tracked_files_only(tmp_path: Path) -> None:
    """R-9.17: inside a git checkout only files from git ls-files are scanned."""
    docs = tmp_path / "docs"
    docs.mkdir()
    tracked, untracked = _synthetic_ruc(6), _synthetic_ruc(7)
    (docs / "tracked.md").write_text(f"RUC: {tracked}\n", encoding="utf-8")
    _git_tree(tmp_path, "docs/tracked.md")
    (docs / "untracked.md").write_text(f"RUC: {untracked}\n", encoding="utf-8")

    problems = check_fixtures.find_problems(tmp_path)

    assert len(problems) == 1
    assert "docs/tracked.md" in problems[0]
    assert tracked in problems[0]


def test_is_valid_ruc_hand_computed_example() -> None:
    """R-9.17: is_valid_ruc matches a hand-computed check digit.

    First ten digits ``2000000000``: 2*5 + 0*4 + 0*3 + 0*2 + 0*7 + 0*6 +
    0*5 + 0*4 + 0*3 + 0*2 = 10, so 11 - (10 mod 11) = 1 and the RUC ends in 1.
    """
    assert check_fixtures.is_valid_ruc("20000000001") is True
    assert check_fixtures.is_valid_ruc("20000000002") is False
