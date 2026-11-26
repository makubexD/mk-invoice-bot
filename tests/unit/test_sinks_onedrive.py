"""Tests for the OneDrive Graph client (R-7.1, R-7.2, R-7.3, R-5.10)."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from invoicebot.auth.msal_cache import AuthRequired
from invoicebot.sinks.onedrive import (
    GRAPH_BASE_URL,
    MAX_LIST_PAGES,
    NOT_SENT_STATUS,
    UPLOAD_CHUNK_BYTES,
    FileTooLarge,
    FolderExists,
    FolderMissing,
    GraphDrive,
    GraphError,
    GraphTransient,
    _encode_path,
)

ACCOUNT = "personal"
BEARER = "synthetic-bearer-value"
TIMEOUT_S = 5.0
CHILDREN_URL = f"{GRAPH_BASE_URL}/me/drive/root:/folder:/children?$select=name&$top=200"

Handler = Callable[[httpx.Request], httpx.Response]


class FakeTokens:
    """A TokenSource that always hands out the same synthetic token."""

    async def token(self) -> str:
        return BEARER


@contextlib.asynccontextmanager
async def _make_drive(handler: Handler) -> AsyncIterator[GraphDrive]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        yield GraphDrive(http, FakeTokens(), TIMEOUT_S, account=ACCOUNT)


async def test_list_names_follows_next_link_and_returns_every_name() -> None:
    """R-7.1: a listing follows @odata.nextLink on the same host and collects every page."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "value": [{"name": "2026-11-Support_1_A"}],
                    "@odata.nextLink": f"{GRAPH_BASE_URL}/me/drive/root:/folder:/children?$skiptoken=2",
                },
            )
        return httpx.Response(200, json={"value": [{"name": "2026-11-Support_2_B"}]})

    async with _make_drive(handler) as drive:
        names = await drive.list_names("/folder")

    assert names == ["2026-11-Support_1_A", "2026-11-Support_2_B"]
    assert len(requests) == 2
    assert str(requests[0].url) == CHILDREN_URL
    assert requests[0].headers["Authorization"] == f"Bearer {BEARER}"


async def test_list_names_of_a_missing_folder_returns_empty() -> None:
    """R-7.1: a 404 for a folder that does not exist lists no names."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with _make_drive(handler) as drive:
        names = await drive.list_names("/missing")

    assert names == []


async def test_download_returns_the_body_of_the_content_endpoint() -> None:
    """R-7.14: a download reads the file's /content and returns its bytes."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"receipt")

    async with _make_drive(handler) as drive:
        data = await drive.download("/root/_outbox/receipts/a.json", 4096)

    assert data == b"receipt"
    assert str(requests[0].url) == f"{GRAPH_BASE_URL}/me/drive/root:/root/_outbox/receipts/a.json:/content"


async def test_download_of_a_missing_file_raises_folder_missing() -> None:
    """R-7.14: a 404 on download raises FolderMissing."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with _make_drive(handler) as drive:
        with pytest.raises(FolderMissing):
            await drive.download("/missing.json", 4096)


async def test_download_over_the_cap_raises_file_too_large() -> None:
    """R-7.14: a body one byte over the cap is abandoned with FileTooLarge."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 4097)

    async with _make_drive(handler) as drive:
        with pytest.raises(FileTooLarge, match="4096"):
            await drive.download("/big.json", 4096)


async def test_list_names_at_the_root_uses_the_root_children_endpoint() -> None:
    """R-7.1: listing the drive root hits /me/drive/root/children, not root:/children."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"value": []})

    async with _make_drive(handler) as drive:
        names = await drive.list_names("/")

    assert names == []
    assert str(requests[0].url) == f"{GRAPH_BASE_URL}/me/drive/root/children?$select=name&$top=200"


async def test_list_names_percent_encodes_path_segments() -> None:
    """R-7.1: a space in a folder name is encoded before the request is built."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"value": []})

    async with _make_drive(handler) as drive:
        await drive.list_names("/a/b c")

    assert str(requests[0].url).startswith(f"{GRAPH_BASE_URL}/me/drive/root:/a/b%20c:/children?")


