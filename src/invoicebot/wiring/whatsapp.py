"""The WhatsApp channel builder: config, secrets, client, outbox transport, media and webhook.

This is the composition step ``app.py`` performed inline before the channel registry; the
routes, transport and media source are the same ones, mounted only when config and both
secret views are complete (R-1.7, R-9.10, D174).
"""

from __future__ import annotations

import logging
from functools import partial
from pathlib import Path

import httpx

from invoicebot.api.intake_route import IntakeDeps
from invoicebot.api.webhook import WebhookDeps, make_router
from invoicebot.channels.base import ChannelName
from invoicebot.channels.profiles import ProfileIndex
from invoicebot.channels.whatsapp.client import WhatsAppClient
from invoicebot.channels.whatsapp.media import WhatsAppMediaSource
from invoicebot.channels.whatsapp.transport import make_transport
from invoicebot.config.models import Config, WhatsAppChannel
from invoicebot.queue.outbox import Transport
from invoicebot.secrets import Secrets, SecretStore
from invoicebot.store.db import Pool
from invoicebot.wiring.channels import (
    SENDER_HASH_KEY,
    ChannelWiring,
    alert_channel_token,
    channel_secrets,
)

logger = logging.getLogger(__name__)

#: Secrets the WhatsApp channel declares (R-9.10).
WHATSAPP_ADAPTER: ChannelName = "whatsapp"
WHATSAPP_SECRETS = frozenset({"whatsapp_token", "whatsapp_app_secret", "whatsapp_verify_token"})


class WhatsAppBuilder:
    """Builds the WhatsApp channel, or ``None`` while its config or secrets are missing.

    It is bound to the process's data directory, where the transport reads documents (D194).
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def configured(self, cfg: Config) -> bool:
        """Return whether ``cfg`` enables this channel, whatever its secrets (D183)."""
        return _whatsapp_channel(cfg) is not None

    def __call__(
        self, cfg: Config, store: SecretStore, http: httpx.AsyncClient, intake_deps: IntakeDeps
    ) -> ChannelWiring | None:
        """Return the WhatsApp wiring, or ``None`` when it must stay disabled (D174)."""
        channel = _whatsapp_channel(cfg)
        if channel is None:
            # Not configured: WhatsApp is optional since D174, so this is not an error.
            return None
        secrets, shared = _whatsapp_secrets(store), channel_secrets(store)
        if not secrets.enabled or not shared.enabled:
            # Fail closed: a configured channel without its secrets is not mounted.
            logger.error("whatsapp channel disabled: missing %s", sorted(secrets.missing | shared.missing))
            return None
        client = WhatsAppClient(
            http,
            phone_number_id=channel.phone_number_id,
            graph_api_version=channel.graph_api_version,
            token=secrets.get("whatsapp_token"),
        )
        return ChannelWiring(
            name=WHATSAPP_ADAPTER,
            transport=_transport(cfg, channel, client, intake_deps.pool, self._data_dir),
            media_source=WhatsAppMediaSource(client),
            router=make_router(_webhook_deps(cfg, secrets, shared, intake_deps)),
        )

    def secret_problem(self, cfg: Config, store: SecretStore) -> str | None:
        """Return the code of the first secret a configured WhatsApp channel lacks (R-9.6)."""
        if not self.configured(cfg):
            return None
        if not _whatsapp_secrets(store).enabled:
            return "secrets:whatsapp"
        if not channel_secrets(store).enabled:
            return "secrets:channels"
        return None


def _transport(
    cfg: Config, channel: WhatsAppChannel, client: WhatsAppClient, pool: Pool, data_dir: Path
) -> Transport:
    """Return the outbox transport that delivers one WhatsApp message per row (R-1.10, PT-4)."""
    numbers = {name: profile.whatsapp for name, profile in cfg.profiles.items() if profile.whatsapp}
    return make_transport(
        client,
        pool,
        numbers=numbers,
        data_dir=data_dir,
        channel=channel,
        on_token_rejected=partial(alert_channel_token, pool, cfg, "WhatsApp"),
    )


def _webhook_deps(cfg: Config, secrets: Secrets, shared: Secrets, intake_deps: IntakeDeps) -> WebhookDeps:
    """Collect what the webhook routes need from the config and the channel's secrets."""
    return WebhookDeps(
        intake_deps=intake_deps,
        index=ProfileIndex.from_config(cfg),
        owner_profile=cfg.owner.profile,
        app_secret=secrets.get("whatsapp_app_secret"),
        verify_token=secrets.get("whatsapp_verify_token"),
        sender_hash_key=shared.get(SENDER_HASH_KEY),
    )


def _whatsapp_channel(cfg: Config) -> WhatsAppChannel | None:
    """Return the configured WhatsApp channel, if any."""
    return next((channel for channel in cfg.channels if isinstance(channel, WhatsAppChannel)), None)


def _whatsapp_secrets(store: SecretStore) -> Secrets:
    """Return the age-store view the WhatsApp channel reads its secrets through (R-9.10)."""
    return store.scoped(WHATSAPP_ADAPTER, set(WHATSAPP_SECRETS))
