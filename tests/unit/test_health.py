"""Unit tests for the /health endpoint (R-9.14)."""

import asyncio
import logging
import os
import signal
from collections.abc import Iterator
from pathlib import Path

import pyrage
import pytest
import yaml
from fastapi.testclient import TestClient

import invoicebot.app
from invoicebot.app import create_app, lifespan
from invoicebot.secrets import SECRET_ENV_SUFFIXES

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "config" / "example.yaml"
VERIFY_VALUE = "synthetic-verify-value"
#: A port nothing listens on, so the pool never connects and no test needs Postgres.
UNREACHABLE_DSN = "postgresql://invoicebot@127.0.0.1:1/invoicebot?connect_timeout=1"


async def _no_ping() -> bool:
    return False


@pytest.fixture
def runtime_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Point the settings at ``tmp_path`` and clear secret-looking variables (R-9.9)."""
    for name in list(os.environ):
        if name.upper().endswith(SECRET_ENV_SUFFIXES):
            monkeypatch.delenv(name)
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DSN)
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("SECRETS_PATH", str(tmp_path / "secrets.age"))
    monkeypatch.setenv("AGE_IDENTITY_PATH", str(tmp_path / "identity.key"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield tmp_path
    # The runtime installs the JSON log handler; give the root logger back.
    root.handlers[:] = handlers
    root.setLevel(level)


def _write_runtime_files(directory: Path) -> None:
    """Copy the example config and encrypt synthetic WhatsApp secrets beside it."""
    (directory / "config.yaml").write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    identity = pyrage.x25519.Identity.generate()
    (directory / "identity.key").write_text(str(identity) + "\n", encoding="utf-8")
    values = {
        "whatsapp_token": "synthetic-token-value",
        "whatsapp_app_secret": "synthetic-app-value",
        "whatsapp_verify_token": VERIFY_VALUE,
        "sender_hash_key": "synthetic-hash-value",
    }
    plaintext = yaml.safe_dump(values).encode("utf-8")
    (directory / "secrets.age").write_bytes(pyrage.encrypt(plaintext, [identity.to_public()]))


def test_lifespan_without_config_serves_health_only(
    runtime_env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """R-9.14: with no config or secrets file the app starts with /health and no webhook."""
    caplog.set_level(logging.WARNING, logger="invoicebot.app")

    with TestClient(create_app(_no_ping, lifespan=lifespan)) as client:
        assert client.get("/health").status_code == 503
        assert client.get("/webhook/whatsapp").status_code == 404

    assert "serving /health only" in caplog.text


def test_lifespan_with_config_and_secrets_answers_the_handshake(runtime_env: Path) -> None:
    """R-1.3: with the example config and a secrets file the webhook answers Meta's handshake."""
    _write_runtime_files(runtime_env)
    params = {"hub.mode": "subscribe", "hub.verify_token": VERIFY_VALUE, "hub.challenge": "c-123"}

    with TestClient(create_app(_no_ping, lifespan=lifespan)) as client:
        response = client.get("/webhook/whatsapp", params=params)

    assert response.status_code == 200
    assert response.text == "c-123"


def test_lifespan_with_missing_channel_secret_does_not_mount_the_webhook(runtime_env: Path) -> None:
    """R-9.10: a missing WhatsApp secret leaves the webhook unmounted (fail closed)."""
    _write_runtime_files(runtime_env)
    identity = pyrage.x25519.Identity.generate()
    (runtime_env / "identity.key").write_text(str(identity) + "\n", encoding="utf-8")
    plaintext = yaml.safe_dump({"whatsapp_token": "synthetic-token-value"}).encode("utf-8")
    (runtime_env / "secrets.age").write_bytes(pyrage.encrypt(plaintext, [identity.to_public()]))

    with TestClient(create_app(_no_ping, lifespan=lifespan)) as client:
        assert client.get("/webhook/whatsapp").status_code == 404


def test_health_ok_when_db_ping_true() -> None:
    """R-9.14: /health returns 200 with ok statuses when the DB ping succeeds."""

    async def ping() -> bool:
        return True

    client = TestClient(create_app(ping))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}


def test_health_degraded_when_db_ping_false() -> None:
    """R-9.14: /health returns 503 degraded when the DB ping returns false."""

    async def ping() -> bool:
        return False

    client = TestClient(create_app(ping))
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "db": "down"}


def test_health_degraded_when_db_ping_raises() -> None:
    """R-9.14: /health returns 503 degraded when the DB ping raises."""

    async def ping() -> bool:
        raise RuntimeError("database unreachable")

    client = TestClient(create_app(ping))
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "db": "down"}


async def test_sighup_logs_reload_request_and_keeps_running(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R-9.6: SIGHUP requests a config reload without terminating the process."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    caplog.set_level(logging.INFO, logger="invoicebot.app")

    async with invoicebot.app.app.router.lifespan_context(invoicebot.app.app):
        os.kill(os.getpid(), signal.SIGHUP)
        await asyncio.sleep(0.1)

    os.kill(os.getpid(), 0)  # the process still exists after SIGHUP
    assert "config reload requested (SIGHUP)" in caplog.text
