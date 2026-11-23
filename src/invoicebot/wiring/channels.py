"""The port a chat channel's builder implements and what a built channel gives the runtime."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

import httpx
from fastapi import APIRouter

from invoicebot.api.intake_route import IntakeDeps
from invoicebot.channels.base import ChannelName, MediaSource
from invoicebot.config.models import Config, Profile, enabled_chat_channels
from invoicebot.pipeline.outgoing import queue_rate_limited_alert
from invoicebot.queue.outbox import Transport
from invoicebot.rules.messages_es import MESSAGES
from invoicebot.secrets import Secrets, SecretStore
from invoicebot.store.db import Pool

logger = logging.getLogger(__name__)

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

    Implementers fail closed: ``__call__`` returns ``None`` exactly when
    :meth:`configured` is false or the channel's declared secrets are incomplete,
    and a disabled channel keeps its router unmounted (D174).
    """

    def configured(self, cfg: Config) -> bool:
        """Return whether ``cfg`` enables this channel, whatever its secrets (D183)."""

    def __call__(
        self, cfg: Config, store: SecretStore, http: httpx.AsyncClient, intake_deps: IntakeDeps
    ) -> ChannelWiring | None:
        """Return the channel's wiring, or ``None`` when it must stay disabled."""

    def secret_problem(self, cfg: Config, store: SecretStore) -> str | None:
        """Return the first missing-secret code the channel needs, or ``None`` (R-9.6)."""


def alert_profiles(cfg: Config, channel: str) -> dict[str, Profile]:
    """Return the profiles BR-O03 alerts, with each admin moved off the rejected channel when it has
    another route (D185).
    """
    rejected = channel.lower()
    fallbacks = enabled_chat_channels(cfg.channels) - {rejected}
    profiles: dict[str, Profile] = {}
    for profile_id, profile in cfg.profiles.items():
        fallback = _fallback_channel(profile, rejected, fallbacks)
        profiles[profile_id] = (
            profile if fallback is None else profile.model_copy(update={"preferred_channel": fallback})
        )
    return profiles


def _fallback_channel(profile: Profile, rejected: str, fallbacks: set[str]) -> str | None:
    """Return the sorted first enabled channel other than ``rejected`` the profile has an id for (D185)."""
    if not profile.admin or profile.preferred_channel != rejected:
        return None
    sender_ids = {"whatsapp": profile.whatsapp, "telegram": profile.telegram_user_id}
    return next((name for name in sorted(fallbacks) if sender_ids.get(name) is not None), None)


async def alert_channel_token(pool: Pool, cfg: Config, channel: str) -> None:
    """Queue BR-O03's rate-limited owner alert for a rejected channel token (BR-O03)."""
    # D185: an admin whose preferred channel is the rejected one gets the alert on another enabled channel.
    logger.error("channel token rejected", extra={"channel": channel})
    async with pool.connection() as conn, conn.transaction():
        await queue_rate_limited_alert(
            conn,
            alert_profiles(cfg, channel),
            f"BR-O03:{channel.lower()}",
            timedelta(hours=cfg.ops.self_alerts.realert_hours),
            "BR-O03",
            MESSAGES["BR-O03"].format(channel=channel),
        )
