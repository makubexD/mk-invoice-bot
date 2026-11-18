"""Tests for the process settings (D147)."""

from __future__ import annotations

import pytest

from invoicebot.settings import Settings


def test_settings_helper_sandbox_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """D147: with no env vars, the sandbox is off and the temp cap is 256 MB."""
    monkeypatch.delenv("HELPER_SANDBOX", raising=False)
    monkeypatch.delenv("HELPER_TMP_MB", raising=False)

    settings = Settings()

    assert settings.helper_sandbox is False
    assert settings.helper_tmp_mb == 256


def test_settings_helper_sandbox_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """D147: HELPER_SANDBOX and HELPER_TMP_MB override the defaults."""
    monkeypatch.setenv("HELPER_SANDBOX", "1")
    monkeypatch.setenv("HELPER_TMP_MB", "64")

    settings = Settings()

    assert settings.helper_sandbox is True
    assert settings.helper_tmp_mb == 64
