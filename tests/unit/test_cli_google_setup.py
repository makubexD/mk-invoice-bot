"""Tests for ``invoicebot google-setup`` (R-6.2, R-6.5, D129)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pyrage
import pytest
import yaml

from invoicebot.auth.google_oauth import TOKEN_URL, save_refresh_token
from invoicebot.auth.providers import google_token_path
from invoicebot.cli import main
from invoicebot.settings import Settings
from invoicebot.sinks.google_api import GoogleError
from invoicebot.sinks.google_sheets import RAW, GoogleSheets

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "config" / "example.yaml"
ACCOUNT = "owner"
CLIENT_SECRET_KEY = "google_client_secret"  # noqa: S105  # the store key's name, not a value
SYNTHETIC_CLIENT_SECRET = "synthetic-client-secret"  # noqa: S105  # a synthetic test value
SYNTHETIC_REFRESH_TOKEN = "synthetic-refresh"  # noqa: S105  # a synthetic test value
TOKEN_RESPONSE = {"access_token": "synthetic-access", "expires_in": 3600}
FOLDER_ID = "folder-id-123456"
SPREADSHEET_ID = "spreadsheet-id-123456"
TAB = "Compras"
BEARER = "synthetic-bearer"
TIMEOUT_S = 5.0
MAX_ROWS = 1000
#: The example config's sheets sink columns, in its own order (D130).
COLUMNS = (
    "Fecha",
    "RUC",
    "Razón social",
    "Tipo",
    "Serie-Número",
    "Base",
    "IGV",
    "Total",
    "Estado SUNAT",
    "Foto",
    "PDF",
    "XML",
)


class FakeTokens:
    """A TokenSource that always hands out the same synthetic token."""

    async def token(self) -> str:
        return BEARER


class GoogleTransport:
    """Answer the token, Drive and Sheets calls of one ``google-setup`` run."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.header_writes: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url == httpx.URL(TOKEN_URL):
            return httpx.Response(200, json=TOKEN_RESPONSE)
        if path == "/drive/v3/files" and request.method == "GET":
            return httpx.Response(200, json={"files": []})
        if path == "/drive/v3/files" and request.method == "POST":
            return httpx.Response(200, json={"id": FOLDER_ID})
        if path == "/v4/spreadsheets" and request.method == "POST":
            return httpx.Response(200, json={"spreadsheetId": SPREADSHEET_ID})
        if request.method == "PUT":
            self.header_writes.append(request)
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")


def _settings(tmp_path: Path) -> Settings:
    """Build settings on the example config, with a saved refresh token and synthetic age files."""
    identity = pyrage.x25519.Identity.generate()
    identity_path = tmp_path / "identity.txt"
    identity_path.write_text(str(identity), encoding="utf-8")
    plaintext = yaml.safe_dump({CLIENT_SECRET_KEY: SYNTHETIC_CLIENT_SECRET}).encode("utf-8")
    secrets_path = tmp_path / "secrets.age"
    secrets_path.write_bytes(pyrage.encrypt(plaintext, [identity.to_public()]))
    settings = Settings(
        config_path=EXAMPLE_CONFIG,
        data_dir=tmp_path / "data",
        secrets_path=secrets_path,
        age_identity_path=identity_path,
    )
    save_refresh_token(google_token_path(settings.data_dir, ACCOUNT), identity, SYNTHETIC_REFRESH_TOKEN)
    return settings


def _run(settings: Settings, transport: GoogleTransport) -> int:
    """Run ``google-setup`` for the example's account."""
    return main(
        ["google-setup", "--account", ACCOUNT],
        settings=settings,
        google_transport=httpx.MockTransport(transport),
    )


def test_setup_writes_the_key_then_the_configured_columns(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R-6.2: the header written is Clave followed by the sink's columns in order."""
    transport = GoogleTransport()

    assert _run(_settings(tmp_path), transport) == 0
    capsys.readouterr()

    assert len(transport.header_writes) == 1
    write = transport.header_writes[0]
    assert write.url.params["valueInputOption"] == RAW
    assert json.loads(write.content) == {"values": [["Clave", *COLUMNS]]}


def test_setup_prints_the_created_ids(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R-6.2: the printed lines carry exactly the folder and spreadsheet Google created."""
    transport = GoogleTransport()

    assert _run(_settings(tmp_path), transport) == 0

    assert capsys.readouterr().out == f"root_folder_id: {FOLDER_ID}\nspreadsheet_id: {SPREADSHEET_ID}\n"


def test_setup_unknown_account_exits_two_without_a_request(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R-6.5: an account no Google sheets sink names exits 2 and makes no request."""
    transport = GoogleTransport()

    with pytest.raises(SystemExit) as exit_info:
        main(
            ["google-setup", "--account", "nobody"],
            settings=_settings(tmp_path),
            google_transport=httpx.MockTransport(transport),
        )

    assert exit_info.value.code == 2
    assert transport.requests == []
    assert "nobody" in capsys.readouterr().err


async def test_sheets_create_without_an_id_is_a_google_error() -> None:
    """R-6.2: a create answer without spreadsheetId is a GoogleError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sheets = GoogleSheets(http, FakeTokens(), TIMEOUT_S, account=ACCOUNT, max_rows=MAX_ROWS)
        with pytest.raises(GoogleError):
            await sheets.create("Comprobantes", TAB, ["Clave", *COLUMNS])
