"""Tests for the Drive sink: folders, uploads and flat links (R-6.1, R-5.9, D131)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, GoogleDriveSink
from invoicebot.sinks.drive_files import (
    LINK_FOLDER,
    LINK_PDF,
    LINK_PHOTO,
    LINK_XML,
    PDF_MIME,
    PHOTO_MIME,
    XML_MIME,
    DriveInput,
    file_to_drive,
)
from invoicebot.sinks.expr import ExprError
from invoicebot.sinks.google_drive import DriveFile

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "example.yaml"

#: Synthetic invoice values only; nothing here belongs to a real invoice.
FECHA = date(2026, 11, 5)
SERIE = "F001"
NUMERO = "123"


@dataclass(frozen=True)
class Upload:
    """One recorded ``put_file`` call."""

    folder_id: str
    name: str
    data: bytes
    mime: str
    file_id: str


class FakeDrive:
    """An in-memory Drive that records the folders it makes and the files it receives (R-6.1)."""

    def __init__(self) -> None:
        self.ensured: list[tuple[str, str, str]] = []
        self.uploads: list[Upload] = []
        self._next_id = 0

    async def ensure_folder(self, parent_id: str, name: str) -> str:
        """Record the folder and return a new id under its parent."""
        folder_id = self._new_id("folder")
        self.ensured.append((parent_id, name, folder_id))
        return folder_id

    async def put_file(self, folder_id: str, name: str, data: bytes, mime: str) -> DriveFile:
        """Record the upload and return a drive file with a new id and link."""
        file_id = self._new_id("file")
        self.uploads.append(Upload(folder_id, name, data, mime, file_id))
        return DriveFile(id=file_id, link=f"https://drive.example/files/{file_id}")

    def folder_link(self, folder_id: str) -> str:
        """Return the browser link the fake reports for a folder."""
        return f"https://drive.example/folders/{folder_id}"

    def _new_id(self, prefix: str) -> str:
        """Return an id shaped like the ones Google sends (``^[A-Za-z0-9_-]{10,200}$``)."""
        self._next_id += 1
        return f"{prefix}_{self._next_id:010d}"


@pytest.fixture(scope="module")
def example_config() -> Config:
    """Load the shipped example config the way the app does (R-9.6)."""
    return load_config(EXAMPLE)


@pytest.fixture(scope="module")
def example_sink(example_config: Config) -> GoogleDriveSink:
    """Return the first Drive sink in ``config/example.yaml`` (R-6.1)."""
    for flow in example_config.flows.values():
        for sink in flow.sinks:
            if isinstance(sink, GoogleDriveSink):
                return sink
    raise AssertionError("config/example.yaml has no google_drive sink")


@pytest.fixture(scope="module")
def values(example_config: Config) -> dict[str, object]:
    """Return the synthetic invoice values; the issuer placeholder comes from the example config (D27)."""
    return {
        "fecha": FECHA,
        "ruc_emisor": example_config.owner.ruc,
        "serie": SERIE,
        "numero": NUMERO,
    }


def _photo(tmp_path: Path, name: str, data: bytes) -> Path:
    """Write a synthetic photo under ``tmp_path`` and return its path."""
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _expected_name(values: dict[str, object]) -> str:
    """Return the file name the example sink's filename template renders."""
    return f"2026-11-05_{values['ruc_emisor']}_{SERIE}-{NUMERO}"


async def test_example_sink_files_under_the_year_and_month(
    example_sink: GoogleDriveSink, values: dict[str, object], tmp_path: Path
) -> None:
    """R-6.1: the example sink files the photo, PDF and XML under 2026/11 with the invoice's name."""
    photo = _photo(tmp_path, "photo.jpg", b"synthetic-photo")
    drive = FakeDrive()

    links = await file_to_drive(
        example_sink, drive, DriveInput([photo], b"synthetic-pdf", b"<Invoice/>"), values
    )

    name = _expected_name(values)
    folder_id = drive.ensured[-1][2]
    assert [folder for _, folder, _ in drive.ensured] == ["2026", "11"]
    assert drive.ensured[0][0] == example_sink.root_folder_id
    assert drive.ensured[1][0] == drive.ensured[0][2]
    assert [(upload.name, upload.data, upload.mime) for upload in drive.uploads] == [
        (f"{name}.jpg", b"synthetic-photo", PHOTO_MIME),
        (f"{name}.pdf", b"synthetic-pdf", PDF_MIME),
        (f"{name}.xml", b"<Invoice/>", XML_MIME),
    ]
    assert all(upload.folder_id == folder_id for upload in drive.uploads)
    assert links == {
        LINK_FOLDER: drive.folder_link(folder_id),
        LINK_PHOTO: f"https://drive.example/files/{drive.uploads[0].file_id}",
        LINK_PDF: f"https://drive.example/files/{drive.uploads[1].file_id}",
        LINK_XML: f"https://drive.example/files/{drive.uploads[2].file_id}",
    }


async def test_two_photos_get_the_sequence_suffix(
    example_sink: GoogleDriveSink, values: dict[str, object], tmp_path: Path
) -> None:
    """R-6.1: a second photo is uploaded as ``<name>_2.jpg`` and the first photo is the link."""
    first = _photo(tmp_path, "first.jpg", b"first")
    second = _photo(tmp_path, "second.jpg", b"second")
    drive = FakeDrive()

    links = await file_to_drive(example_sink, drive, DriveInput([first, second], None, None), values)

    name = _expected_name(values)
    assert [upload.name for upload in drive.uploads] == [f"{name}.jpg", f"{name}_2.jpg"]
    assert links[LINK_PHOTO] == f"https://drive.example/files/{drive.uploads[0].file_id}"


async def test_absent_pdf_leaves_its_link_empty(
    example_sink: GoogleDriveSink, values: dict[str, object], tmp_path: Path
) -> None:
    """R-5.9: a delivery without a PDF uploads nothing for it and returns an empty link."""
    photo = _photo(tmp_path, "photo.jpg", b"synthetic-photo")
    drive = FakeDrive()

    links = await file_to_drive(example_sink, drive, DriveInput([photo], None, b"<Invoice/>"), values)

    assert links[LINK_PDF] == ""
    assert [upload.mime for upload in drive.uploads] == [PHOTO_MIME, XML_MIME]


@pytest.mark.parametrize(
    "path",
    ["", "2026//11", "2026/./11", "2026/../11", "2026/ .. /11"],
)
async def test_a_path_part_that_is_not_a_folder_is_refused(
    example_sink: GoogleDriveSink, values: dict[str, object], path: str
) -> None:
    """R-6.1: an empty, ``.`` or ``..`` path part is refused, whitespace included."""
    sink = example_sink.model_copy(update={"path": path})

    with pytest.raises(ExprError):
        await file_to_drive(sink, FakeDrive(), DriveInput([], None, None), values)


async def test_a_kind_not_in_upload_is_not_uploaded(
    example_sink: GoogleDriveSink, values: dict[str, object], tmp_path: Path
) -> None:
    """R-6.1: only the kinds listed in ``upload`` are sent to the drive."""
    photo = _photo(tmp_path, "photo.jpg", b"synthetic-photo")
    sink = example_sink.model_copy(update={"upload": ["pdf"]})
    drive = FakeDrive()

    links = await file_to_drive(sink, drive, DriveInput([photo], b"synthetic-pdf", b"<Invoice/>"), values)

    assert [upload.name for upload in drive.uploads] == [f"{_expected_name(values)}.pdf"]
    assert links[LINK_PHOTO] == ""
    assert links[LINK_PDF] != ""
    assert links[LINK_XML] == ""
