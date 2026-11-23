"""Unit tests for the WhatsApp channel builder (R-1.7, R-9.10, D174)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import APIRouter

from invoicebot.api.intake_route import IntakeDeps
from invoicebot.channels.whatsapp.media import WhatsAppMediaSource
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config
from invoicebot.pipeline.intake_config import intake_settings
from invoicebot.secrets import SecretStore
from invoicebot.wiring.channels import CHANNEL_SECRETS, SENDER_HASH_KEY
from invoicebot.wiring.whatsapp import WHATSAPP_SECRETS, whatsapp_wiring

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "example.yaml"


def _config() -> Config:
    """Load the example config, which carries a WhatsApp channel."""
    return load_config(EXAMPLE)


def _secrets() -> dict[str, str]:
    """Return synthetic values for every secret the WhatsApp channel declares (R-9.10)."""
    return dict.fromkeys(WHATSAPP_SECRETS | CHANNEL_SECRETS, "synthetic-value")


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


async def test_builder_without_whatsapp_config_returns_none(
    http: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """R-1.7, D174: a config with no WhatsApp channel builds no wiring and logs no error.

    WhatsApp is optional since D174, so its absence is not a fail-closed alert.
    """
    cfg = _config().model_copy(update={"channels": []})

    with caplog.at_level(logging.ERROR):
        wiring = whatsapp_wiring(cfg, SecretStore(_secrets()), http, _intake_deps(cfg))

    assert wiring is None
    assert caplog.messages == []


async def test_builder_without_whatsapp_secrets_returns_none(
    http: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """R-1.7: an incomplete WhatsApp secret view disables the channel and names the view."""
    cfg = _config()
    values = _secrets()
    del values["whatsapp_token"]
    store = SecretStore(values)

    with caplog.at_level(logging.ERROR):
        wiring = whatsapp_wiring(cfg, store, http, _intake_deps(cfg))

    assert wiring is None
    assert "whatsapp channel disabled: missing ['whatsapp_token']" in caplog.messages
    assert whatsapp_wiring.secret_problem(cfg, store) == "secrets:whatsapp"


async def test_builder_without_sender_hash_key_returns_none(
    http: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """R-1.7: a missing shared channel secret disables the channel and names the channel view."""
    cfg = _config()
    values = _secrets()
    del values[SENDER_HASH_KEY]
    store = SecretStore(values)

    with caplog.at_level(logging.ERROR):
        wiring = whatsapp_wiring(cfg, store, http, _intake_deps(cfg))

    assert wiring is None
    assert "whatsapp channel disabled: missing ['sender_hash_key']" in caplog.messages
    assert whatsapp_wiring.secret_problem(cfg, store) == "secrets:channels"


async def test_builder_with_full_config_returns_whatsapp_wiring(http: httpx.AsyncClient) -> None:
    """R-1.7: complete config and secrets build WhatsApp's transport, media source and router."""
    cfg = _config()

    wiring = whatsapp_wiring(cfg, SecretStore(_secrets()), http, _intake_deps(cfg))

    assert wiring is not None
    assert wiring.name == "whatsapp"
    assert callable(wiring.transport)
    assert isinstance(wiring.media_source, WhatsAppMediaSource)
    assert isinstance(wiring.router, APIRouter)


def test_secret_problem_without_whatsapp_config_is_none() -> None:
    """R-1.7: an optional WhatsApp channel that is not configured needs no secrets (D174)."""
    cfg = _config().model_copy(update={"channels": []})

    assert whatsapp_wiring.secret_problem(cfg, SecretStore({})) is None


def test_secret_problem_with_every_secret_is_none() -> None:
    """R-1.7: a configured WhatsApp channel with both secret views complete raises no problem."""
    assert whatsapp_wiring.secret_problem(_config(), SecretStore(_secrets())) is None
