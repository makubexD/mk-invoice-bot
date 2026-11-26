"""Canonical inbound/outbound messages and the channel port (PT-1, R-1.9).

Every channel adapter speaks this one contract, so the core never depends on
WhatsApp or Telegram specifics. Attachments travel by reference (R-1.4), not as
bytes, and the sender role lets the pipeline apply admin/owner permissions. An
outbound message may also carry https links and one document (D194).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, NamedTuple, Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from invoicebot.config.models import MediaLimits

type ChannelName = Literal["whatsapp", "telegram", "console"]
type SenderRole = Literal["owner", "admin", "sender"]
type AttachmentKind = Literal["photo", "pdf", "xml", "xlsx", "captcha"]

MENU_MAX = 9999
#: The longest URL, the longest button title and the https port a link may carry (D194).
LINK_URL_MAX = 2048
LINK_TITLE_MAX = 64
HTTPS_PORT = 443
#: A document's MIME type: a type and a subtype of plain token characters (D194).
_MIME = re.compile(r"[\w.+-]+/[\w.+-]+", re.ASCII)


def _has_control(text: str, *, space: bool) -> bool:
    """Return whether ``text`` has a control character, a space too when ``space`` is set (D194)."""
    limit = 0x20 if space else 0x1F
    return any(ord(char) <= limit or ord(char) == 0x7F for char in text)


class Attachment(BaseModel):
    """A media item referenced by the channel, not embedded in the message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: AttachmentKind
    media_ref: str
    mime: str
    size: int


class InboundMessage(BaseModel):
    """One message as received from a channel, in canonical form."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: ChannelName
    external_id: str
    profile_id: str
    sender_role: SenderRole
    text: str = ""
    attachments: list[Attachment] = Field(default_factory=list)
    reply_to_external_id: str | None = None
    button_id: str | None = None
    received_at: datetime


class Button(BaseModel):
    """A reply button or list row: the id the channel echoes back and its label."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    title: str


class ButtonPress(NamedTuple):
    """The invoice, action, optional choice and its menu number a reply id carries."""

    invoice_id: int
    action: str
    choice: int | None
    menu: int | None


class Link(BaseModel):
    """A titled https link an outbound message carries (D194)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=LINK_TITLE_MAX)
    url: str = Field(max_length=LINK_URL_MAX)

    @field_validator("title")
    @classmethod
    def _plain_title(cls, title: str) -> str:
        """Refuse a title with a control character."""
        if _has_control(title, space=False):
            raise ValueError("a link title must not contain a control character")
        return title

    @field_validator("url")
    @classmethod
    def _https_with_host(cls, url: str) -> str:
        """Refuse a URL that is not a plain https URL with a host and no credentials."""
        if _has_control(url, space=True):
            raise ValueError("a link must not contain a control character or a space")
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc or not parsed.hostname:
            raise ValueError("a link must be an https URL with a host")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("a link must not carry credentials")
        if parsed.port not in (None, HTTPS_PORT):
            raise ValueError("a link must use the https port")
        return url


class Document(BaseModel):
    """A file under the data directory that an outbound message sends (D194)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    filename: str = Field(min_length=1, max_length=255)
    mime: str = Field(min_length=1)
    size: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def _absolute(cls, path: str) -> str:
        """Refuse a path that is not absolute."""
        if not Path(path).is_absolute():
            raise ValueError("a document path must be absolute")
        return path

    @field_validator("filename")
    @classmethod
    def _plain_name(cls, filename: str) -> str:
        """Refuse a file name that names another directory or carries a control character."""
        if "/" in filename or "\\" in filename:
            raise ValueError("a document filename must not contain a separator")
        if filename in {".", ".."} or _has_control(filename, space=False):
            raise ValueError("a document filename must be a plain name")
        return filename

    @field_validator("mime")
    @classmethod
    def _mime_type(cls, mime: str) -> str:
        """Refuse a MIME type that is not ``type/subtype`` of token characters."""
        if _MIME.fullmatch(mime) is None:
            raise ValueError("a document mime must look like type/subtype")
        return mime


class OutboundMessage(BaseModel):
    """One message to send through a channel, in canonical form."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: ChannelName
    recipient: str
    profile_id: str
    rule_id: str | None = None
    text: str
    buttons: list[Button] = Field(default_factory=list)
    template: str | None = None
    links: list[Link] = Field(default_factory=list)
    document: Document | None = None


class Channel(Protocol):
    """The port every channel adapter implements (R-1.9)."""

    name: ChannelName

    async def send(self, msg: OutboundMessage) -> str:
        """Send a message and return the channel's external id for it."""
        ...

    async def fetch_media(self, media_ref: str) -> Path:
        """Download media by reference and return the local path."""
        ...


@dataclass(frozen=True)
class DownloadedMedia:
    """A downloaded media file and the bytes that were written (R-1.4)."""

    path: Path
    sha256: str
    size: int
    mime: str


class MediaTransient(Exception):
    """A media download failed in a way a retry may fix (R-1.11)."""


class MediaUnusable(Exception):
    """This media can never be kept, so a retry will not help (R-1.11)."""


class MediaSource(Protocol):
    """The port a channel implements to download one upload's media (R-1.5).

    Implementations write the file under ``out_dir`` and return the stored file with the
    hash and size they wrote. They fail with ``MediaTransient`` when a retry may fix it,
    ``MediaUnusable`` when this media can never be kept, or one of the ``media_limits``
    rejections they raise today, and leave no partial file behind.
    """

    async def download(
        self, media_ref: str, kind: AttachmentKind, out_dir: Path, limits: MediaLimits
    ) -> DownloadedMedia:
        """Download ``media_ref`` into ``out_dir`` and return the file, its hash and size (R-1.4)."""
        ...
