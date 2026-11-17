"""Google Drive REST client for flow A delivery (R-6.1, R-6.3, R-6.4, D129).

The client is plain ``httpx``; tokens come from an injected :class:`TokenSource`
and are fetched per call. Every request has the configured timeout and a bearer
header. Folders and files are found by name before anything is created, so a
retried delivery reuses what it already uploaded (R-6.3). Ids that come back
from Google are checked against :data:`GOOGLE_ID_PATTERN` before they are used.
Errors carry the HTTP status only, never a URL, a path or a body (R-9.16).
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from invoicebot.config.models import GOOGLE_ID_PATTERN
from invoicebot.sinks.base import TokenSource
from invoicebot.sinks.google_api import GoogleError, auth_headers, raise_google_error

#: The Drive v3 endpoint; the only host this client talks to.
DRIVE_URL = "https://www.googleapis.com/drive/v3"

#: The upload endpoint that takes a multipart body.
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"

#: The MIME type of a Drive folder.
FOLDER_MIME = "application/vnd.google-apps.folder"

#: The fields a lookup needs: the id to reuse and the link to report.
_LOOKUP_FIELDS = "files(id,webViewLink)"

#: The number of random bytes in a multipart boundary.
_BOUNDARY_BYTES = 16


@dataclass(frozen=True)
class DriveFile:
    """A file on the drive: its Google id and its browser link (R-6.1)."""

    id: str
    link: str


def _escape_query(value: str) -> str:
    """Return ``value`` as a Drive query literal (R-6.1).

    The backslash is escaped first, then the single quote, so the quote's own
    backslash is not doubled a second time.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _json_object(response: httpx.Response) -> dict[str, object]:
    """Return the decoded JSON object, or raise when the body is not one."""
    try:
        body: object = response.json()
    except ValueError as err:
        raise GoogleError() from err
    if not isinstance(body, dict):
        raise GoogleError()
    return body


def _read_id(item: Mapping[str, object]) -> str:
    """Return the item's id, refusing one Google should never have sent."""
    value = item.get("id")
    if not isinstance(value, str) or GOOGLE_ID_PATTERN.fullmatch(value) is None:
        raise GoogleError()
    return value


def _read_file(item: Mapping[str, object]) -> DriveFile:
    """Return the item as a :class:`DriveFile`, requiring both id and link."""
    file_id = _read_id(item)
    link = item.get("webViewLink")
    if not isinstance(link, str) or not link:
        raise GoogleError()
    return DriveFile(id=file_id, link=link)


def _multipart_body(boundary: str, name: str, folder_id: str, data: bytes, mime: str) -> bytes:
    """Build the ``multipart/related`` body: JSON metadata, then the media (R-6.1)."""
    metadata = json.dumps({"name": name, "parents": [folder_id]}).encode()
    metadata_head = f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
    media_head = f"\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return metadata_head + metadata + media_head + data + tail


class GoogleDrive:
    """One Google account's drive, over an injected HTTP client (D129).

    Long-lived: one instance per signed-in account.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        tokens: TokenSource,
        timeout_s: float,
        *,
        account: str,
    ) -> None:
        self._http = http
        self._tokens = tokens
        self._timeout_s = timeout_s
        self._account = account

    async def ensure_folder(self, parent_id: str, name: str) -> str:
        """Return the id of the folder ``name`` under ``parent_id`` (R-6.1).

        An existing folder is reused; only when the lookup finds nothing is one
        created.
        """
        query = (
            f"'{parent_id}' in parents and name = '{_escape_query(name)}' "
            f"and mimeType = '{FOLDER_MIME}' and trashed = false"
        )
        found = await self._find(query)
        if found is not None:
            return _read_id(found)
        response = await self._http.post(
            f"{DRIVE_URL}/files",
            params={"fields": "id"},
            json={"name": name, "parents": [parent_id], "mimeType": FOLDER_MIME},
            headers=await self._headers(),
            timeout=self._timeout_s,
        )
        raise_google_error(response, self._account)
        return _read_id(_json_object(response))

    async def put_file(self, folder_id: str, name: str, data: bytes, mime: str) -> DriveFile:
        """Upload ``data`` as ``name`` under ``folder_id`` (R-6.1, R-6.3).

        A file with that name is reused and nothing is uploaded; otherwise one
        ``multipart/related`` request carries the metadata and the bytes.
        """
        query = f"'{folder_id}' in parents and name = '{_escape_query(name)}' and trashed = false"
        found = await self._find(query)
        if found is not None:
            return _read_file(found)
        boundary = secrets.token_hex(_BOUNDARY_BYTES)
        response = await self._http.post(
            UPLOAD_URL,
            params={"uploadType": "multipart", "fields": "id,webViewLink"},
            content=_multipart_body(boundary, name, folder_id, data, mime),
            headers={
                **await self._headers(),
                "Content-Type": f'multipart/related; boundary="{boundary}"',
            },
            timeout=self._timeout_s,
        )
        raise_google_error(response, self._account)
        return _read_file(_json_object(response))

    def folder_link(self, folder_id: str) -> str:
        """Return the browser link of a Drive folder (R-6.1)."""
        return f"https://drive.google.com/drive/folders/{folder_id}"

    async def _headers(self) -> dict[str, str]:
        return auth_headers(await self._tokens.token())

    async def _find(self, query: str) -> dict[str, object] | None:
        """Return the first file matching ``query``, or None when there is none."""
        response = await self._http.get(
            f"{DRIVE_URL}/files",
            params={
                "q": query,
                "fields": _LOOKUP_FIELDS,
                "pageSize": "1",
                "spaces": "drive",
            },
            headers=await self._headers(),
            timeout=self._timeout_s,
        )
        raise_google_error(response, self._account)
        files = _json_object(response).get("files")
        if not isinstance(files, list) or not files:
            return None
        first = files[0]
        if not isinstance(first, dict):
            raise GoogleError()
        _read_id(first)
        return first
