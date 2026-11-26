"""Tests for the portal probe helper entry (D193, R-9.16)."""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from invoicebot.fetchers.browser import probe
from invoicebot.fetchers.browser.probe import guarded_report, main
from invoicebot.fetchers.browser.probe_protocol import ProbeOutcome, ProbeReport
from invoicebot.fetchers.browser.session import MAX_REQUEST_BYTES
from invoicebot.net.guard import GuardReason

TECHNICAL_LINE = {"outcome": "technical", "error_class": None}


def _payload(out_dir: Path) -> dict[str, object]:
    """A synthetic probe request."""
    return {
        "url": "https://portal.example/",
        "invoice": {
            "ruc_emisor": "20123456789",
            "tipo": "01",
            "serie": "F001",
            "numero": "123",
            "fecha": "2026-10-01",
            "total": "118.00",
        },
        "out_dir": str(out_dir),
        "max_bytes": {"image": 1024, "pdf": 1024, "xml": 1024, "zip": 1024},
        "page_timeout_ms": 5000,
    }


def _feed(monkeypatch: pytest.MonkeyPatch, raw: bytes, argv: list[str] | None = None) -> None:
    """Make *raw* the helper's stdin and *argv* its arguments."""
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw)))
    monkeypatch.setattr(sys, "argv", ["probe", *(argv or [])])


def _check_technical(capsys: pytest.CaptureFixture[str]) -> None:
    """Assert the one stdout line is a bare technical report."""
    out = json.loads(capsys.readouterr().out)
    assert {key: out[key] for key in TECHNICAL_LINE} == TECHNICAL_LINE
    assert out["fields"] == []


def test_bad_json_is_technical(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """D193, R-9.16: stdin that is not JSON prints technical and exits 0."""
    _feed(monkeypatch, b"not json")

    assert main() == 0
    _check_technical(capsys)


def test_oversized_stdin_is_technical(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D193, R-9.16: stdin over the cap prints technical and exits 0."""
    _feed(monkeypatch, b" " * (MAX_REQUEST_BYTES + 1))

    assert main() == 0
    _check_technical(capsys)


def test_a_bad_argument_is_technical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D193, R-9.16: an unknown argument prints technical and exits 0."""
    _feed(monkeypatch, json.dumps(_payload(tmp_path)).encode(), ["--bogus"])

    assert main() == 0
    _check_technical(capsys)


def _run_raising(coro: Coroutine[Any, Any, None]) -> None:
    """Close the coroutine and fail the way a page error would."""
    coro.close()
    raise ValueError("secret page text")


def test_an_exception_prints_only_its_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D193, R-9.16: a failure shows its class name and no page text on either stream."""
    _feed(monkeypatch, json.dumps(_payload(tmp_path)).encode())
    monkeypatch.setattr(probe, "limit_resources", lambda max_bytes: None)
    monkeypatch.setattr(probe, "asyncio", SimpleNamespace(run=_run_raising))

    assert main() == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["error_class"] == "ValueError"
    assert json.loads(captured.out)["outcome"] == "technical"
    assert "secret page text" not in captured.out
    assert "secret page text" not in captured.err


@pytest.mark.parametrize("outcome", [ProbeOutcome.TECHNICAL, ProbeOutcome.BUSY, ProbeOutcome.NO_DOWNLOAD])
def test_a_block_promotes_three_outcomes(outcome: ProbeOutcome) -> None:
    """D193, R-9.16: a block turns technical, busy and no_download into guard_blocked."""
    promoted = guarded_report(ProbeReport(outcome=outcome), GuardReason.PRIVATE_ADDRESS)

    assert promoted.outcome is ProbeOutcome.GUARD_BLOCKED


def test_a_block_keeps_a_captcha_and_no_block_keeps_all() -> None:
    """D193, R-9.16: a captcha stays, and without a block nothing changes."""
    captcha = ProbeReport(outcome=ProbeOutcome.CAPTCHA, captcha="image")
    technical = ProbeReport(outcome=ProbeOutcome.TECHNICAL)

    assert guarded_report(captcha, GuardReason.PRIVATE_ADDRESS) is captcha
    assert guarded_report(technical, None) is technical
