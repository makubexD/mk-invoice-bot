"""Tests for the SOL helper entry (R-5.2, R-9.10, R-9.16)."""

from __future__ import annotations

import io
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from invoicebot.config.models import MediaMaxBytes, Portal, SunatSolSite
from invoicebot.fetchers.base import FetchInvoice, FetchReason, FetchResult
from invoicebot.fetchers.browser.sol import SolCredentialsIn, SolRequest, main, run_sol

_CONFIG = Path(__file__).resolve().parents[2] / "config" / "sunat_sol.yaml"
RUC = "20000000001"
USER = "USUARIO1"
HIDDEN_TEXT = "synthetic-secret-pass"
PAGE_TIMEOUT_MS = 5000
ONE_MEGABYTE = 1024 * 1024


class FakePage:
    """A page that records calls and lands on ``url_after_goto`` after each navigation."""

    def __init__(self, url_after_goto: str) -> None:
        self.url = ""
        self._url_after_goto = url_after_goto
        self.calls: list[tuple[str, ...]] = []

    async def goto(self, url: str) -> None:
        """Record the navigation and land on the configured URL."""
        self.calls.append(("goto", url))
        self.url = self._url_after_goto

    async def fill(self, selector: str, value: str) -> None:
        """Record one filled field."""
        self.calls.append(("fill", selector, value))

    async def click(self, selector: str) -> None:
        """Record one click."""
        self.calls.append(("click", selector))

    async def wait_for_load_state(self) -> None:
        """Record the post-submit wait."""
        self.calls.append(("wait",))


class FakeFill:
    """A form filler that records each call and returns a fixed result."""

    def __init__(self, result: FetchResult) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []

    async def __call__(
        self,
        page: FakePage,
        portal: Portal,
        invoice: FetchInvoice,
        out_dir: Path,
        max_bytes: MediaMaxBytes,
    ) -> FetchResult:
        """Record the call and return the fixed result."""
        self.calls.append((page, portal, invoice, out_dir, max_bytes))
        return self.result


def _site_document() -> dict[str, object]:
    """The SOL site file's YAML, as the bot writes it."""
    return yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))


def _site() -> SunatSolSite:
    """The SOL site the tests run against, loaded from ``config/sunat_sol.yaml``."""
    return SunatSolSite.model_validate(_site_document())


def _request(out_dir: Path) -> SolRequest:
    """One synthetic SOL request with no real credential or invoice value."""
    return SolRequest(
        site=_site(),
        invoice=FetchInvoice(
            ruc_emisor=RUC,
            tipo="01",
            serie="F001",
            numero="123",
            fecha=date(2026, 10, 1),
            total=Decimal("118.00"),
        ),
        out_dir=out_dir,
        max_bytes=MediaMaxBytes(image=ONE_MEGABYTE, pdf=ONE_MEGABYTE, xml=ONE_MEGABYTE, zip=ONE_MEGABYTE),
        page_timeout_ms=PAGE_TIMEOUT_MS,
        credentials=SolCredentialsIn(ruc=RUC, user=USER, password=HIDDEN_TEXT),
    )


def _payload(out_dir: Path) -> dict[str, object]:
    """A request as the bot would send it, with a field the model must refuse."""
    return {
        "site": _site_document(),
        "invoice": {
            "ruc_emisor": RUC,
            "tipo": "01",
            "serie": "F001",
            "numero": "123",
            "fecha": "2026-10-01",
            "total": "118.00",
        },
        "out_dir": str(out_dir),
        "max_bytes": {
            "image": ONE_MEGABYTE,
            "pdf": ONE_MEGABYTE,
            "xml": ONE_MEGABYTE,
            "zip": ONE_MEGABYTE,
        },
        "page_timeout_ms": PAGE_TIMEOUT_MS,
        "credentials": {"ruc": RUC, "user": USER, "password": HIDDEN_TEXT},
        "unexpected": "rejected",
    }


async def test_a_sunat_sign_in_runs_the_query_field_filler(tmp_path: Path) -> None:
    """R-5.2: after a SUNAT sign-in the query page is filled for the invoice."""
    request = _request(tmp_path)
    page = FakePage(request.site.login.url)
    expected = FetchResult(reason=FetchReason.OK, files=(), portal_key=request.site.query.key)
    fill = FakeFill(expected)

    result = await run_sol(page, request, fill=fill)

    assert result is expected
    assert page.calls == [
        ("goto", request.site.login.url),
        ("fill", request.site.login.ruc, RUC),
        ("fill", request.site.login.user, USER),
        ("fill", request.site.login.password, HIDDEN_TEXT),
        ("click", request.site.login.submit),
        ("wait",),
    ]
    assert fill.calls == [(page, request.site.query, request.invoice, request.out_dir, request.max_bytes)]


async def test_a_redirect_off_sunat_returns_technical_without_filling(tmp_path: Path) -> None:
    """R-9.10: a page that leaves the SUNAT origin gets no credential and no query."""
    request = _request(tmp_path)
    page = FakePage("https://evil.test/login")
    fill = FakeFill(FetchResult(reason=FetchReason.OK, files=(), portal_key="sunat_sol"))

    result = await run_sol(page, request, fill=fill)

    assert result.reason is FetchReason.TECHNICAL
    assert result.files == ()
    assert result.portal_key == request.site.query.key
    assert page.calls == [("goto", request.site.login.url)]
    assert fill.calls == []


def test_main_never_prints_a_password_from_a_bad_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R-9.16: a malformed request prints technical and neither stream shows the password."""
    raw = json.dumps(_payload(tmp_path)).encode()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw)))

    assert main() == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"reason": "technical", "files": []}
    assert HIDDEN_TEXT not in captured.out
    assert HIDDEN_TEXT not in captured.err


def test_a_sol_request_repr_hides_the_password(tmp_path: Path) -> None:
    """R-9.16: the request's repr holds no credential text."""
    request = _request(tmp_path)

    assert HIDDEN_TEXT not in repr(request)
