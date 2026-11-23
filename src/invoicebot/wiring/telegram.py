"""The Telegram channel builder: config, secrets, client, outbox transport, media and webhook.

This is the composition step the channel registry delegates for the second chat channel;
the routes, transport and media source are mounted only when config and both secret
views are complete (R-1.7, R-9.10, D174).
"""

from __future__ import annotations

import logging
from functools import partial

import httpx

from invoicebot.api.intake_route import IntakeDeps
from invoicebot.api.telegram_webhook import TelegramWebhookDeps, make_router
from invoicebot.channels.base import ChannelName
from invoicebot.channels.profiles import ProfileIndex
from invoicebot.channels.telegram.client import TelegramClient
from invoicebot.channels.telegram.media import TelegramMediaSource
from invoicebot.channels.telegram.transport import make_transport
from invoicebot.config.models import Config, TelegramChannel
from invoicebot.queue.outbox import Transport
from invoicebot.secrets import Secrets, SecretStore
from invoicebot.store.db import Pool
from invoicebot.wiring.channels import (
    SENDER_HASH_KEY,
    ChannelBuilder,
    ChannelWiring,
    alert_channel_token,
    channel_secrets,
)

logger = logging.getLogger(__name__)

TELEGRAM_ADAPTER: ChannelName = "telegram"
#: Secrets the Telegram channel declares (R-9.10).
TELEGRAM_SECRETS = frozenset({"telegram_bot_token", "telegram_webhook_secret"})


class TelegramBuilder:
    """Builds the Telegram channel, or ``None`` while its config or secrets are missing."""

    def configured(self, cfg: Config) -> bool:
        """Return whether ``cfg`` enables this channel, whatever its secrets (D183)."""
        return _telegram_channel(cfg) is not None

    def __call__(
        self, cfg: Config, store: SecretStore, http: httpx.AsyncClient, intake_deps: IntakeDeps
    ) -> ChannelWiring | None:
        """Return the Telegram wiring, or ``None`` when it must stay disabled (D174)."""
        channel = _telegram_channel(cfg)
        if channel is None:
            # Not configured: Telegram is optional since D174, so this is not an error.
            return None
        secrets, shared = _telegram_secrets(store), channel_secrets(store)
        if not secrets.enabled or not shared.enabled:
            # Fail closed: a configured channel without its secrets is not mounted.
            logger.error("telegram channel disabled: missing %s", sorted(secrets.missing | shared.missing))
            return None
        client = TelegramClient(http, token=secrets.get("telegram_bot_token"))
        return ChannelWiring(
            name=TELEGRAM_ADAPTER,
            transport=_transport(cfg, client, intake_deps.pool),
            media_source=TelegramMediaSource(client),
            router=make_router(_webhook_deps(cfg, secrets, shared, intake_deps, client)),
        )

    def secret_problem(self, cfg: Config, store: SecretStore) -> str | None:
        """Return the code of the first secret a configured Telegram channel lacks (R-9.6)."""
        if not self.configured(cfg):
            return None
        if not _telegram_secrets(store).enabled:
            return "secrets:telegram"
        if not channel_secrets(store).enabled:
            return "secrets:channels"
        return None


#: The builder ``app.py`` registers in its channel tuple (D174).
telegram_wiring: ChannelBuilder = TelegramBuilder()


def _transport(cfg: Config, client: TelegramClient, pool: Pool) -> Transport:
    """Return the outbox transport that delivers one Telegram message per row (D180, R-1.10)."""
    user_ids = {
        name: user_id
        for name, profile in cfg.profiles.items()
        if (user_id := profile.telegram_user_id) is not None
    }
    return make_transport(
        client,
        user_ids=user_ids,
        on_token_rejected=partial(alert_channel_token, pool, cfg, "Telegram"),
    )


def _webhook_deps(
    cfg: Config, secrets: Secrets, shared: Secrets, intake_deps: IntakeDeps, client: TelegramClient
) -> TelegramWebhookDeps:
    """Collect what the Telegram webhook route needs from the config and the channel's secrets."""
    return TelegramWebhookDeps(
        intake_deps=intake_deps,
        index=ProfileIndex.from_config(cfg),
        owner_profile=cfg.owner.profile,
        webhook_secret=secrets.get("telegram_webhook_secret"),
        sender_hash_key=shared.get(SENDER_HASH_KEY),
        client=client,
    )


def _telegram_channel(cfg: Config) -> TelegramChannel | None:
    """Return the enabled Telegram channel, if any (D174)."""
    return next(
        (channel for channel in cfg.channels if isinstance(channel, TelegramChannel) and channel.enabled),
        None,
    )


def _telegram_secrets(store: SecretStore) -> Secrets:
    """Return the age-store view the Telegram channel reads its secrets through (R-9.10)."""
    return store.scoped(TELEGRAM_ADAPTER, set(TELEGRAM_SECRETS))
