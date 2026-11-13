"""Integration tests for the browser helper process and its route guard (S10.1, S7.6)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from invoicebot.fetchers.browser.routing import RouteGuard
from invoicebot.net.guard import GuardReason


class _FakeRequest:
    """The two request attributes the guard reads."""

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


class _FakeResolver:
    """A resolver with fixed answers per host."""

    def __init__(self, answers: dict[str, list[str]]) -> None:
        self._answers = answers

    async def resolve(self, host: str) -> list[str]:
        """Return the canned addresses for ``host``."""
        return self._answers.get(host, [])


async def test_route_guard_blocks_loopback_http_as_private_address() -> None:
    """R-5.10: a request to a loopback host is aborted with private_address."""
    guard = RouteGuard(
        frozenset({"127.0.0.1"}),
        _FakeResolver({"127.0.0.1": ["127.0.0.1"]}),
        allow_images=True,
    )
    route = _FakeRoute("http://127.0.0.1/", "document")

    await guard.on_route(route)

    assert route.aborted_with == "blockedbyclient"
    assert guard.first_block is GuardReason.PRIVATE_ADDRESS


async def test_route_guard_blocks_private_https_address() -> None:
    """R-5.10: an https host resolving to a private address is aborted as private_address."""
    guard = RouteGuard(
        frozenset(),
        _FakeResolver({"internal.test": ["10.0.0.1"]}),
        allow_images=False,
    )
    route = _FakeRoute("https://internal.test/", "document")

    await guard.on_route(route)

    assert route.aborted_with == "blockedbyclient"
    assert guard.first_block is GuardReason.PRIVATE_ADDRESS


async def test_route_guard_blocks_file_scheme() -> None:
    """R-5.10: a file: URL is aborted as scheme_blocked."""
    guard = RouteGuard(frozenset(), _FakeResolver({}), allow_images=False)
    route = _FakeRoute("file:///etc/passwd", "document")

    await guard.on_route(route)

    assert route.aborted_with == "blockedbyclient"
    assert guard.first_block is GuardReason.SCHEME_BLOCKED


async def test_route_guard_aborts_font_without_recording_a_reason() -> None:
    """R-5.10: a font request is dropped without counting as a guard block."""
    guard = RouteGuard(frozenset(), _FakeResolver({}), allow_images=False)
    route = _FakeRoute("https://cdn.example/font.woff2", "font")

    await guard.on_route(route)

    assert route.aborted_with == "blockedbyclient"
    assert guard.first_block is None


async def test_route_guard_continues_a_public_https_request() -> None:
    """R-5.10: a public https request is allowed through."""
    guard = RouteGuard(
        frozenset(),
        _FakeResolver({"example.com": ["93.184.216.34"]}),
        allow_images=False,
    )
    route = _FakeRoute("https://example.com/", "document")

    await guard.on_route(route)

    assert route.continued
    assert route.aborted_with is None
    assert guard.first_block is None


def _request(out_dir: Path) -> dict[str, Any]:
    """One synthetic helper request for a portal on a loopback http URL."""
    return {
        "portal": {
            "key": "synthetic",
            "adapter": "generic_form",
            "url": "http://127.0.0.1:1/",
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
