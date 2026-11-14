"""OneDrive delivery over Microsoft Graph's REST API (R-7.1, R-7.2, R-7.3, D84).

The client is plain ``httpx``; tokens come from an injected :class:`TokenSource`
and are fetched per call. Every request has the configured timeout and a bearer
header, except the upload-session chunk ``PUT``s, which Graph expects without
one. Errors carry the HTTP status only, never a URL, a path or a body (R-9.16).
"""

from __future__ import annotations

import unicodedata
import urllib.parse
from pathlib import PurePosixPath

import httpx

from invoicebot.auth.msal_cache import AuthRequired
from invoicebot.sinks.base import TokenSource

#: The Graph v1.0 endpoint; the only host this client talks to (D84).
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

#: Graph's simple upload ceiling; above it an upload session is opened.
MAX_SIMPLE_UPLOAD_BYTES = 4 * 1024 * 1024

#: Graph requires session chunks to be a multiple of 320 KiB; this is ten of them.
UPLOAD_CHUNK_BYTES = 10 * 320 * 1024

#: The status of an error raised before any follow-up request was sent.
NOT_SENT_STATUS = 0

#: The hosts an upload session may point at, exactly (D84).
UPLOAD_HOSTS = ("graph.microsoft.com",)

#: The suffixes an upload session host may end with; Microsoft's regional drives.
UPLOAD_HOST_SUFFIXES = (".sharepoint.com", ".files.1drv.com", ".office.com")

#: A segment that may not appear in a drive path.
_FORBIDDEN_SEGMENT_CHARS = "/\\:"


class GraphError(Exception):
    """A Graph request failed; the status is all the message carries (R-7.3)."""

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"Microsoft Graph request failed with status {status}")


class GraphTransient(Exception):
    """A Graph failure the queue retries (429 or 5xx; R-7.3)."""

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"Microsoft Graph request failed with status {status}")


def raise_graph_error(response: httpx.Response, account: str) -> None:
    """Raise the exception a failed Graph response maps to (R-7.3).

    A 401 means the account needs a sign-in; 429 and 5xx are worth retrying;
    anything else is final. Only the status reaches the message (R-9.16).
    """
    status = response.status_code
    if status < 400:
        return
    if status == 401:
        raise AuthRequired(account)
    if status == 429 or status >= 500:
        raise GraphTransient(status)
    raise GraphError(status)


def auth_headers(token: str) -> dict[str, str]:
    """Return the bearer header for one Graph call (D84)."""
    return {"Authorization": f"Bearer {token}"}


def _validate_segment(segment: str) -> None:
    """Reject one path segment that could escape or alter the Graph path."""
    if (
        not segment
        or segment in {".", ".."}
        or any(char in segment for char in _FORBIDDEN_SEGMENT_CHARS)
        or any(unicodedata.category(char) == "Cc" for char in segment)
    ):
        raise ValueError("invalid drive path segment")


def _encode_path(path: str) -> str:
    """Validate an absolute drive path and percent-encode each segment.

    ``/a/b c`` becomes ``/a/b%20c``. The root ``/`` is returned as-is; callers
    decide where it is meaningful. Raises ``ValueError`` before any request.
    """
    if not path.startswith("/"):
        raise ValueError("drive path must be absolute")
    if path == "/":
        return "/"
    encoded: list[str] = []
    for segment in path.split("/")[1:]:
        _validate_segment(segment)
        encoded.append(urllib.parse.quote(segment, safe=""))
    return "/" + "/".join(encoded)


def _children_url(parent: str) -> str:
    """Return the Graph children endpoint for an absolute folder path."""
    encoded = _encode_path(parent)
    if encoded == "/":
        return "/me/drive/root/children"
    return f"/me/drive/root:{encoded}:/children"


def _json_body(response: httpx.Response) -> object:
    """Return the decoded JSON body, or None when the body is not JSON."""
    try:
        body: object = response.json()
    except ValueError:
        return None
    return body


def _names(body: object) -> list[str]:
    """Return the ``name`` of each usable entry in a children page."""
    if not isinstance(body, dict):
        return []
    value = body.get("value")
    if not isinstance(value, list):
        return []
    return [name for item in value if isinstance(item, dict) and isinstance(name := item.get("name"), str)]


def _next_link(body: object) -> str | None:
    """Return the page link only when it stays on this Graph endpoint."""
    if not isinstance(body, dict):
        return None
    link = body.get("@odata.nextLink")
    if isinstance(link, str) and link.startswith(f"{GRAPH_BASE_URL}/"):
        return link
    return None


def _upload_url(body: object) -> str | None:
    """Return the session URL from a createUploadSession answer."""
    if not isinstance(body, dict):
        return None
    url = body.get("uploadUrl")
    return url if isinstance(url, str) else None


def _check_upload_url(url: str) -> None:
    """Reject an upload session URL that is not HTTPS on a Microsoft upload host.

    Runs before any chunk is sent, so a token or a file never reaches a host the
    account does not own. Raises ``GraphError(NOT_SENT_STATUS)`` on a bad URL.
    """
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or not (host in UPLOAD_HOSTS or host.endswith(UPLOAD_HOST_SUFFIXES))
    ):
        raise GraphError(NOT_SENT_STATUS)


