"""Integration tests for the channel-composition lifespan (D174)."""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pyrage
import pytest
import yaml

import invoicebot.app
from invoicebot.app import _check_reload_candidate, create_app, lifespan
from invoicebot.runtime_config import ConfigRejected
from invoicebot.secrets import SECRET_ENV_SUFFIXES
from invoicebot.settings import Settings

pytestmark = pytest.mark.asyncio(loop_scope="session")

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "config" / "example.yaml"
PARSING_RULES = REPO_ROOT / "config" / "parsing_rules.yaml"
VERIFY_VALUE = "synthetic-verify-value"
#: The synthetic secrets the example config needs to start its WhatsApp channel (R-9.10).
SECRET_VALUES = {
    "whatsapp_token": "synthetic-token-value",
    "whatsapp_app_secret": "synthetic-app-value",
    "whatsapp_verify_token": VERIFY_VALUE,
    "sender_hash_key": "synthetic-hash-value",
    "github_app_private_key": "synthetic-github-app-key",
    "google_client_secret": "synthetic-google-client-value",
}


async def _no_ping() -> bool:
    """A database ping that never connects; the lifespan replaces it with the real one."""
    return False


def _write_config(directory: Path, document: dict[str, Any]) -> None:
    """Write ``document`` as the runtime config file."""
    (directory / "config.yaml").write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _write_secrets(directory: Path, values: dict[str, str]) -> None:
    """Encrypt ``values`` into the runtime secrets file beside its identity (R-9.9)."""
    identity = pyrage.x25519.Identity.generate()
    (directory / "identity.key").write_text(str(identity) + "\n", encoding="utf-8")
    plaintext = yaml.safe_dump(values).encode("utf-8")
    (directory / "secrets.age").write_bytes(pyrage.encrypt(plaintext, [identity.to_public()]))


def _no_channel_document() -> dict[str, Any]:
    """Return the example config with no chat channel and no preferred channel (D174)."""
    document: dict[str, Any] = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["channels"] = []
    for profile in document["profiles"].values():
        profile.pop("preferred_channel", None)
    return document


@pytest.fixture
def runtime_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, test_database_url: str) -> Iterator[Path]:
    """Point Settings at ``tmp_path`` and clear secret-looking variables (R-9.9)."""
    for name in list(os.environ):
        if name.upper().endswith(SECRET_ENV_SUFFIXES):
            monkeypatch.delenv(name)
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("SECRETS_PATH", str(tmp_path / "secrets.age"))
    monkeypatch.setenv("AGE_IDENTITY_PATH", str(tmp_path / "identity.key"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PARSING_RULES_PATH", str(PARSING_RULES))
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield tmp_path
    # The runtime installs the JSON log handler; give the root logger back.
    root.handlers[:] = handlers
    root.setLevel(level)


@contextlib.asynccontextmanager
async def _no_background(factories: dict[str, Any]) -> AsyncIterator[None]:
    """Stand in for ``_background`` so a test start never runs a daily task or worker."""
    yield


@contextlib.asynccontextmanager
async def _app_client() -> AsyncIterator[httpx.AsyncClient]:
    """Run the real lifespan and yield a client that reaches the app in-process."""
    app = create_app(_no_ping, lifespan=lifespan)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def test_whatsapp_config_mounts_the_webhook(runtime_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D174: a config with WhatsApp mounts /webhook/whatsapp and answers Meta's handshake."""
    _write_config(runtime_env, yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")))
    _write_secrets(runtime_env, SECRET_VALUES)
    monkeypatch.setattr(invoicebot.app, "_background", _no_background)
    params = {"hub.mode": "subscribe", "hub.verify_token": VERIFY_VALUE, "hub.challenge": "c-123"}

    async with _app_client() as client:
        response = await client.get("/webhook/whatsapp", params=params)

    assert response.status_code == 200
    assert response.text == "c-123"


async def test_no_chat_channel_serves_health_only(
    runtime_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D174: with no chat channel the app logs, mounts no webhook and still serves /health."""
    _write_config(runtime_env, _no_channel_document())
    _write_secrets(runtime_env, SECRET_VALUES)
    # configure_logging replaces the root handlers, which would hide the records from caplog.
    monkeypatch.setattr(invoicebot.app, "configure_logging", lambda *_args, **_kwargs: None)
    caplog.set_level(logging.ERROR, logger="invoicebot.app")

    async with _app_client() as client:
        health = await client.get("/health")
        webhook = await client.get("/webhook/whatsapp")

    assert "no chat channel enabled" in caplog.text
    assert webhook.status_code == 404
    assert health.status_code == 200


async def test_reload_refuses_a_config_with_no_chat_channel(runtime_env: Path) -> None:
    """D174: reloading a config that enables no chat channel is refused as channels:none."""
    _write_config(runtime_env, _no_channel_document())
    _write_secrets(runtime_env, SECRET_VALUES)

    with pytest.raises(ConfigRejected) as raised:
        _check_reload_candidate(Settings())

    assert raised.value.reason == "channels:none"


async def test_failed_startup_check_mounts_no_webhook(
    runtime_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D174: a chat channel's router is mounted only after every fail-closed check passed (D124)."""
    _write_config(runtime_env, yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")))
    _write_secrets(runtime_env, SECRET_VALUES)
    missing = SimpleNamespace(enabled=False, missing=frozenset({"sunat_client_secret"}))
    monkeypatch.setattr(invoicebot.app, "_validation_secrets", lambda *_args: missing)
    params = {"hub.mode": "subscribe", "hub.verify_token": VERIFY_VALUE, "hub.challenge": "c-123"}

    async with _app_client() as client:
        response = await client.get("/webhook/whatsapp", params=params)

    assert response.status_code == 404
