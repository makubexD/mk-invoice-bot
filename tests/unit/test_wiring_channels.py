"""Unit tests for BR-O03's fallback channel (BR-O03, D185)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from invoicebot.config.loader import load_config
from invoicebot.config.models import Channel, Config, Profile, TelegramChannel, WhatsAppChannel
from invoicebot.wiring.channels import alert_profiles

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "example.yaml"


def _profile(**changes: Any) -> Profile:
    """Return the example's admin profile, with ``changes`` applied."""
    return load_config(EXAMPLE).profiles["yo"].model_copy(update=changes)


def _config(profiles: dict[str, Profile], *, whatsapp: bool = True) -> Config:
    """Return the example config with Telegram enabled, WhatsApp optional, and ``profiles`` (D174)."""
    cfg = load_config(EXAMPLE)
    channels: list[Channel] = []
    if whatsapp:
        channels.extend(channel for channel in cfg.channels if isinstance(channel, WhatsAppChannel))
    channels.extend(
        channel.model_copy(update={"enabled": True})
        for channel in cfg.channels
        if isinstance(channel, TelegramChannel)
    )
    return cfg.model_copy(update={"channels": channels, "profiles": profiles})


def test_admin_preferring_rejected_telegram_moves_to_whatsapp() -> None:
    """BR-O03, D185: an admin who prefers the rejected channel gets the alert on WhatsApp."""
    cfg = _config({"yo": _profile(preferred_channel="telegram")})

    profiles = alert_profiles(cfg, "telegram")

    assert profiles["yo"].preferred_channel == "whatsapp"


def test_admin_without_another_route_keeps_the_rejected_channel() -> None:
    """BR-O03, D185: an admin with no id on another enabled channel keeps its preferred one."""
    cfg = _config({"yo": _profile(preferred_channel="telegram", whatsapp=None)})

    profiles = alert_profiles(cfg, "telegram")

    assert profiles["yo"].preferred_channel == "telegram"


def test_disabled_whatsapp_is_not_a_fallback() -> None:
    """BR-O03, D185: a disabled WhatsApp channel never receives the fallback alert."""
    cfg = _config({"yo": _profile(preferred_channel="telegram")}, whatsapp=False)

    profiles = alert_profiles(cfg, "telegram")

    assert profiles["yo"].preferred_channel == "telegram"


def test_admin_preferring_another_channel_is_unchanged() -> None:
    """BR-O03, D185: an admin whose preferred channel is not the rejected one is unchanged."""
    cfg = _config({"yo": _profile()})

    profiles = alert_profiles(cfg, "telegram")

    assert profiles["yo"].preferred_channel == "whatsapp"


def test_non_admin_preferring_the_rejected_channel_is_unchanged() -> None:
    """BR-O03, D185: only admin profiles are rerouted."""
    cfg = _config({"yo": _profile(preferred_channel="telegram", admin=False)})

    profiles = alert_profiles(cfg, "telegram")

    assert profiles["yo"].preferred_channel == "telegram"


def test_admin_preferring_rejected_whatsapp_moves_to_telegram() -> None:
    """BR-O03, D185: the reroute applies when WhatsApp is the rejected channel too."""
    cfg = _config({"yo": _profile()})

    profiles = alert_profiles(cfg, "whatsapp")

    assert profiles["yo"].preferred_channel == "telegram"


def test_alert_profiles_does_not_mutate_the_config() -> None:
    """BR-O03, D185: rerouting returns copies and leaves ``cfg.profiles`` untouched."""
    cfg = _config({"yo": _profile(preferred_channel="telegram")})
    before = dict(cfg.profiles)

    profiles = alert_profiles(cfg, "telegram")

    assert cfg.profiles == before
    assert profiles["yo"] is not cfg.profiles["yo"]