async def upload_chunks(
    client: httpx.AsyncClient,
    upload_url: str,
    data: bytes,
    *,
    timeout_s: float,
    account: str,
) -> None:
    """Upload ``data`` to a Graph upload session in fixed-size chunks (R-7.2, R-5.10).

    The session URL must be HTTPS on a Microsoft upload host
    (:data:`UPLOAD_HOSTS` or :data:`UPLOAD_HOST_SUFFIXES`); it is used as given
    and its ``PUT``s carry no bearer header, so a token never reaches a host
    other than one Microsoft chose. A chunk answering 200 or 201 means Graph
    stored the file; 202 means continue. A session that only ever answered 202
    never confirmed the file and raises ``GraphError`` rather than reporting
    success.
    """
    _check_upload_url(upload_url)
    total = len(data)
    for start in range(0, total, UPLOAD_CHUNK_BYTES):
        chunk = data[start : start + UPLOAD_CHUNK_BYTES]
        end = start + len(chunk) - 1
        chunk_response = await client.put(
            upload_url,
            content=chunk,
            headers={
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{end}/{total}",
            },
            timeout=timeout_s,
        )
        if chunk_response.status_code in (200, 201):
            return
        if chunk_response.status_code != 202:
            raise_graph_error(chunk_response, account)
    # Every chunk answered 202, so Graph never confirmed the file: never report that as success.
    raise GraphError(202)


class GraphDrive:
    """One Microsoft account's drive, over Graph and an injected HTTP client (D84).

    Long-lived: one instance per signed-in account.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        tokens: TokenSource,
        timeout_s: float,
        *,
        account: str,
    ) -> None:
        self._client = client
        self._tokens = tokens
        self._timeout_s = timeout_s
        self._account = account

    async def list_names(self, path: str) -> list[str]:
        """Return the names directly under ``path``; a missing folder lists ``[]`` (R-7.1)."""
        url = f"{GRAPH_BASE_URL}{_children_url(path)}?$select=name&$top=200"
        token = await self._tokens.token()
        names: list[str] = []
        while True:
            response = await self._client.get(url, headers=auth_headers(token), timeout=self._timeout_s)
            if response.status_code == 404:
                return []
            raise_graph_error(response, self._account)
            body = _json_body(response)
            names.extend(_names(body))
            next_url = _next_link(body)
            if next_url is None:
                return names
            url = next_url

    async def create_folder(self, parent: str, name: str) -> None:
        """Create ``name`` under ``parent``; an existing folder is accepted (R-7.1, D88).

        A 409 means the folder is already there. A 404 on the parent creates it
        once (and only once) before retrying the original request.
        """
        await self._create_folder(parent, name, retry_missing_parent=True)

    async def upload(self, path: str, data: bytes) -> None:
        """Write ``data`` to ``path``, replacing the file (R-7.2).

        Up to :data:`MAX_SIMPLE_UPLOAD_BYTES` is one ``PUT``; above that an
        upload session carries the file in fixed-size chunks.
        """
        encoded = _encode_path(path)
        if encoded == "/":
            raise ValueError("cannot upload to the drive root")
        token = await self._tokens.token()
        if len(data) <= MAX_SIMPLE_UPLOAD_BYTES:
            response = await self._client.put(
                f"{GRAPH_BASE_URL}/me/drive/root:{encoded}:/content",
                content=data,
                headers=auth_headers(token),
                timeout=self._timeout_s,
            )
            raise_graph_error(response, self._account)
            return
        await self._upload_in_session(encoded, data, token)

    async def _create_folder(self, parent: str, name: str, *, retry_missing_parent: bool) -> None:
        _validate_segment(name)
        children_url = _children_url(parent)
        response = await self._post_folder(children_url, name)
        if response.status_code in (200, 201, 409):
            return
        if response.status_code != 404 or not retry_missing_parent:
            raise_graph_error(response, self._account)
        missing = PurePosixPath(parent)
        if missing.parent == missing:
            raise GraphError(response.status_code)
        await self._create_folder(str(missing.parent), missing.name, retry_missing_parent=False)
        retried = await self._post_folder(children_url, name)
        if retried.status_code in (200, 201, 409):
            return
        raise_graph_error(retried, self._account)

    async def _post_folder(self, children_url: str, name: str) -> httpx.Response:
        token = await self._tokens.token()
        return await self._client.post(
            f"{GRAPH_BASE_URL}{children_url}",
            json={
                "name": name,
                "folder": {},
                "@microsoft.graph.conflictBehavior": "fail",
            },
            headers=auth_headers(token),
            timeout=self._timeout_s,
        )

    async def _upload_in_session(self, encoded_path: str, data: bytes, token: str) -> None:
        response = await self._client.post(
            f"{GRAPH_BASE_URL}/me/drive/root:{encoded_path}:/createUploadSession",
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
            headers=auth_headers(token),
            timeout=self._timeout_s,
        )
        raise_graph_error(response, self._account)
        upload_url = _upload_url(_json_body(response))
        if upload_url is None:
            raise GraphError(NOT_SENT_STATUS)
        await upload_chunks(self._client, upload_url, data, timeout_s=self._timeout_s, account=self._account)
