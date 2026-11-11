"""In-memory channel adapter for conversation tests.

Records what the pipeline sends and resolves media references registered by the
test, so a conversation can run end to end without a network or a real channel.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from invoicebot.channels.base import Channel, ChannelName, OutboundMessage
from invoicebot.queue.outbox import OutboxRow, Transport


class FakeChannel:
    """A :class:`Channel` that keeps sent messages and media paths in memory."""

    name: ChannelName = "whatsapp"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []
        self._media: dict[str, Path] = {}

    async def send(self, msg: OutboundMessage) -> str:
        """Record msg and return a synthetic external id for it."""
        self.sent.append(msg)
        return f"fake-{len(self.sent)}"

    async def fetch_media(self, media_ref: str) -> Path:
        """Return the path registered for media_ref.

        Raises:
            KeyError: when no path was registered under media_ref.
        """
        return self._media[media_ref]

    def add_media(self, media_ref: str, path: Path) -> None:
        """Register the local path that :meth:`fetch_media` returns."""
        self._media[media_ref] = path

    def rule_ids(self) -> list[str | None]:
        """Return the rule id of each sent message, in order."""
        return [msg.rule_id for msg in self.sent]

    def texts(self) -> list[str]:
        """Return the text of each sent message, in order."""
        return [msg.text for msg in self.sent]

    def transport(self) -> Transport:
        """Return an outbox transport that delivers rows through :meth:`send`."""

        async def deliver(row: OutboxRow) -> None:
            await self.send(OutboundMessage.model_validate(row.payload))

        return deliver


if TYPE_CHECKING:
    _: Channel = FakeChannel()
