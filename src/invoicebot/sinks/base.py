"""Ports the delivery sinks talk to (spec 03 §7, D84).

A sink names what it needs, never a concrete storage adapter: OneDrive today,
another drive later, is a new module that implements these protocols.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class DeliveryContext:
    """The invoice data and callbacks one delivery sink needs (R-7.1, R-7.2, D88).

    ``values`` holds the flow's step-6 names for the templates, ``saved`` the
    invoice's flow data so a retried delivery reuses what it already created, and
    ``claim_seq`` turns the largest sequence number listed on the drive into the
    invoice's own, without ever renumbering an earlier folder. ``xml`` is None
    when the invoice has no XML, and then no XML file is filed.
    """

    invoice_id: int
    values: Mapping[str, object]
    pdf: bytes
    xml: bytes | None
    claim_seq: Callable[[int], Awaitable[int]]
    saved: Mapping[str, Any]


@dataclass(frozen=True)
class Attachment:
    """One file in an outgoing mail: its file name, bytes and MIME type."""

    name: str
    data: bytes
    mime: str


@dataclass(frozen=True)
class OutgoingMail:
    """One mail to send: its recipients, subject, body and attachments."""

    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    body: str
    attachments: tuple[Attachment, ...]


@dataclass(frozen=True)
class DriveFile:
    """A file on the drive: its Google id and its browser link (R-6.1)."""

    id: str
    link: str


class Drive(Protocol):
    """One storage target's files and folders.

    Invariants every implementation keeps:

    - every path is absolute from the drive root and starts with ``/``;
    - ``create_folder`` is idempotent: creating a folder whose name already
      exists at that parent changes nothing;
    - ``upload`` replaces a file that already exists at that path.
    """

    async def list_names(self, path: str) -> list[str]:
        """Return the names of the entries directly under ``path``."""
        ...

    async def create_folder(self, parent: str, name: str) -> None:
        """Create the folder ``name`` under ``parent``; an existing one is accepted."""
        ...

    async def upload(self, path: str, data: bytes) -> None:
        """Write ``data`` to the file at ``path``, replacing what was there."""
        ...

    async def download(self, path: str, max_bytes: int) -> bytes:
        """Return the bytes of the file at ``path``, refusing a body over ``max_bytes`` (R-7.14).

        The body is read while streaming, so a file larger than ``max_bytes`` never
        arrives whole; reading stops as soon as the cap is passed.

        Raises:
            FolderMissing: the file at ``path`` does not exist.
            FileTooLarge: the body exceeds ``max_bytes``.
        """
        ...

    async def move(self, path: str, new_parent: str, new_name: str) -> None:
        """Move and rename the item at ``path`` to ``new_parent/new_name`` (R-7.11).

        Raises:
            FolderMissing: the item at ``path`` or ``new_parent`` does not exist.
            FolderExists: an entry with ``new_name`` already exists under ``new_parent``.
        """
        ...

    async def delete(self, path: str) -> None:
        """Delete the item at ``path``; an item already gone is accepted (R-7.14).

        Raises:
            GraphError: the item could not be deleted.
        """
        ...


class FolderDrive(Protocol):
    """The folders and files one delivery sink needs.

    Invariants every implementation keeps:

    - ``ensure_folder`` reuses an existing folder of that name under the parent
      instead of creating a second one;
    - ``put_file`` returns the uploaded file's id and link.
    """

    async def ensure_folder(self, parent_id: str, name: str) -> str:
        """Return the id of the folder ``name`` under ``parent_id``."""
        ...

    async def put_file(self, folder_id: str, name: str, data: bytes, mime: str) -> DriveFile:
        """Upload ``data`` as ``name`` under ``folder_id`` and return its file."""
        ...

    def folder_link(self, folder_id: str) -> str:
        """Return the browser link of the folder ``folder_id``."""
        ...


class FolderLinks(Protocol):
    """The browser link of one item on a drive, for the console's links (D194).

    Invariants every implementation keeps:

    - it returns the item's browser link exactly as Graph gives it, never a
      share or sharing link: reading it creates no permission.
    """

    async def web_url(self, path: str) -> str:
        """Return the browser link of the item at ``path`` (D194).

        Raises:
            FolderMissing: the item at ``path`` does not exist.
            GraphError: the answer carries no HTTPS ``webUrl``.
        """
        ...


class RowSheet(Protocol):
    """The rows one delivery sink needs from a spreadsheet.

    Invariants every implementation keeps:

    - ``keys`` maps each column-A value to its 1-based row number, and raises
      rather than return a partial map when the tab is too long to read whole;
    - ``append`` returns the new row number.
    """

    async def keys(self, spreadsheet_id: str, tab: str) -> dict[str, int]:
        """Return each column-A value in ``tab`` mapped to its row number."""
        ...

    async def append(self, spreadsheet_id: str, tab: str, row: list[str]) -> int:
        """Append ``row`` to ``tab`` and return the row number it landed on."""
        ...

    async def update(self, spreadsheet_id: str, tab: str, row_number: int, row: list[str]) -> None:
        """Write ``row`` over the row at ``row_number`` in ``tab``."""
        ...

    async def row_link(self, spreadsheet_id: str, tab: str, row_number: int) -> str:
        """Return the browser link to ``row_number`` in ``tab``."""
        ...


class TokenSource(Protocol):
    """A bearer token for one account, fetched when needed (D84)."""

    async def token(self) -> str:
        """Return a current access token.

        Raises:
            AuthRequired: the account needs an interactive sign-in.
        """
        ...
