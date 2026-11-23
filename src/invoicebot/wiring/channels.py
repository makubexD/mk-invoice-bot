"""The port a chat channel's builder implements and what a built channel gives the runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx
from fastapi import APIRouter

from invoicebot.api.intake_route import IntakeDeps
from invoicebot.channels.base import ChannelName, MediaSource
from invoicebot.config.models import Config
from invoicebot.queue.outbox import Transport
from invoicebot.secrets import Secrets, SecretStore

#: The one secret every chat channel declares: senders are logged only as its keyed hash (R-9.10, R-9.16).
CHANNEL_ADAPTER = "channels"
SENDER_HASH_KEY = "sender_hash_key"
CHANNEL_SECRETS = frozenset({SENDER_HASH_KEY})


def channel_secrets(store: SecretStore) -> Secrets:
    """Return the age-store view every chat channel reads the shared secrets through (R-9.10)."""
    return store.scoped(CHANNEL_ADAPTER, set(CHANNEL_SECRETS))


@dataclass(frozen=True)
class ChannelWiring:
    """One chat channel as the runtime uses it: outbound, media and, for webhooks, a router."""

    name: ChannelName
    transport: Transport
    media_source: MediaSource | None
    router: APIRouter | None


class ChannelBuilder(Protocol):
    """Builds one chat channel, or ``None`` when its config or secrets are missing.

    Implementers fail closed: a channel that is not configured, or whose declared
    secrets are incomplete, returns ``None`` and keeps its router unmounted (D174).
    """

    def __call__(
        self, cfg: Config, store: SecretStore, http: httpx.AsyncClient, intake_deps: IntakeDeps
    ) -> ChannelWiring | None:
        """Return the channel's wiring, or ``None`` when it must stay disabled."""

    def secret_problem(self, cfg: Config, store: SecretStore) -> str | None:
        """Return the first missing-secret code the channel needs, or ``None`` (R-9.6)."""
