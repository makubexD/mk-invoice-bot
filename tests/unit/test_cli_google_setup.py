"""Tests for ``invoicebot google-setup`` (R-6.2, R-6.5, D129, D145)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
FLOW = "personal_google"
CLIENT_SECRET_KEY = "google_client_secret"  # noqa: S105  # the store key's name, not a value
SYNTHETIC_CLIENT_SECRET = "synthetic-client-secret"  # noqa: S105  # a synthetic test value
SYNTHETIC_REFRESH_TOKEN = "synthetic-refresh"  # noqa: S105  # a synthetic test value
TOKEN_RESPONSE = {"access_token": "synthetic-access", "expires_in": 3600}
FOLDER_ID = "folder-id-123456"
SPREADSHEET_ID = "spreadsheet-id-123456"
EXISTING_SPREADSHEET_ID = "existing-spreadsheet-id"
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
HEADER = ["Clave", *COLUMNS]


class FakeTokens:
    """A TokenSource that always hands out the same synthetic token."""

    async def token(self) -> str:
        return BEARER


class GoogleTransport:
    """Answer the token, Drive and Sheets calls of one ``google-setup`` run (D145)."""

    def __init__(self, *, existing_header: list[str] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.header_writes: list[httpx.Request] = []
        self.existing_header = HEADER if existing_header is None else existing_header

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url == httpx.URL(TOKEN_URL):
            return httpx.Response(200, json=TOKEN_RESPONSE)
        if path == "/drive/v3/files":
            body = {"id": FOLDER_ID} if request.method == "POST" else {"files": []}
            return httpx.Response(200, json=body)
        if path == "/v4/spreadsheets" and request.method == "POST":
            return httpx.Response(200, json={"spreadsheetId": SPREADSHEET_ID})
        if path.startswith("/v4/spreadsheets/") and "/values/" in path:
            if request.method == "PUT":
                self.header_writes.append(request)
                return httpx.Response(200, json={})
            if request.method == "GET":
                return httpx.Response(200, json={"values": [self.existing_header]})
        if path.startswith("/v4/spreadsheets/") and request.method == "GET":
            return httpx.Response(200, json={"sheets": [{"properties": {"title": TAB}}]})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    def drive_requests(self) -> list[httpx.Request]:
        """Return the Drive requests, if any, this run made."""
        return [request for request in self.requests if request.url.path.startswith("/drive/")]


def _settings(tmp_path: Path, *, config_path: Path = EXAMPLE_CONFIG) -> Settings:
    """Build settings on ``config_path``, with a saved refresh token and synthetic age files."""
    identity = pyrage.x25519.Identity.generate()
    identity_path = tmp_path / "identity.txt"
    identity_path.write_text(str(identity), encoding="utf-8")
    plaintext = yaml.safe_dump({CLIENT_SECRET_KEY: SYNTHETIC_CLIENT_SECRET}).encode("utf-8")
    secrets_path = tmp_path / "secrets.age"
    secrets_path.write_bytes(pyrage.encrypt(plaintext, [identity.to_public()]))
    settings = Settings(
        config_path=config_path,
        data_dir=tmp_path / "data",
        secrets_path=secrets_path,
        age_identity_path=identity_path,
    )
    save_refresh_token(google_token_path(settings.data_dir, ACCOUNT), identity, SYNTHETIC_REFRESH_TOKEN)
    return settings


def _sheets_only_config(tmp_path: Path, *, existing: bool) -> Path:
    """Write the example config with flow A keeping only its sheets sink (D145)."""
    document: dict[str, Any] = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    flow = document["flows"][FLOW]
    sheet = next(sink for sink in flow["sinks"] if sink["kind"] == "google_sheets")
    sheet["existing"] = existing
    sheet["spreadsheet_id"] = EXISTING_SPREADSHEET_ID
    flow["sinks"] = [sheet]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _run(settings: Settings, transport: GoogleTransport, *, flow: str = FLOW) -> int:
    """Run ``google-setup`` for ``flow``."""
    return main(
        ["google-setup", "--flow", flow],
        settings=settings,
        google_transport=httpx.MockTransport(transport),
    )


def test_setup_creates_the_sheet_with_the_key_then_the_configured_columns(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D145: a created sheet's header is Clave followed by the sink's columns in order."""
    transport = GoogleTransport()

    assert _run(_settings(tmp_path), transport) == 0
    capsys.readouterr()

    assert len(transport.header_writes) == 1
    write = transport.header_writes[0]
    assert write.url.params["valueInputOption"] == RAW
    assert json.loads(write.content) == {"values": [HEADER]}


def test_setup_drive_and_sheets_flow_prints_both_ids(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D145: a flow with Drive and Sheets prints the created folder and spreadsheet ids."""
    transport = GoogleTransport()

    assert _run(_settings(tmp_path), transport) == 0

    assert capsys.readouterr().out == f"root_folder_id: {FOLDER_ID}\nspreadsheet_id: {SPREADSHEET_ID}\n"


def test_setup_sheets_only_flow_prints_only_the_spreadsheet_id(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D145: a flow without a Drive sink creates only the sheet and makes no Drive request."""
    config_path = _sheets_only_config(tmp_path, existing=False)
    transport = GoogleTransport()

    assert _run(_settings(tmp_path, config_path=config_path), transport) == 0

    assert capsys.readouterr().out == f"spreadsheet_id: {SPREADSHEET_ID}\n"
    assert transport.drive_requests() == []
    assert len(transport.header_writes) == 1


def test_setup_existing_sheet_with_matching_header_prints_checked(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D145: an existing sheet whose row 1 matches is checked and nothing is created."""
    config_path = _sheets_only_config(tmp_path, existing=True)
    transport = GoogleTransport()

    assert _run(_settings(tmp_path, config_path=config_path), transport) == 0

    assert capsys.readouterr().out == f"spreadsheet_id: {EXISTING_SPREADSHEET_ID} (checked)\n"
    assert transport.header_writes == []
    assert transport.drive_requests() == []
    assert not any(
        request.url.path == "/v4/spreadsheets" and request.method == "POST" for request in transport.requests
    )


def test_setup_existing_sheet_with_a_different_header_exits_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D145: an existing tab whose first row differs exits 1 and names the expected header."""
    config_path = _sheets_only_config(tmp_path, existing=True)
    transport = GoogleTransport(existing_header=["Clave", "Otra"])

    assert _run(_settings(tmp_path, config_path=config_path), transport) == 1

    error = capsys.readouterr().err
    assert error.startswith("google-setup failed: ")
    assert "expected header" in error
    assert transport.header_writes == []


def test_setup_unknown_flow_exits_two_without_a_request(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D145: a flow name the config does not define exits 2 and makes no request."""
    transport = GoogleTransport()

    with pytest.raises(SystemExit) as exit_info:
        _run(_settings(tmp_path), transport, flow="nobody")

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
            await sheets.create("Comprobantes", TAB, HEADER)
