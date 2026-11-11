"""Unit tests for the /health endpoint (R-9.14)."""

import asyncio
import logging
import os
import signal

import pytest
from fastapi.testclient import TestClient

import invoicebot.app
from invoicebot.app import create_app


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