async def test_list_names_does_not_follow_a_next_link_to_another_host() -> None:
    """R-7.1: a nextLink outside the Graph endpoint stops the listing with what it collected."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "value": [{"name": "2026-11-Support_1_A"}],
                "@odata.nextLink": "https://elsewhere.example/children?$skiptoken=2",
            },
        )

    async with _make_drive(handler) as drive:
        names = await drive.list_names("/folder")

    assert names == ["2026-11-Support_1_A"]
    assert len(requests) == 1


def _page(number: int, *, has_next: bool) -> dict[str, object]:
    """Return one children page with a graph.microsoft.com next link when asked."""
    body: dict[str, object] = {"value": [{"name": f"name-{number}"}]}
    if has_next:
        body["@odata.nextLink"] = f"{GRAPH_BASE_URL}/me/drive/root:/folder:/children?$skiptoken={number + 1}"
    return body


async def test_list_names_of_three_pages_returns_every_name_in_order() -> None:
    """R-7.1: a three-page listing collects every name in order and stops at the last page."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = len(requests)
        return httpx.Response(200, json=_page(page, has_next=page < 3))

    async with _make_drive(handler) as drive:
        names = await drive.list_names("/folder")

    assert names == ["name-1", "name-2", "name-3"]
    assert len(requests) == 3


async def test_list_names_accepts_exactly_fifty_pages() -> None:
    """R-7.1: fifty pages whose last has no next link is a complete listing, not a cap error."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = len(requests)
        return httpx.Response(200, json=_page(page, has_next=page < MAX_LIST_PAGES))

    async with _make_drive(handler) as drive:
        names = await drive.list_names("/folder")

    assert names == [f"name-{page}" for page in range(1, MAX_LIST_PAGES + 1)]
    assert len(requests) == MAX_LIST_PAGES


async def test_list_names_raises_when_the_fiftieth_page_has_a_next_link() -> None:
    """R-7.1: the page past the cap is never requested; the listing fails with GraphError."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_page(len(requests), has_next=True))

    async with _make_drive(handler) as drive:
        with pytest.raises(GraphError) as raised:
            await drive.list_names("/folder")

    assert raised.value.status == NOT_SENT_STATUS
    assert len(requests) == MAX_LIST_PAGES


async def test_create_folder_sends_conflict_behavior_fail_and_accepts_409() -> None:
    """R-7.1: creating a folder asks Graph to fail on a conflict, and a 409 is accepted."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(409)

    async with _make_drive(handler) as drive:
        await drive.create_folder("/", "2026-11-Support_1_LaLena")

    assert len(requests) == 1
    assert str(requests[0].url) == f"{GRAPH_BASE_URL}/me/drive/root/children"
    assert json.loads(requests[0].content) == {
        "name": "2026-11-Support_1_LaLena",
        "folder": {},
        "@microsoft.graph.conflictBehavior": "fail",
    }


async def test_create_folder_creates_a_missing_parent_once_then_retries() -> None:
    """R-7.1: a 404 on the parent creates it once and retries the original creation once."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(404)
        return httpx.Response(201)

    async with _make_drive(handler) as drive:
        await drive.create_folder("/2026-11", "2026-11-Support_1_LaLena")

    assert [json.loads(request.content)["name"] for request in requests] == [
        "2026-11-Support_1_LaLena",
        "2026-11",
        "2026-11-Support_1_LaLena",
    ]
    assert str(requests[1].url) == f"{GRAPH_BASE_URL}/me/drive/root/children"


