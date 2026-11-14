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


class TokenSource(Protocol):
    """A bearer token for one account, fetched when needed (D84)."""

    async def token(self) -> str:
        """Return a current access token.

        Raises:
            AuthRequired: the account needs an interactive sign-in.
        """
        ...
