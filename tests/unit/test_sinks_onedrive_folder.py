"""Tests for the OneDrive folder sink (R-7.1, R-7.2, R-7.11, D88)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

from invoicebot.config.models import OneDriveFolderSink, SinkFiles
from invoicebot.sinks.base import DeliveryContext
from invoicebot.sinks.onedrive_folder import file_folder

ROOT = "/Reembolsos"
FOLDER_TEMPLATE = "{fecha:%Y}-{fecha:%m}-Support_{seq}_{comercio}"
INVOICE_ID = 42
VALUES = {"fecha": date(2026, 11, 5), "comercio": "LaLena", "profile": {"legal_name": "LaLena SAC"}}


class FakeDrive:
    """An in-memory Drive: folders hold their entry names and uploads keep their bytes."""

    def __init__(self, folders: dict[str, list[str]] | None = None) -> None:
        self.folders = {path: list(names) for path, names in (folders or {}).items()}
        self.listings: list[str] = []
        self.created: list[tuple[str, str]] = []
        self.uploads: dict[str, bytes] = {}

    async def list_names(self, path: str) -> list[str]:
        """Return the entries of a folder; a path that does not exist has none."""
        self.listings.append(path)
        return list(self.folders.get(path, []))

    async def create_folder(self, parent: str, name: str) -> None:
        """Record the creation and list the new folder under its parent."""
        self.created.append((parent, name))
        self.folders.setdefault(parent, []).append(name)

    async def upload(self, path: str, data: bytes) -> None:
        """Keep the file's bytes at its absolute path."""
        self.uploads[path] = data


def _sink() -> OneDriveFolderSink:
    """Return the flow B folder sink with the step's example templates."""
    return OneDriveFolderSink(
        kind="onedrive_folder",
        targets=["onedrive_personal"],
        root=ROOT,
        folder=FOLDER_TEMPLATE,
        files=SinkFiles(pdf="{profile.legal_name}.pdf", xml="{profile.legal_name}.xml"),
    )


def _ctx(saved: dict[str, object] | None = None) -> tuple[DeliveryContext, list[int]]:
    """Return a delivery context and the listed maxima its claim callback received."""
    claimed: list[int] = []

    async def claim(listed_max: int) -> int:
        claimed.append(listed_max)
        return listed_max + 1

    ctx = DeliveryContext(
        invoice_id=INVOICE_ID,
        values=VALUES,
        pdf=b"%PDF synthetic",
        xml=b"<Invoice/>",
        claim_seq=claim,
        saved=saved or {},
    )
    return ctx, claimed


async def test_empty_month_files_the_first_folder() -> None:
    """R-7.1, R-7.2, D88: an empty month files Support_1 with its PDF, XML and manifest."""
    drive = FakeDrive({ROOT: [], f"{ROOT}/2026": []})
    ctx, claimed = _ctx()

    result = await file_folder(_sink(), drive, ctx)

    assert result.folder == "2026-11-Support_1_LaLena"
    assert result.path == f"{ROOT}/2026-11-Support_1_LaLena"
    assert result.seq == 1
    assert claimed == [0]
    assert drive.created == [(ROOT, "2026-11-Support_1_LaLena")]
    assert set(drive.uploads) == {
        f"{result.path}/LaLena SAC.pdf",
        f"{result.path}/LaLena SAC.xml",
        f"{result.path}/.invoicebot.json",
    }
    assert drive.uploads[f"{result.path}/LaLena SAC.pdf"] == b"%PDF synthetic"
    manifest = json.loads(drive.uploads[f"{result.path}/.invoicebot.json"])
    assert manifest == {"invoice_id": INVOICE_ID, "version": 1}


async def test_an_invoice_without_xml_files_no_xml() -> None:
    """R-7.2: an invoice with no XML files its PDF and manifest, never an empty XML."""
    drive = FakeDrive({ROOT: [], f"{ROOT}/2026": []})
    ctx, _ = _ctx()

    result = await file_folder(_sink(), drive, replace(ctx, xml=None))

    assert set(drive.uploads) == {
        f"{result.path}/LaLena SAC.pdf",
        f"{result.path}/.invoicebot.json",
    }


async def test_existing_folders_in_root_and_year_claim_the_next_number() -> None:
    """R-7.1, R-7.11, D88: root and year folders count; the claim gets 4 and the folder gets 5."""
    drive = FakeDrive(
        {
            ROOT: ["2026-11-Support_2_X"],
            f"{ROOT}/2026": ["2026-11-Support_4_Y_Sent_PAID_15_12___144_6"],
        }
    )
    ctx, claimed = _ctx()

    result = await file_folder(_sink(), drive, ctx)

    assert drive.listings == [ROOT, f"{ROOT}/2026"]
    assert claimed == [4]
    assert result.seq == 5
    assert result.folder == "2026-11-Support_5_LaLena"


async def test_a_saved_folder_is_reused_without_listing_or_claiming() -> None:
    """R-7.1, R-7.2, D88: a saved folder and seq are reused; nothing is listed or claimed again."""
    drive = FakeDrive()
    ctx, claimed = _ctx(saved={"folder": "2026-11-Support_7_LaLena", "seq": 7})

    result = await file_folder(_sink(), drive, ctx)

    assert result.folder == "2026-11-Support_7_LaLena"
    assert result.path == f"{ROOT}/2026-11-Support_7_LaLena"
    assert result.seq == 7
    assert drive.listings == []
    assert claimed == []
    assert drive.created == [(ROOT, "2026-11-Support_7_LaLena")]
    assert f"{result.path}/LaLena SAC.pdf" in drive.uploads
    assert f"{result.path}/.invoicebot.json" in drive.uploads
