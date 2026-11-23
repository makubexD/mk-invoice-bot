"""Tests for reading the go-live import's claim folders (R-7.16)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

# The existing synthetic invoice generator; its RUCs are the ones in tests/synthetic_rucs.txt.
from test_extract_ubl import _invoice_xml

from invoicebot.config.models import Config
from invoicebot.extract.ubl import MAX_XML_BYTES
from invoicebot.importer import (
    SKIP_FIELDS_MISSING,
    SKIP_NAME,
    SKIP_NO_XML,
    SKIP_PAID,
    SKIP_SEVERAL_XML,
    SKIP_UNSENT,
    SKIP_XML_TOO_LARGE,
    SKIP_XML_UNPARSEABLE,
    SKIP_XML_UNREADABLE,
    Claim,
    ClaimFolder,
    Skipped,
    import_origin_channel,
    parse_claim_folder,
    read_claim,
)

_SENT_NAME = "2026-07-Support_3_LaLena_Sent"
ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "example.yaml"


def _example_document() -> dict[str, Any]:
    """Return the shipped example config as the mapping the tests edit."""
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def _folder(root: Path, name: str) -> Path:
    """Create and return the folder ``name`` under ``root``."""
    folder = root / name
    folder.mkdir()
    return folder


def _write_xml(folder: Path, content: str | bytes, name: str = "invoice.xml") -> None:
    """Write one XML file, as text or already-encoded bytes, into ``folder``."""
    path = folder / name
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param(
            "2026-7-Support_3_LaLena_Sent",
            ClaimFolder(2026, 7, 3, "LaLena", sent=True, paid=False),
            id="short-month",
        ),
        pytest.param(
            "2026-07-Support_3_LaLena_Sent",
            ClaimFolder(2026, 7, 3, "LaLena", sent=True, paid=False),
            id="long-month",
        ),
        pytest.param(
            "2026-11-Support_2_LaLena",
            ClaimFolder(2026, 11, 2, "LaLena", sent=False, paid=False),
            id="unsent",
        ),
        pytest.param(
            "2026-11-Support_2_LaLena_Sent_PAID_15_12___144_6",
            ClaimFolder(2026, 11, 2, "LaLena", sent=True, paid=True),
            id="sent-paid",
        ),
        pytest.param(
            "2026-11-Support_2_LaLena_PAID_15_12___144_6",
            ClaimFolder(2026, 11, 2, "LaLena", sent=False, paid=True),
            id="paid-without-sent",
        ),
        pytest.param(
            "2026-11-Support_2_X_PAID_15_12___144.6",
            ClaimFolder(2026, 11, 2, "X", sent=False, paid=True),
            id="paid-amount-with-dot",
        ),
    ],
)
def test_parse_claim_folder_reads_the_name(name: str, expected: ClaimFolder) -> None:
    """R-7.16: a claim folder name gives its year, month, seq, comercio and status."""
    assert parse_claim_folder(name) == expected


def test_parse_claim_folder_reads_both_month_forms_the_same() -> None:
    """R-7.16: `2026-7-...` and `2026-07-...` name the same claim folder."""
    assert parse_claim_folder("2026-7-Support_3_LaLena_Sent") == parse_claim_folder(
        "2026-07-Support_3_LaLena_Sent"
    )


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("Fotos", id="not-a-claim"),
        pytest.param("2026-13-Support_3_LaLena", id="month-13"),
        pytest.param("2026-0-Support_3_LaLena", id="month-0"),
        pytest.param("2026-07-Support_0_LaLena", id="seq-0"),
        pytest.param("2026-07-Support_3_La/Lena", id="slash-in-comercio"),
        pytest.param("2026-07-Support_3_La\\Lena", id="backslash-in-comercio"),
        pytest.param("2026-07-Support_3_", id="empty-comercio"),
        pytest.param("2026-07-Support_LaLena", id="no-seq"),
        pytest.param("2026-07-3-Support_3_LaLena", id="no-support"),
    ],
)
def test_parse_claim_folder_rejects_other_names(name: str) -> None:
    """R-7.16: a non-claim name, a month outside 1-12, a zero seq or a separator is rejected."""
    assert parse_claim_folder(name) is None


def test_read_claim_reads_a_sent_unpaid_folder(tmp_path: Path) -> None:
    """R-7.16: a sent, unpaid folder with one XML gives its claim fields and folder."""
    folder = _folder(tmp_path, _SENT_NAME)
    _write_xml(folder, _invoice_xml())

    claim = read_claim(folder)

    assert claim == Claim(
        ruc="20000000001",
        tipo="01",
        serie="F014",
        numero="15160",
        fecha=date(2026, 11, 13),
        total=Decimal("49.90"),
        razon_social="RESTAURANTE EJEMPLO S.A.C.",
        folder=ClaimFolder(2026, 7, 3, "LaLena", sent=True, paid=False),
    )


def test_read_claim_skips_a_paid_folder(tmp_path: Path) -> None:
    """R-7.16: a `_PAID_` folder is skipped even when its XML is readable."""
    folder = _folder(tmp_path, "2026-07-Support_3_LaLena_Sent_PAID_15_12___144_6")
    _write_xml(folder, _invoice_xml())

    assert read_claim(folder) == Skipped(SKIP_PAID)


def test_read_claim_skips_an_unsent_folder(tmp_path: Path) -> None:
    """R-7.16: a folder without `_Sent` is skipped."""
    folder = _folder(tmp_path, "2026-07-Support_3_LaLena")
    _write_xml(folder, _invoice_xml())

    assert read_claim(folder) == Skipped(SKIP_UNSENT)


def test_read_claim_skips_a_folder_that_is_not_a_claim(tmp_path: Path) -> None:
    """R-7.16: a folder such as `Fotos` is skipped as not a claim."""
    folder = _folder(tmp_path, "Fotos")

    assert read_claim(folder) == Skipped(SKIP_NAME)


def test_read_claim_skips_a_folder_with_no_xml(tmp_path: Path) -> None:
    """R-7.16: a claim folder with no XML is skipped with its reason."""
    folder = _folder(tmp_path, _SENT_NAME)

    assert read_claim(folder) == Skipped(SKIP_NO_XML)


def test_read_claim_skips_a_folder_with_several_xmls(tmp_path: Path) -> None:
    """R-7.16: a claim folder with more than one XML is skipped with its reason."""
    folder = _folder(tmp_path, _SENT_NAME)
    _write_xml(folder, _invoice_xml(), name="a.xml")
    _write_xml(folder, _invoice_xml(), name="b.xml")

    assert read_claim(folder) == Skipped(SKIP_SEVERAL_XML)


def test_read_claim_skips_an_xml_over_the_size_cap(tmp_path: Path) -> None:
    """R-7.16: an XML over the existing size cap is skipped before it is read."""
    folder = _folder(tmp_path, _SENT_NAME)
    _write_xml(folder, b"<" + b" " * MAX_XML_BYTES)

    assert read_claim(folder) == Skipped(SKIP_XML_TOO_LARGE)


def test_read_claim_skips_an_xml_it_cannot_read(tmp_path: Path) -> None:
    """R-7.16: an XML path that cannot be read is skipped with its reason."""
    folder = _folder(tmp_path, _SENT_NAME)
    (folder / "invoice.xml").mkdir()

    assert read_claim(folder) == Skipped(SKIP_XML_UNREADABLE)


def test_read_claim_skips_an_unparseable_xml(tmp_path: Path) -> None:
    """R-7.16: an XML the hardened parser rejects is skipped with its reason."""
    folder = _folder(tmp_path, _SENT_NAME)
    _write_xml(folder, b"<CreditNote/>")

    assert read_claim(folder) == Skipped(SKIP_XML_UNPARSEABLE)


def test_read_claim_skips_an_xml_missing_a_claim_field(tmp_path: Path) -> None:
    """R-7.16: a readable invoice without a required claim field is skipped with its reason."""
    folder = _folder(tmp_path, _SENT_NAME)
    _write_xml(folder, _invoice_xml(ruc_emisor=""))

    assert read_claim(folder) == Skipped(SKIP_FIELDS_MISSING)


def test_import_origin_channel_reads_the_owner_preferred_channel() -> None:
    """R-1.9: an owner profile preferring telegram imports with telegram as the origin channel."""
    document = _example_document()
    document["profiles"]["yo"]["preferred_channel"] = "telegram"
    for channel in document["channels"]:
        if channel["kind"] == "telegram":
            channel["enabled"] = True

    cfg = Config.model_validate(document)

    assert import_origin_channel(cfg) == "telegram"


def test_import_origin_channel_refuses_an_owner_without_a_preferred_channel() -> None:
    """R-1.9: an owner profile with no preferred channel is refused, naming the profile."""
    document = _example_document()
    document["profiles"]["yo"]["preferred_channel"] = None

    cfg = Config.model_validate(document)

    with pytest.raises(ValueError, match="yo"):
        import_origin_channel(cfg)