async def test_create_folder_does_not_retry_twice_when_the_parent_creation_fails() -> None:
    """R-7.1: the parent creation is attempted once; a second 404 is the final error."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404)

    async with _make_drive(handler) as drive:
        with pytest.raises(GraphError) as raised:
            await drive.create_folder("/2026-11", "2026-11-Support_1_LaLena")

    assert raised.value.status == 404
    assert len(requests) == 2


async def test_upload_up_to_four_megabytes_is_one_put() -> None:
    """R-7.2: a 3 MB file goes in one PUT to the content endpoint, with the bearer header."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201)

    async with _make_drive(handler) as drive:
        await drive.upload("/folder/invoice.pdf", b"x" * (3 * 1024 * 1024))

    assert len(requests) == 1
    assert requests[0].method == "PUT"
    assert str(requests[0].url) == f"{GRAPH_BASE_URL}/me/drive/root:/folder/invoice.pdf:/content"
    assert requests[0].headers["Authorization"] == f"Bearer {BEARER}"


async def test_upload_above_four_megabytes_chunks_through_an_upload_session() -> None:
    """R-7.2: a 5 MB file opens a session and sends correctly ranged chunks without auth."""
    requests: list[httpx.Request] = []
    size = 5 * 1024 * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"uploadUrl": "https://sn3302.up.1drv.com/up/session"})
        if len(requests) == 2:
            return httpx.Response(202)
        return httpx.Response(200)

    async with _make_drive(handler) as drive:
        await drive.upload("/folder/invoice.pdf", b"x" * size)

    assert requests[0].method == "POST"
    assert str(requests[0].url) == (
        f"{GRAPH_BASE_URL}/me/drive/root:/folder/invoice.pdf:/createUploadSession"
    )
    assert json.loads(requests[0].content) == {"item": {"@microsoft.graph.conflictBehavior": "replace"}}
    assert requests[0].headers["Authorization"] == f"Bearer {BEARER}"

    chunks = requests[1:]
    assert len(chunks) == 2
    first_length = UPLOAD_CHUNK_BYTES
    assert all("Authorization" not in chunk.headers for chunk in chunks)
    assert chunks[0].headers["Content-Range"] == f"bytes 0-{first_length - 1}/{size}"
    assert chunks[1].headers["Content-Range"] == f"bytes {first_length}-{size - 1}/{size}"
    assert chunks[0].headers["Content-Length"] == str(first_length)
    assert chunks[1].headers["Content-Length"] == str(size - first_length)


async def test_upload_session_never_confirmed_raises() -> None:
    """R-7.2: a session whose last chunk still answers 202 never reports the file as uploaded."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"uploadUrl": "https://sn3302.up.1drv.com/up/session"})
        return httpx.Response(202)

    async with _make_drive(handler) as drive:
        with pytest.raises(GraphError):
            await drive.upload("/folder/invoice.pdf", b"x" * (5 * 1024 * 1024))


async def test_upload_rejects_a_non_https_session_url_before_chunking() -> None:
    """R-5.10: an upload session URL that is not HTTPS is refused and no chunk is sent."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"uploadUrl": "http://sn3302.up.1drv.com/up/session"})

    async with _make_drive(handler) as drive:
        with pytest.raises(GraphError):
            await drive.upload("/folder/invoice.pdf", b"x" * (5 * 1024 * 1024))

    assert len(requests) == 1


