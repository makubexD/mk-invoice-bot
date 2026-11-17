"""The Drive sink: one invoice's files in its folder, with its links (R-6.1, R-5.9, D131).

One job: render the folder path and the file name, create the folders and upload
the configured file kinds. The drive is a port; nothing here talks to HTTP.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from invoicebot.config.models import GoogleDriveSink
from invoicebot.sinks.expr import ExprError, render
from invoicebot.sinks.google_drive import DriveFile, GoogleDrive

#: The file kinds ``sink.upload`` may name (R-6.1).
PHOTO_KIND = "photo"
PDF_KIND = "pdf"
XML_KIND = "xml"

#: The flat values every sink returns for the later sinks and the completion message (D131).
LINK_FOLDER = "link_folder"
LINK_PHOTO = "link_photo"
LINK_PDF = "link_pdf"
LINK_XML = "link_xml"

#: The types Drive stores each kind with (R-6.1).
PHOTO_MIME = "image/jpeg"
PDF_MIME = "application/pdf"
XML_MIME = "application/xml"

#: The separator between the parts of ``sink.path``.
_PATH_SEPARATOR = "/"

#: A rendered path part with one of these names is refused (R-6.1).
_REFUSED_PARTS = frozenset({"", ".", ".."})


@dataclass(frozen=True)
class DriveInput:
    """The invoice's local files to upload: photos, and the PDF and XML when present (R-6.1)."""

    photos: list[Path]
    pdf: bytes | None
    xml: bytes | None


async def file_to_drive(
    sink: GoogleDriveSink,
    drive: GoogleDrive,
    files: DriveInput,
    values: Mapping[str, object],
) -> dict[str, str]:
    """File the invoice on the drive and return its links (R-6.1, R-5.9, D131).

    The folder path is rendered once and walked from ``sink.root_folder_id``;
    the file name is rendered once and shared by every uploaded kind. A kind the
    sink does not list in ``upload`` is skipped, and its link is empty when the
    file is absent, so the caller can still render the completion message.
    """
    folder_id = await _ensure_folders(sink, drive, values)
    name = render(sink.filename, values)
    links = {
        LINK_FOLDER: drive.folder_link(folder_id),
        LINK_PHOTO: "",
        LINK_PDF: "",
        LINK_XML: "",
    }
    if PHOTO_KIND in sink.upload:
        links[LINK_PHOTO] = await _upload_photos(drive, folder_id, name, files.photos)
    if PDF_KIND in sink.upload and files.pdf is not None:
        uploaded = await drive.put_file(folder_id, f"{name}.pdf", files.pdf, PDF_MIME)
        links[LINK_PDF] = uploaded.link
    if XML_KIND in sink.upload and files.xml is not None:
        uploaded = await drive.put_file(folder_id, f"{name}.xml", files.xml, XML_MIME)
        links[LINK_XML] = uploaded.link
    return links


async def _ensure_folders(sink: GoogleDriveSink, drive: GoogleDrive, values: Mapping[str, object]) -> str:
    """Return the id of the last folder in the rendered path (R-6.1)."""
    folder_id = sink.root_folder_id
    for raw_part in render(sink.path, values).split(_PATH_SEPARATOR):
        part = raw_part.strip()
        if part in _REFUSED_PARTS:
            raise ExprError(f"path part {part!r} is not a folder name")
        folder_id = await drive.ensure_folder(folder_id, part)
    return folder_id


async def _upload_photos(drive: GoogleDrive, folder_id: str, name: str, photos: list[Path]) -> str:
    """Upload the photos as ``<name>.jpg``, ``<name>_2.jpg``, … and return the first link (R-6.1)."""
    first_link = ""
    for index, photo in enumerate(photos):
        suffix = "" if index == 0 else f"_{index + 1}"
        uploaded: DriveFile = await drive.put_file(
            folder_id, f"{name}{suffix}.jpg", await asyncio.to_thread(photo.read_bytes), PHOTO_MIME
        )
        if index == 0:
            first_link = uploaded.link
    return first_link
