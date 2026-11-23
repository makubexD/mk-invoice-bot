"""Tests for wiring the console channel into the runtime (D165)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from invoicebot.app import _console_transports, _media_sources
from invoicebot.channels.console import CONSOLE, ConsoleMediaSource
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "example.yaml"


def _config_with_console(tmp_path: Path) -> Config:
    """Load the example config with a console channel appended (D165)."""
    document: dict[str, Any] = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["channels"].append({"kind": "console", "profile": "yo"})
    path = tmp_path / "example.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return load_config(path)


def test_without_console_channel_wires_no_console_source_or_transport() -> None:
    """D165: a config without a console channel adds no console media source or transport."""
    cfg = load_config(EXAMPLE)

    assert _media_sources([], cfg, Path()) == {}
    assert _console_transports(cfg) == {}


def test_console_channel_wires_one_source_and_one_transport(tmp_path: Path) -> None:
    """D165: a console channel adds a console media source and outbox transport."""
    cfg = _config_with_console(tmp_path)

    sources = _media_sources([], cfg, tmp_path)
    assert set(sources) == {CONSOLE}
    assert isinstance(sources[CONSOLE], ConsoleMediaSource)
    transports = _console_transports(cfg)
    assert set(transports) == {CONSOLE}
    assert callable(transports[CONSOLE])
