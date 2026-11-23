"""Tests for wiring the console channel into the runtime (D165, D174)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import yaml

from invoicebot.api.intake_route import IntakeDeps
from invoicebot.app import _channel_builders
from invoicebot.channels.console import CONSOLE, ConsoleMediaSource
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config
from invoicebot.pipeline.intake_config import intake_settings
from invoicebot.secrets import SecretStore
from invoicebot.wiring.channels import ChannelWiring

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "example.yaml"


def _console_only_config(tmp_path: Path) -> Config:
    """Load the example config as a dev config whose only channel is the console (D174)."""
    document: dict[str, Any] = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["channels"] = [{"kind": "console", "profile": "yo"}]
    for profile in document["profiles"].values():
        profile.pop("preferred_channel", None)
    path = tmp_path / "example.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return load_config(path)


def _wirings(cfg: Config, data_dir: Path, http: httpx.AsyncClient) -> list[ChannelWiring]:
    """Build the channel wirings the runtime builds at startup (D174)."""
    deps = IntakeDeps(pool=cast(Any, None), intake=intake_settings(cfg))
    store = SecretStore({})
    return [w for b in _channel_builders(data_dir) if (w := b(cfg, store, http, deps)) is not None]


def _no_request(request: httpx.Request) -> httpx.Response:
    """Fail the test if building a channel makes an HTTP request."""
    raise AssertionError(f"unexpected request to {request.url}")


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    """Return a client whose transport fails on any request (R-1.7)."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(_no_request)) as client:
        yield client


async def test_console_only_development_config_starts_workers_and_outbox(
    tmp_path: Path, http: httpx.AsyncClient
) -> None:
    """D174: a dev config whose only channel is the console wires a console transport and media source.

    A non-empty wiring list is what starts the workers and the outbox: without it the
    runtime fails closed. The console adds no router.
    """
    cfg = _console_only_config(tmp_path)

    wirings = _wirings(cfg, tmp_path / "data", http)

    assert [wiring.name for wiring in wirings] == [CONSOLE]
    assert callable(wirings[0].transport)
    assert isinstance(wirings[0].media_source, ConsoleMediaSource)
    assert wirings[0].router is None


async def test_config_without_console_channel_wires_no_console_wiring(
    tmp_path: Path, http: httpx.AsyncClient
) -> None:
    """D165: a config with only WhatsApp builds no console wiring."""
    cfg = load_config(EXAMPLE)

    wirings = _wirings(cfg, tmp_path / "data", http)

    assert CONSOLE not in [wiring.name for wiring in wirings]
