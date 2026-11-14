"""Integration tests for the browser helper process and its route guard (S10.1, S7.6)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from invoicebot.fetchers.base import FetchReason, FetchResult
from invoicebot.fetchers.browser.__main__ import _guarded_result
from invoicebot.fetchers.browser.routing import RouteGuard
from invoicebot.net.guard import GuardReason


class _FakeRequest:
    """The request attributes the guard reads."""

    def __init__(self, url: str, resource_type: str) -> None:
        self.url = url
        self.resource_type = resource_type


class _FakeRoute:
    """A route that remembers whether it was aborted or continued."""

    def __init__(self, url: str, resource_type: str) -> None:
        self.request = _FakeRequest(url, resource_type)
        self.aborted_with: str | None = None
        self.continued = False

    async def abort(self, error_code: str) -> None:
        """Record the abort code."""
        self.aborted_with = error_code

    async def continue_(self) -> None:
        """Record that the request was allowed."""
        self.continued = True


async def test_route_guard_aborts_font_without_recording_a_result() -> None:
    """R-5.10: a font request is dropped, leaving the fetch result untouched."""
    guard = RouteGuard(allow_images=False)
    route = _FakeRoute("https://cdn.example/font.woff2", "font")

    await guard.on_route(route)

    assert route.aborted_with == "blockedbyclient"
    assert not route.continued


async def test_route_guard_aborts_a_file_url() -> None:
    """R-5.10: a file: URL never reaches the proxy, so the guard aborts it."""
    guard = RouteGuard(allow_images=False)
    route = _FakeRoute("file:///etc/passwd", "document")

    await guard.on_route(route)

    assert route.aborted_with == "blockedbyclient"
    assert not route.continued


async def test_route_guard_continues_a_network_request() -> None:
    """R-5.10: an http or https request is left to the guard proxy."""
    guard = RouteGuard(allow_images=False)
    route = _FakeRoute("https://example.com/", "document")

    await guard.on_route(route)

    assert route.continued
    assert route.aborted_with is None


def test_guarded_result_keeps_not_found_yet_when_blocked() -> None:
    """R-5.10: a block with a not-found result keeps not_found_yet."""
    not_found = FetchResult(reason=FetchReason.NOT_FOUND_YET, files=(), portal_key="synthetic")

    assert _guarded_result(not_found, GuardReason.PRIVATE_ADDRESS) is not_found


def test_guarded_result_promotes_a_blocked_technical_result() -> None:
    """R-5.10: a block with a technical result gives guard_blocked."""
    technical = FetchResult(reason=FetchReason.TECHNICAL, files=(), portal_key="synthetic")

    promoted = _guarded_result(technical, GuardReason.PRIVATE_ADDRESS)

    assert promoted.reason is FetchReason.GUARD_BLOCKED
    assert promoted.portal_key == "synthetic"


def test_guarded_result_keeps_technical_without_a_block() -> None:
    """R-5.10: a technical result without a block stays technical."""
    technical = FetchResult(reason=FetchReason.TECHNICAL, files=(), portal_key="synthetic")

    assert _guarded_result(technical, None) is technical


def _request(out_dir: Path) -> dict[str, Any]:
    """One synthetic helper request for a portal on a loopback http URL."""
    return {
        "portal": {
            "key": "synthetic",
            "adapter": "generic_form",
            "url": "http://127.0.0.1:8080/",
            "status": "active",
            "fields": {"#ruc": "{ruc_emisor}"},
            "submit": "#buscar",
            "downloads": {"xml": "a:has-text('XML')", "pdf": "a:has-text('PDF')"},
            "not_found": "#no-encontrado",
            "allow_http": False,
        },
        "invoice": {
            "ruc_emisor": "20000000001",
            "tipo": "01",
            "serie": "F001",
            "numero": "123",
            "fecha": "2026-10-01",
            "total": "118.00",
        },
        "out_dir": str(out_dir),
        "max_bytes": {
            "image": 1024 * 1024,
            "pdf": 1024 * 1024,
            "xml": 1024 * 1024,
            "zip": 1024 * 1024,
        },
        "page_timeout_ms": 5000,
    }


def _run_helper(stdin: bytes) -> subprocess.CompletedProcess[bytes]:
    """Run the helper module as the fetch handler does, capped at 60 s."""
    return subprocess.run(
        [sys.executable, "-P", "-m", "invoicebot.fetchers.browser"],
        input=stdin,
        capture_output=True,
        timeout=60,
        check=False,
    )


def test_helper_reports_guard_blocked_for_disallowed_http_portal(tmp_path: Path) -> None:
    """R-5.10: an http portal without allow_http ends guard_blocked with exit 0."""
    completed = _run_helper(json.dumps(_request(tmp_path)).encode())

    assert completed.returncode == 0
    assert json.loads(completed.stdout) == {"reason": "guard_blocked", "files": []}


def test_helper_reports_technical_for_malformed_stdin() -> None:
    """R-9.1: stdin that is not a valid request prints technical and exits 0."""
    completed = _run_helper(b"not json")

    assert completed.returncode == 0
    assert json.loads(completed.stdout) == {"reason": "technical", "files": []}
