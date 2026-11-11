"""Resolve channel senders to config profiles (R-1.9).

Profiles live in config: each one links at most one WhatsApp number and one
Telegram user id. Lookups are kept per channel so the same digits on another
channel never match.
"""

from __future__ import annotations

from invoicebot.channels.base import ChannelName
from invoicebot.config.models import Config, Profile


class ProfileIndex:
    """Map a WhatsApp number or Telegram user id to its config profile id."""

    def __init__(self, profiles: dict[str, Profile]) -> None:
        self._profiles = profiles
        self._ids: dict[ChannelName, dict[str, str]] = {"whatsapp": {}, "telegram": {}}
        for profile_id, profile in profiles.items():
            if profile.whatsapp is not None:
                self._ids["whatsapp"][str(profile.whatsapp)] = profile_id
            if profile.telegram_user_id is not None:
                self._ids["telegram"][str(profile.telegram_user_id)] = profile_id

    @classmethod
    def from_config(cls, cfg: Config) -> ProfileIndex:
        """Build the index from the loaded runtime config."""
        return cls(cfg.profiles)

    def resolve(self, channel: ChannelName, sender_id: str | int) -> str | None:
        """Return the profile id for a sender, or ``None`` when not allowlisted.

        Ids are compared as strings, but only within the sender's own channel.
        """
        return self._ids[channel].get(str(sender_id))

    def reply_channel(self, invoice_origin_channel: ChannelName) -> ChannelName:
        """Replies about an invoice always go back to its origin channel."""
        return invoice_origin_channel

    def admin_channel(self, owner_profile: str) -> ChannelName:
        """Admin alerts go to the owner profile's preferred channel."""
        profile = self._profiles[owner_profile]
        if profile.preferred_channel is None:
            raise ValueError(f"profile {owner_profile!r} has no preferred_channel")
        return profile.preferred_channel