@pytest.mark.parametrize(
    "upload_url",
    [
        "https://sn3302.up.1drv.com/up/abc",
        "https://api.onedrive.com/rup/abc",
        "https://api.files.1drv.com/up/abc",
        "https://graph.microsoft.com/v1.0/upload/abc",
        "https://outlook.office.com/api/v2.0/abc",
        "https://SN3302.UP.1DRV.COM/up/abc",
    ],
)
async def test_upload_accepts_a_microsoft_upload_host(upload_url: str) -> None:
    """R-5.10: an upload session on a Microsoft host receives the chunks."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"uploadUrl": upload_url})
        return httpx.Response(200)

    async with _make_drive(handler) as drive:
        await drive.upload("/folder/invoice.pdf", b"x" * (5 * 1024 * 1024))

    chunks = [request for request in requests if request.method == "PUT"]
    assert chunks
    assert all(str(chunk.url) == str(httpx.URL(upload_url)) for chunk in chunks)


@pytest.mark.parametrize(
    "upload_url",
    [
        "https://evil.example/upload",
        "https://sharepoint.com.evil.example/u",
        "http://sn3302.up.1drv.com/u",
        "https://user@sn3302.up.1drv.com/u",
        "https://tenant-my.sharepoint.com/u",
        "https://evil.office.com/u",
        "https://up.1drv.com.evil.example/u",
        "https://evil\uff0fup.1drv.com/u",
    ],
)
async def test_upload_refuses_a_session_url_outside_microsoft_before_chunking(upload_url: str) -> None:
    """R-5.10: a session URL that is not HTTPS on a Microsoft host is refused with no PUT."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"uploadUrl": upload_url})

    async with _make_drive(handler) as drive:
        with pytest.raises(GraphError) as raised:
            await drive.upload("/folder/invoice.pdf", b"x" * (5 * 1024 * 1024))

    assert raised.value.status == NOT_SENT_STATUS
    assert [request.method for request in requests] == ["POST"]


async def test_invalid_path_raises_before_any_request() -> None:
    """R-7.2: a path with a .. segment is rejected before the drive is contacted."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"value": []})

    async with _make_drive(handler) as drive:
        with pytest.raises(ValueError, match="invalid drive path segment"):
            await drive.list_names("/folder/../other")

    assert requests == []


async def test_unsafe_character_in_a_path_raises_before_any_request() -> None:
    """R-12.8: a path segment with a bidi override is rejected before the drive is contacted."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"value": []})

    async with _make_drive(handler) as drive:
        with pytest.raises(ValueError, match="invalid drive path segment"):
            await drive.list_names("/folder/\u202eevil")

    assert requests == []


async def test_a_401_maps_to_auth_required_for_the_account() -> None:
    """R-7.3: a 401 means the account needs a sign-in and names the account only."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    async with _make_drive(handler) as drive:
        with pytest.raises(AuthRequired) as raised:
            await drive.list_names("/folder")

    assert raised.value.account == ACCOUNT
    assert "folder" not in str(raised.value)


async def test_a_503_maps_to_graph_transient() -> None:
    """R-7.3: a 5xx is a transient failure the queue retries, carrying only the status."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503)

    async with _make_drive(handler) as drive:
        with pytest.raises(GraphTransient) as raised:
            await drive.upload("/folder/invoice.pdf", b"data")

    assert raised.value.status == 503
    assert "invoice.pdf" not in str(raised.value)


async def test_a_400_maps_to_graph_error() -> None:
    """R-7.3: any other 4xx is final and its message carries the status only."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400)

    async with _make_drive(handler) as drive:
        with pytest.raises(GraphError) as raised:
            await drive.create_folder("/folder", "child")

    assert raised.value.status == 400
    assert "child" not in str(raised.value)


async def test_move_patches_the_item_with_the_new_name_and_parent() -> None:
    """R-7.11: a move PATCHes the encoded item URL with the new name and parent reference."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "synthetic"})

    async with _make_drive(handler) as drive:
        await drive.move("/Reembolsos/Pendientes/F001-123.pdf", "/Reembolsos/2026", "F001-123_Sent.pdf")

    assert len(requests) == 1
    assert requests[0].method == "PATCH"
    assert str(requests[0].url) == f"{GRAPH_BASE_URL}/me/drive/root:/Reembolsos/Pendientes/F001-123.pdf"
    assert requests[0].headers["Authorization"] == f"Bearer {BEARER}"
    assert json.loads(requests[0].content) == {
        "name": "F001-123_Sent.pdf",
        "parentReference": {"path": "/drive/root:/Reembolsos/2026"},
    }


