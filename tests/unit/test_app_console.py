"""Tests for wiring the console channel into the runtime (D165)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import yaml

from invoicebot.app import _console_transports, _media_sources
from invoicebot.channels.console import CONSOLE, ConsoleMediaSource
from invoicebot.channels.whatsapp.client import WhatsAppClient
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "example.yaml"
PHONE_NUMBER_ID = "123456789"
GRAPH_VERSION = "v23.0"
BEARER = "synthetic-bearer-value"

Handler = Callable[[httpx.Request], httpx.Response]


def _unused(request: httpx.Request) -> httpx.Response:
    """Fail when a test reaches the network instead of using the wiring."""
    raise AssertionError(f"unexpected request: {request.url}")


@contextlib.asynccontextmanager
async def _client() -> AsyncIterator[WhatsAppClient]:
    """Yield a WhatsApp client whose requests never leave the test (R-1.4)."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(_unused)) as http:
        yield WhatsAppClient(
            http,
            phone_number_id=PHONE_NUMBER_ID,
            graph_api_version=GRAPH_VERSION,
            token=BEARER,
        )


def _config_with_console(tmp_path: Path) -> Config:
    """Load the example config with a console channel appended (D165)."""
    document: dict[str, Any] = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["channels"].append({"kind": "console", "profile": "yo"})
    path = tmp_path / "example.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return load_config(path)


async def test_without_console_channel_wires_no_console_source_or_transport() -> None:
    """D165: a config without a console channel keeps only the WhatsApp media source."""
    cfg = load_config(EXAMPLE)
    async with _client() as client:
        sources = _media_sources(cfg, client, Path())

    assert _console_transports(cfg) == {}
    assert set(sources) == {"whatsapp"}


async def test_console_channel_wires_one_source_and_one_transport(tmp_path: Path) -> None:
    """D165: a console channel adds a console media source and outbox transport."""
    cfg = _config_with_console(tmp_path)
    async with _client() as client:
        sources = _media_sources(cfg, client, tmp_path)

    assert set(sources) == {"whatsapp", CONSOLE}
    assert isinstance(sources[CONSOLE], ConsoleMediaSource)
    transports = _console_transports(cfg)
    assert set(transports) == {CONSOLE}
    assert callable(transports[CONSOLE])
