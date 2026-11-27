"""Keep a flow's chosen documents for the [📎 Ver documentos] button (D206, BR-D24)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from invoicebot.channels.base import Document

#: The folder under the data directory that holds the kept documents (D206).
DOCS_DIRECTORY = "docs"

_MIME_BY_EXTENSION = {
    ".pdf": "application/pdf",
    ".xml": "application/xml",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}
_OTHER_MIME = "application/octet-stream"


def _folder(data_dir: Path, invoice_id: int) -> Path:
    """Return the folder that holds one invoice's kept documents."""
    return data_dir / DOCS_DIRECTORY / str(invoice_id)


def _document(path: Path, size: int) -> Document:
    """Build the outbound ``Document`` for a kept file; its name is checked like any document's."""
    mime = _MIME_BY_EXTENSION.get(path.suffix.lower(), _OTHER_MIME)
    return Document(path=str(path.resolve()), filename=path.name, mime=mime, size=size)


def keep_documents(data_dir: Path, invoice_id: int, files: Sequence[tuple[str, bytes]]) -> int:
    """Write each ``(file name, bytes)`` under the invoice's folder and return how many.

    Every name is checked first, so a bad name raises ``ValueError`` and writes nothing.
    """
    folder = _folder(data_dir, invoice_id)
    for name, content in files:
        _document(folder / name, len(content))
        if (folder / name).parent != folder:
            raise ValueError("a document name must be a plain file name")
    folder.mkdir(parents=True, exist_ok=True)
    for name, content in files:
        (folder / name).write_bytes(content)
    return len(files)


def kept_documents(data_dir: Path, invoice_id: int) -> list[Document]:
    """Return the invoice's kept documents sorted by name; empty when the folder is gone."""
    folder = _folder(data_dir, invoice_id)
    if not folder.is_dir():
        return []
    return [_document(path, path.stat().st_size) for path in sorted(folder.iterdir()) if path.is_file()]
