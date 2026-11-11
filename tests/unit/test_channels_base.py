"""Tests for the channel port's profile resolution (R-1.9)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from invoicebot.channels.profiles import ProfileIndex
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"


@pytest.fixture(scope="module")
def cfg() -> Iterator[Config]:
    yield load_config(EXAMPLE)


@pytest.fixture(scope="module")
def index(cfg: Config) -> Iterator[ProfileIndex]:
    yield ProfileIndex.from_config(cfg)


def test_same_profile_on_both_channels(index: ProfileIndex) -> None:
    """R-1.9: a profile's WhatsApp number and Telegram id resolve to one profile_id."""
    assert index.resolve("whatsapp", "51999999999") == "yo"
    assert index.resolve("telegram", 100000001) == "yo"


def test_unknown_sender_resolves_to_none(index: ProfileIndex) -> None:
    """R-1.9: an id that is not in the allowlist resolves to None."""
    assert index.resolve("whatsapp", "51900000000") is None
    assert index.resolve("telegram", 999999999) is None


def test_replies_go_to_the_origin_channel(index: ProfileIndex) -> None:
    """R-1.9: a reply about an invoice keeps the channel the invoice came from."""
    assert index.reply_channel("whatsapp") == "whatsapp"
    assert index.reply_channel("telegram") == "telegram"


def test_admin_alerts_go_to_the_preferred_channel(index: ProfileIndex, cfg: Config) -> None:
    """R-1.9: admin alerts go to the owner profile's preferred_channel."""
    assert index.admin_channel(cfg.owner.profile) == "whatsapp"


def test_ids_do_not_cross_channels(index: ProfileIndex) -> None:
    """R-1.9: the same digits on the other channel must not match a profile."""
    assert index.resolve("whatsapp", "100000001") is None
    assert index.resolve("telegram", "51999999999") is None
    assert index.resolve("telegram", "100000001") == "yo"
