"""Canonical inbound/outbound messages and the channel port (PT-1, R-1.9).

Every channel adapter speaks this one contract, so the core never depends on
WhatsApp or Telegram specifics. Attachments travel by reference (R-1.4), not as
bytes, and the sender role lets the pipeline apply admin/owner permissions.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

type ChannelName = Literal["whatsapp", "telegram"]
type SenderRole = Literal["owner", "admin", "sender"]
type AttachmentKind = Literal["photo", "pdf", "xml", "xlsx", "captcha"]


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
    received_at: datetime


class OutboundMessage(BaseModel):
    """One message to send through a channel, in canonical form."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: ChannelName
    recipient: str
    profile_id: str
    rule_id: str | None = None
    text: str
    buttons: list[str] = Field(default_factory=list)
    template: str | None = None


class Channel(Protocol):
    """The port every channel adapter implements (R-1.9)."""

    name: ChannelName

    async def send(self, msg: OutboundMessage) -> str:
        """Send a message and return the channel's external id for it."""
        ...

    async def fetch_media(self, media_ref: str) -> Path:
        """Download media by reference and return the local path."""
        ...
