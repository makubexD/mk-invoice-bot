"""The console channel builder: the dev-only rehearsal wiring ``app.py`` built inline (D165, D174).

The console has no router and no channel-specific secret. Its media source reads from the
inbox under the process's data directory, so the builder is created where ``Settings`` is
known rather than as a module-level constant (D174).
"""

from __future__ import annotations

from pathlib import Path

import httpx

from invoicebot.api.intake_route import IntakeDeps
from invoicebot.channels.console import CONSOLE, ConsoleMediaSource, console_channel, console_transport
from invoicebot.config.models import Config
from invoicebot.secrets import SecretStore
from invoicebot.wiring.channels import ChannelWiring


class ConsoleBuilder:
    """Builds the console channel, bound to the process's data directory (D165, D174)."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def __call__(
        self, cfg: Config, store: SecretStore, http: httpx.AsyncClient, intake_deps: IntakeDeps
    ) -> ChannelWiring | None:
        """Return the console wiring, or ``None`` when the config does not list it (D165)."""
        # The console declares no secret and has no router, so the other builder inputs go unused.
        del store, http, intake_deps
        if console_channel(cfg) is None:
            return None
        return ChannelWiring(
            name=CONSOLE,
            transport=console_transport(),
            media_source=ConsoleMediaSource(self._data_dir),
            router=None,
        )

    def secret_problem(self, cfg: Config, store: SecretStore) -> str | None:
        """Return ``None``: the console needs only the shared channel secret (R-9.10)."""
        del cfg, store
        return None
