"""Unit tests for the Telegram channel builder (R-1.7, R-9.10, D174, D183)."""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import APIRouter

from invoicebot.api.intake_route import IntakeDeps
from invoicebot.channels.telegram.client import TELEGRAM_SECRETS, webhook_secret_is_strong
from invoicebot.channels.telegram.media import TelegramMediaSource
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, TelegramChannel
from invoicebot.pipeline.intake_config import intake_settings
from invoicebot.secrets import SecretStore
from invoicebot.wiring.channels import CHANNEL_SECRETS, SENDER_HASH_KEY
from invoicebot.wiring.telegram import TelegramBuilder

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "example.yaml"
#: The builder under test; no test here sends a document, so its data directory is never read (D194).
telegram_wiring = TelegramBuilder(Path("/nonexistent-data-dir"))
#: A webhook secret of the strength D184 requires, 43 characters like token_urlsafe(32).
WEBHOOK_SECRET = "synthetic_webhook_secret_0123456789abcdefgh"  # noqa: S105  # a synthetic test value


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
    return {
        **dict.fromkeys(TELEGRAM_SECRETS | CHANNEL_SECRETS, "synthetic-value"),
        "telegram_webhook_secret": WEBHOOK_SECRET,
    }


def _store_with_webhook_secret(secret: str) -> SecretStore:
    """Return an enabled Telegram and channel store whose webhook secret is ``secret`` (D184)."""
    values = _secrets()
    values["telegram_webhook_secret"] = secret
    return SecretStore(values)


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


def test_secret_problem_accepts_a_token_urlsafe_webhook_secret() -> None:
    """D184, R-9.6: a 43-character token_urlsafe secret is strong enough."""
    store = _store_with_webhook_secret(secrets.token_urlsafe(32))

    assert telegram_wiring.secret_problem(_enabled_config(), store) is None


def test_secret_problem_accepts_a_32_character_webhook_secret() -> None:
    """D184, R-9.6: the shortest allowed secret, 32 charset characters, is accepted."""
    assert telegram_wiring.secret_problem(_enabled_config(), _store_with_webhook_secret("a" * 32)) is None


@pytest.mark.parametrize(
    "secret",
    ["a" * 31, "a" * 39 + " ", "a" * 257],
    ids=["too-short", "off-charset", "too-long"],
)
def test_secret_problem_rejects_a_weak_webhook_secret(secret: str) -> None:
    """D184, R-9.6: a secret outside the charset or length range is refused as weak."""
    assert (
        telegram_wiring.secret_problem(_enabled_config(), _store_with_webhook_secret(secret))
        == "secrets:telegram_webhook_weak"
    )


def test_weak_webhook_secret_problem_never_contains_the_value() -> None:
    """D184: the weak-secret problem code never echoes the secret."""
    secret = "short secret"  # noqa: S105  # a synthetic weak test value
    problem = telegram_wiring.secret_problem(_enabled_config(), _store_with_webhook_secret(secret))

    assert problem == "secrets:telegram_webhook_weak"
    assert secret not in problem


def test_secret_problem_ignores_webhook_strength_when_telegram_is_disabled() -> None:
    """D184, D174: a disabled Telegram channel needs no strong webhook secret."""
    assert telegram_wiring.secret_problem(_config(), _store_with_webhook_secret("short")) is None


async def test_builder_with_weak_webhook_secret_returns_none(
    http: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """D184: a configured channel with a weak webhook secret fails startup closed without echoing it."""
    cfg = _enabled_config()
    secret = "short"  # noqa: S105  # a synthetic weak test value
    store = _store_with_webhook_secret(secret)

    with caplog.at_level(logging.ERROR):
        wiring = telegram_wiring(cfg, store, http, _intake_deps(cfg))

    assert wiring is None
    assert "telegram channel disabled: secrets:telegram_webhook_weak" in caplog.messages
    assert secret not in " ".join(caplog.messages)


def test_webhook_secret_is_strong_accepts_a_32_character_secret() -> None:
    """D184: the shortest allowed secret, 32 charset characters, is strong."""
    assert webhook_secret_is_strong("a" * 32) is True


@pytest.mark.parametrize(
    "secret",
    ["a" * 31, "a" * 31 + " "],
    ids=["too-short", "off-charset"],
)
def test_webhook_secret_is_strong_rejects_a_weak_secret(secret: str) -> None:
    """D184: a secret shorter than 32 characters or outside the charset is weak."""
    assert webhook_secret_is_strong(secret) is False
