"""Tests for the Google Drive client (R-6.1, R-6.3, R-6.4)."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from invoicebot.auth.msal_cache import AuthRequired
from invoicebot.sinks.google_api import GoogleError, GoogleTransient
from invoicebot.sinks.google_drive import (
    DRIVE_URL,
    FOLDER_MIME,
    UPLOAD_URL,
    DriveFile,
    GoogleDrive,
)

ACCOUNT = "google"
BEARER = "synthetic-bearer-value"
TIMEOUT_S = 5.0
ROOT_ID = "root_folder_0123456789"
FOLDER_ID = "folder_0123456789ab"
FILE_ID = "file_0123456789abcd"
LINK = "https://drive.google.com/file/d/0123456789ab/view"

Handler = Callable[[httpx.Request], httpx.Response]


class FakeTokens:
    """A TokenSource that always hands out the same synthetic token."""

    async def token(self) -> str:
        return BEARER


@contextlib.asynccontextmanager
async def _make_drive(handler: Handler) -> AsyncIterator[GoogleDrive]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        yield GoogleDrive(http, FakeTokens(), TIMEOUT_S, account=ACCOUNT)


def _lookup_query(request: httpx.Request) -> str:
    """Return the ``q`` parameter of a files.list request."""
    return request.url.params["q"]


async def test_ensure_folder_creates_a_missing_folder() -> None:
    """R-6.1: a folder the lookup does not find is created under its parent."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        return httpx.Response(200, json={"id": FOLDER_ID})

    async with _make_drive(handler) as drive:
        folder_id = await drive.ensure_folder(ROOT_ID, "2026")

    assert folder_id == FOLDER_ID
    assert [request.method for request in requests] == ["GET", "POST"]
    assert str(requests[0].url).startswith(f"{DRIVE_URL}/files?")
    assert requests[0].headers["Authorization"] == f"Bearer {BEARER}"
    query = _lookup_query(requests[0])
    assert f"'{ROOT_ID}' in parents" in query
    assert "name = '2026'" in query
    assert f"mimeType = '{FOLDER_MIME}'" in query
    assert "trashed = false" in query
    assert requests[0].url.params["fields"] == "files(id,webViewLink)"
    assert requests[0].url.params["pageSize"] == "1"
    assert requests[0].url.params["spaces"] == "drive"

    assert str(requests[1].url) == f"{DRIVE_URL}/files?fields=id"
    assert json.loads(requests[1].content) == {
        "name": "2026",
        "parents": [ROOT_ID],
        "mimeType": FOLDER_MIME,
    }


async def test_ensure_folder_reuses_an_existing_folder() -> None:
    """R-6.1: a folder the lookup finds is reused and nothing is created."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"files": [{"id": FOLDER_ID, "webViewLink": LINK}]})

    async with _make_drive(handler) as drive:
        folder_id = await drive.ensure_folder(ROOT_ID, "2026")

    assert folder_id == FOLDER_ID
    assert [request.method for request in requests] == ["GET"]


async def test_put_file_uploads_the_metadata_and_media_parts() -> None:
    """R-6.1: an upload sends the name and parent as JSON, then the file bytes."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        return httpx.Response(200, json={"id": FILE_ID, "webViewLink": LINK})

    async with _make_drive(handler) as drive:
        uploaded = await drive.put_file(FOLDER_ID, "2026-11_F001-123.pdf", b"receipt", "application/pdf")

    assert uploaded == DriveFile(id=FILE_ID, link=LINK)
    assert [request.method for request in requests] == ["GET", "POST"]
    query = _lookup_query(requests[0])
    assert f"'{FOLDER_ID}' in parents" in query
    assert "name = '2026-11_F001-123.pdf'" in query
    assert "mimeType" not in query
    assert "trashed = false" in query

    upload = requests[1]
    assert str(upload.url).startswith(f"{UPLOAD_URL}?")
    assert upload.url.params["uploadType"] == "multipart"
    assert upload.url.params["fields"] == "id,webViewLink"
    assert upload.headers["Authorization"] == f"Bearer {BEARER}"
    content_type = upload.headers["Content-Type"]
    assert content_type.startswith("multipart/related; boundary=")
    boundary = content_type.split("boundary=", 1)[1].strip('"')
    parts = upload.content.split(b"--" + boundary.encode())
    metadata = json.loads(parts[1].split(b"\r\n\r\n", 1)[1].split(b"\r\n", 1)[0])
    assert metadata == {"name": "2026-11_F001-123.pdf", "parents": [FOLDER_ID]}
    assert b"Content-Type: application/pdf" in parts[2]
    assert parts[2].endswith(b"receipt\r\n")


async def test_put_file_reuses_an_existing_file_without_uploading() -> None:
    """R-6.3: a second put_file of the same name returns the stored file and sends no upload."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"files": [{"id": FILE_ID, "webViewLink": LINK}]})

    async with _make_drive(handler) as drive:
        uploaded = await drive.put_file(FOLDER_ID, "2026-11_F001-123.pdf", b"receipt", "application/pdf")

    assert uploaded == DriveFile(id=FILE_ID, link=LINK)
    assert [request.method for request in requests] == ["GET"]


async def test_put_file_reused_file_without_a_link_raises_google_error() -> None:
    """R-6.1: a reused file whose answer has no webViewLink is a GoogleError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"files": [{"id": FILE_ID}]})

    async with _make_drive(handler) as drive:
        with pytest.raises(GoogleError):
            await drive.put_file(FOLDER_ID, "2026-11_F001-123.pdf", b"receipt", "application/pdf")


async def test_a_quote_and_backslash_in_a_name_are_escaped_in_q() -> None:
    """R-6.1: a name's backslash is doubled first, then its quote is escaped."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        return httpx.Response(200, json={"id": FILE_ID, "webViewLink": LINK})

    async with _make_drive(handler) as drive:
        await drive.put_file(FOLDER_ID, "back\\slash'O", b"receipt", "application/pdf")

    assert "name = 'back\\\\slash\\'O'" in _lookup_query(requests[0])


async def test_a_malformed_id_from_google_raises_google_error() -> None:
    """R-6.1: an id Google hands back that does not match ID_PATTERN is refused."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"files": [{"id": "bad id!", "webViewLink": LINK}]})

    async with _make_drive(handler) as drive:
        with pytest.raises(GoogleError):
            await drive.ensure_folder(ROOT_ID, "2026")


async def test_folder_link_is_the_drive_folder_url() -> None:
    """R-6.1: the folder's reported link points at its Drive folder."""

    async with _make_drive(lambda request: httpx.Response(200)) as drive:
        link = drive.folder_link(FOLDER_ID)

    assert link == f"https://drive.google.com/drive/folders/{FOLDER_ID}"


async def test_a_401_raises_auth_required_for_the_account() -> None:
    """R-6.4: a 401 means the account needs a sign-in and names the account only."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401)

    async with _make_drive(handler) as drive:
        with pytest.raises(AuthRequired) as raised:
            await drive.ensure_folder(ROOT_ID, "2026")

    assert raised.value.account == ACCOUNT
    assert "2026" not in str(raised.value)


async def test_a_503_maps_to_google_transient() -> None:
    """R-6.4: a 5xx is a transient failure the queue retries, carrying only the status."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with _make_drive(handler) as drive:
        with pytest.raises(GoogleTransient) as raised:
            await drive.put_file(FOLDER_ID, "2026-11_F001-123.pdf", b"receipt", "application/pdf")

    assert raised.value.status == 503
    assert "2026-11_F001-123.pdf" not in str(raised.value)
