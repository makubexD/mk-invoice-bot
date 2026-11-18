"""Unit tests for the SIGHUP reload decision (R-9.6, D155)."""

from __future__ import annotations

from typing import cast

from invoicebot.config.models import Config, StoreDirectory
from invoicebot.reload import reload_config
from invoicebot.runtime_config import ConfigRejected, RuntimeConfig

CANDIDATE = RuntimeConfig(
    cfg=cast(Config, object()),
    directory=cast(StoreDirectory, object()),
    rules=(),
)


async def test_reload_config_rejected_candidate_alerts_and_keeps_running() -> None:
    """R-9.6: a rejected candidate reports its reason and never calls restart."""
    rejected: list[str] = []
    restarts: list[bool] = []

    def load() -> RuntimeConfig:
        raise ConfigRejected("yaml")

    async def on_rejected(reason: str) -> None:
        rejected.append(reason)

    def restart() -> None:
        restarts.append(True)

    accepted = await reload_config(load, on_rejected, restart)

    assert accepted is False
    assert rejected == ["yaml"]
    assert restarts == []


async def test_reload_config_accepted_candidate_restarts_once() -> None:
    """R-9.6: a candidate that loads restarts into it without an alert."""
    rejected: list[str] = []
    restarts: list[bool] = []

    def load() -> RuntimeConfig:
        return CANDIDATE

    async def on_rejected(reason: str) -> None:
        rejected.append(reason)

    def restart() -> None:
        restarts.append(True)

    accepted = await reload_config(load, on_rejected, restart)

    assert accepted is True
    assert restarts == [True]
    assert rejected == []
