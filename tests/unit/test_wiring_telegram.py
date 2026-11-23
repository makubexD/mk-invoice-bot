"""Unit tests for the Telegram channel builder (R-1.7, R-9.10, D174, D183)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import APIRouter

from invoicebot.api.intake_route import IntakeDeps
from invoicebot.channels.telegram.media import TelegramMediaSource
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, TelegramChannel
from invoicebot.pipeline.intake_config import intake_settings
from invoicebot.secrets import SecretStore
from invoicebot.wiring.channels import CHANNEL_SECRETS, SENDER_HASH_KEY
from invoicebot.wiring.telegram import TELEGRAM_SECRETS, telegram_wiring

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "example.yaml"


def _config() -> Config:
    """Load the example config, which carries a disabled Telegram channel."""
    return load_config(EXAMPLE)


def _enabled_config() -> Config:
    """Return the example config with its Telegram channel switched on (D174)."""
    cfg = _config()
    channels = [
        channel.model_copy(update={"enabled": True}) if isinstance(channel, TelegramChannel) else channel
        for channel in cfg.channels
    ]
    return cfg.model_copy(update={"channels": channels})


def _secrets() -> dict[str, str]:
    """Return synthetic values for every secret the Telegram channel declares (R-9.10)."""
    return dict.fromkeys(TELEGRAM_SECRETS | CHANNEL_SECRETS, "synthetic-value")


def _intake_deps(cfg: Config) -> IntakeDeps:
    """Return intake arguments over a null pool: building a channel never uses the pool."""
    return IntakeDeps(pool=cast(Any, None), intake=intake_settings(cfg))


def _no_request(request: httpx.Request) -> httpx.Response:
    """Fail the test if building a channel makes an HTTP request."""
    raise AssertionError(f"unexpected request to {request.url}")


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    """Return a client whose transport fails on any request (R-1.7)."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(_no_request)) as client:
        yield client


async def test_builder_with_disabled_telegram_config_returns_none(
    http: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """D174: a config with its Telegram channel disabled builds no wiring and logs no error.

    Telegram is optional since D174, so its absence is not a fail-closed alert.
    """
    cfg = _config()

    with caplog.at_level(logging.ERROR):
        wiring = telegram_wiring(cfg, SecretStore(_secrets()), http, _intake_deps(cfg))

    assert wiring is None
    assert caplog.messages == []


async def test_builder_with_enabled_config_returns_telegram_wiring(http: httpx.AsyncClient) -> None:
    """R-1.7: complete config and secrets build Telegram's transport, media source and router."""
    cfg = _enabled_config()

    wiring = telegram_wiring(cfg, SecretStore(_secrets()), http, _intake_deps(cfg))

    assert wiring is not None
    assert wiring.name == "telegram"
    assert callable(wiring.transport)
    assert isinstance(wiring.media_source, TelegramMediaSource)
    assert isinstance(wiring.router, APIRouter)
    assert "/webhook/telegram" in {route.path for route in wiring.router.routes}


async def test_builder_without_telegram_bot_token_returns_none(
    http: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """R-9.10, D183: an incomplete Telegram secret view disables the channel and names the view."""
    cfg = _enabled_config()
    values = _secrets()
    del values["telegram_bot_token"]
    store = SecretStore(values)

    with caplog.at_level(logging.ERROR):
        wiring = telegram_wiring(cfg, store, http, _intake_deps(cfg))

    assert wiring is None
    assert "telegram channel disabled: missing ['telegram_bot_token']" in caplog.messages
    assert telegram_wiring.secret_problem(cfg, store) == "secrets:telegram"


async def test_builder_without_sender_hash_key_returns_none(
    http: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """R-9.10: a missing shared channel secret disables the channel and names the channel view."""
    cfg = _enabled_config()
    values = _secrets()
    del values[SENDER_HASH_KEY]
    store = SecretStore(values)

    with caplog.at_level(logging.ERROR):
        wiring = telegram_wiring(cfg, store, http, _intake_deps(cfg))

    assert wiring is None
    assert "telegram channel disabled: missing ['sender_hash_key']" in caplog.messages
    assert telegram_wiring.secret_problem(cfg, store) == "secrets:channels"


def test_configured_is_true_for_an_enabled_telegram_channel() -> None:
    """D183: an enabled Telegram channel makes its builder report it configured."""
    assert telegram_wiring.configured(_enabled_config()) is True


def test_configured_is_false_for_a_disabled_telegram_channel() -> None:
    """D183: the example config's disabled Telegram channel is not configured."""
    assert telegram_wiring.configured(_config()) is False


def test_secret_problem_without_telegram_config_is_none() -> None:
    """R-9.10, D174: an optional Telegram channel that is not configured needs no secrets."""
    assert telegram_wiring.secret_problem(_config(), SecretStore({})) is None


def test_secret_problem_with_every_secret_is_none() -> None:
    """R-9.10: an enabled Telegram channel with both secret views complete raises no problem."""
    assert telegram_wiring.secret_problem(_enabled_config(), SecretStore(_secrets())) is None
