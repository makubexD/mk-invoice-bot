"""Tests for ``invoicebot telegram`` (D186, R-9.16)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pyrage
import pytest
import yaml

from invoicebot.cli import main
from invoicebot.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "config" / "example.yaml"
TOKEN = "synthetic-bot-token"  # noqa: S105  # a synthetic test value
#: 43 characters, the shape ``secrets.token_urlsafe(32)`` gives (D184).
WEBHOOK_SECRET = "synthetic_webhook_secret_0123456789abcdefgh"  # noqa: S105  # a synthetic test value
WEBHOOK_URL = "https://h.example.ts.net/webhook/telegram"
ERROR_STATUS = 401
WEBHOOK_ACTIVE_STATUS = 409


class BotApi:
    """Answer one Bot API method with a fixed result or an error (D186)."""

    def __init__(self, method: str, *, status: int = 200, result: object = True) -> None:
        self.method = method
        self.status = status
        self.result = result
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not request.url.path.endswith("/" + self.method):
            raise AssertionError(f"unexpected request: {request.url.path}")
        if self.status != 200:
            return httpx.Response(
                self.status,
                json={"ok": False, "error_code": self.status, "description": "synthetic error"},
            )
        return httpx.Response(200, json={"ok": True, "result": self.result})


def _config_path(tmp_path: Path, *, telegram_enabled: bool) -> Path:
    """Write the example config with its Telegram channel on or off (D186)."""
    document: dict[str, Any] = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    telegram = next(channel for channel in document["channels"] if channel["kind"] == "telegram")
    telegram["enabled"] = telegram_enabled
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _settings(tmp_path: Path, *, telegram_enabled: bool = True) -> Settings:
    """Build settings on a config whose Telegram channel is enabled, with a synthetic age store."""
    identity = pyrage.x25519.Identity.generate()
    identity_path = tmp_path / "identity.txt"
    identity_path.write_text(str(identity), encoding="utf-8")
    values = {"telegram_bot_token": TOKEN, "telegram_webhook_secret": WEBHOOK_SECRET}
    plaintext = yaml.safe_dump(values).encode("utf-8")
    secrets_path = tmp_path / "secrets.age"
    secrets_path.write_bytes(pyrage.encrypt(plaintext, [identity.to_public()]))
    return Settings(
        config_path=_config_path(tmp_path, telegram_enabled=telegram_enabled),
        data_dir=tmp_path / "data",
        secrets_path=secrets_path,
        age_identity_path=identity_path,
    )


def _run(settings: Settings, api: BotApi, *argv: str) -> int:
    """Run ``telegram`` with ``argv`` against the fake Bot API."""
    return main(["telegram", *argv], settings=settings, http_transport=httpx.MockTransport(api))


def _private_update(user_id: int, *, username: str | None = None) -> dict[str, Any]:
    """Return one private-message update from ``user_id`` (D175)."""
    sender: dict[str, Any] = {"id": user_id}
    if username is not None:
        sender["username"] = username
    return {
        "update_id": user_id,
        "message": {"chat": {"id": user_id, "type": "private"}, "from": sender, "text": "synthetic"},
    }


def test_ids_prints_one_line_per_private_sender(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D186, R-9.16: ids prints each private sender's id and username, never the message text."""
    api = BotApi(
        "getUpdates",
        result=[_private_update(100000001, username="a"), _private_update(100000002)],
    )

    assert _run(_settings(tmp_path), api, "ids") == 0

    captured = capsys.readouterr()
    assert captured.out == "100000001 @a\n100000002 -\n"
    assert "synthetic" not in captured.out


def test_ids_with_a_webhook_active_exits_one_with_the_delete_hint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D186: a 409 from getUpdates means a webhook is set; the hint goes to stderr."""
    api = BotApi("getUpdates", status=WEBHOOK_ACTIVE_STATUS)

    assert _run(_settings(tmp_path), api, "ids") == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invoicebot telegram webhook --delete" in captured.err


def test_webhook_url_registers_the_stored_secret(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D186, R-9.9: webhook --url sends the URL and the secret read from the age store."""
    api = BotApi("setWebhook", result=True)

    assert _run(_settings(tmp_path), api, "webhook", "--url", WEBHOOK_URL) == 0

    assert capsys.readouterr().out == "webhook set\n"
    assert json.loads(api.requests[0].content) == {
        "url": WEBHOOK_URL,
        "secret_token": WEBHOOK_SECRET,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": True,
    }


def test_webhook_info_prints_four_lines(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D186: webhook --info prints the URL, pending count and last error, one per line."""
    api = BotApi(
        "getWebhookInfo",
        result={
            "url": WEBHOOK_URL,
            "pending_update_count": 0,
            "last_error_date": 1700000000,
            "last_error_message": "synthetic error",
        },
    )

    assert _run(_settings(tmp_path), api, "webhook", "--info") == 0

    assert capsys.readouterr().out == (
        f"url: {WEBHOOK_URL}\npending: 0\nlast_error_date: 1700000000\nlast_error_message: synthetic error\n"
    )


def test_telegram_disabled_exits_two_without_a_request(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D186: a config without an enabled Telegram channel exits 2 before any request."""
    api = BotApi("getUpdates", result=[])

    with pytest.raises(SystemExit) as exit_info:
        _run(_settings(tmp_path, telegram_enabled=False), api, "ids")

    assert exit_info.value.code == 2
    assert api.requests == []
    assert "not enabled" in capsys.readouterr().err


def test_a_rejected_token_exits_one_without_the_token_or_url(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R-9.16: a failed Bot API call exits 1 and prints neither the token nor the request URL."""
    api = BotApi("getUpdates", status=ERROR_STATUS)

    assert _run(_settings(tmp_path), api, "ids") == 1

    captured = capsys.readouterr()
    assert "Telegram API error 401" in captured.err
    assert captured.out == ""
    assert TOKEN not in captured.err
    assert TOKEN not in captured.out
    assert "api.telegram.org" not in captured.err


def test_url_and_info_together_exit_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D186: --url, --info and --delete are mutually exclusive; two of them exit 2."""
    api = BotApi("setWebhook", result=True)

    with pytest.raises(SystemExit) as exit_info:
        _run(_settings(tmp_path), api, "webhook", "--url", WEBHOOK_URL, "--info")

    assert exit_info.value.code == 2
    assert api.requests == []


def test_commands_sets_default_and_admin_menus_without_printing_user_ids(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D187, R-9.16: commands sets the default menu and each admin's, naming profiles not ids."""
    api = BotApi("setMyCommands", result=True)

    assert _run(_settings(tmp_path), api, "commands") == 0

    assert len(api.requests) == 2
    default_menu, admin_menu = (json.loads(request.content) for request in api.requests)
    assert default_menu["scope"] == {"type": "default"}
    assert "gaps" not in [command["command"] for command in default_menu["commands"]]
    assert admin_menu["scope"] == {"type": "chat", "chat_id": 100000001}
    assert "gaps" in [command["command"] for command in admin_menu["commands"]]
    captured = capsys.readouterr()
    assert "menu set: default" in captured.out
    assert "menu set: yo" in captured.out
    assert "100000001" not in captured.out + captured.err