@pytest.mark.parametrize(
    ("status", "error"),
    [(404, FolderMissing), (409, FolderExists), (503, GraphTransient)],
)
async def test_move_maps_a_failed_status_to_its_error(
    status: int, error: type[GraphError | GraphTransient]
) -> None:
    """R-7.11: a 404 is FolderMissing, a 409 FolderExists and a 5xx GraphTransient."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    async with _make_drive(handler) as drive:
        with pytest.raises(error, match=f"status {status}") as raised:
            await drive.move("/Reembolsos/Pendientes/F001-123.pdf", "/Reembolsos/2026", "F001-123_Sent.pdf")

    assert raised.value.status == status


async def test_web_url_returns_the_items_browser_link() -> None:
    """D194: the item's own webUrl is returned from its encoded item URL, never a share link."""
    path = "/Reembolsos/Año 2026"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"webUrl": "https://onedrive.live.com/?id=X"})

    async with _make_drive(handler) as drive:
        url = await drive.web_url(path)

    assert url == "https://onedrive.live.com/?id=X"
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.raw_path.split(b"?")[0] == ("/v1.0/me/drive/root:" + _encode_path(path)).encode()
    assert requests[0].url.params["$select"] == "webUrl"
    assert requests[0].headers["Authorization"] == f"Bearer {BEARER}"


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (404, None, FolderMissing),
        (200, {"webUrl": "http://x"}, GraphError),
        (200, {}, GraphError),
        (503, None, GraphTransient),
    ],
)
async def test_web_url_maps_a_failed_answer_to_its_error(
    status: int, body: dict[str, object] | None, error: type[GraphError | GraphTransient]
) -> None:
    """D194: a 404 is FolderMissing, a missing or non-HTTPS webUrl GraphError and a 5xx GraphTransient."""

    def handler(request: httpx.Request) -> httpx.Response:
        if body is None:
            return httpx.Response(status)
        return httpx.Response(status, json=body)

    async with _make_drive(handler) as drive:
        with pytest.raises(error):
            await drive.web_url("/Reembolsos/2026")


async def test_delete_sends_a_delete_to_the_encoded_item_url() -> None:
    """R-7.14: a 204 deletes the item with a DELETE on its encoded path."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    async with _make_drive(handler) as drive:
        await drive.delete("/root/_outbox/receipts/a b.json")

    assert len(requests) == 1
    assert requests[0].method == "DELETE"
    assert str(requests[0].url) == f"{GRAPH_BASE_URL}/me/drive/root:/root/_outbox/receipts/a%20b.json"
    assert requests[0].headers["Authorization"] == f"Bearer {BEARER}"


async def test_delete_of_a_missing_item_returns_without_error() -> None:
    """R-7.14: a 404 means the item is already gone and is not an error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with _make_drive(handler) as drive:
        await drive.delete("/root/_outbox/receipts/gone.json")


async def test_delete_maps_a_server_error_to_graph_transient() -> None:
    """R-7.14: a 500 on delete is retried as a transient failure."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    async with _make_drive(handler) as drive:
        with pytest.raises(GraphTransient) as raised:
            await drive.delete("/root/_outbox/receipts/a.json")

    assert raised.value.status == 500
    assert len(requests) == 1


async def test_delete_refuses_a_dotdot_segment_before_any_request() -> None:
    """R-7.14: a path with a .. segment is refused before the drive is contacted."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    async with _make_drive(handler) as drive:
        with pytest.raises(ValueError, match="invalid drive path segment"):
            await drive.delete("/root/_outbox/../a.json")

    assert requests == []


async def test_delete_refuses_the_drive_root_before_any_request() -> None:
    """R-7.14: the drive root is never deleted, and no request is sent."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    async with _make_drive(handler) as drive:
        with pytest.raises(ValueError, match="drive root"):
            await drive.delete("/")

    assert requests == []
