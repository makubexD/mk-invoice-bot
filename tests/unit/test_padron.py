"""Unit tests for the padrón text parser (R-2.5)."""

import zipfile
from pathlib import Path

import pytest

from invoicebot.padron import Skipped, open_member, rows

_VALID_ROW = "20123456789|ACME S.A.C.|ACTIVO|HABIDO"
_VALID_PARSED = ("20123456789", "ACME S.A.C.", "ACTIVO", "HABIDO")


def _write_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return path


def test_open_member_decodes_latin1(tmp_path: Path) -> None:
    """R-2.5 a padrón line with Ñ in ISO-8859-1 decodes."""
    member = "RUC|RAZÓN SOCIAL|ESTADO|CONDICIÓN\r\n20123456789|MÜLLER ÑANDÚ S.A.C.|ACTIVO|HABIDO\r\n"
    path = _write_zip(tmp_path / "padron.zip", {"padron_reducido_ruc.txt": member.encode("iso-8859-1")})

    with open_member(path) as text:
        lines = list(text)

    assert lines[0].startswith("RUC|RAZÓN")
    assert "MÜLLER ÑANDÚ" in lines[1]


def test_rows_skips_a_short_line_and_a_ten_digit_ruc() -> None:
    """R-2.5 a short line and a 10-digit RUC are skipped and counted."""
    skipped = Skipped()
    lines = [
        "RUC|RAZÓN SOCIAL|ESTADO|CONDICIÓN",
        "12345678901|ONLY THREE COLUMNS|ACTIVO",
        "1234567890|TEN DIGIT RUC|ACTIVO|HABIDO",
        _VALID_ROW,
    ]

    assert list(rows(lines, skipped)) == [_VALID_PARSED]
    assert skipped.count == 2


def test_rows_skips_the_header() -> None:
    """R-2.5 the header row is skipped."""
    skipped = Skipped()

    assert list(rows(["RUC|RAZÓN SOCIAL|ESTADO|CONDICIÓN", _VALID_ROW], skipped)) == [_VALID_PARSED]
    assert skipped.count == 0


def test_open_member_rejects_two_text_members(tmp_path: Path) -> None:
    """R-2.5 a ZIP with two .txt members raises."""
    path = _write_zip(tmp_path / "padron.zip", {"one.txt": b"a", "two.txt": b"b"})

    with pytest.raises(ValueError, match=r"one \.txt member"), open_member(path):
        pass
