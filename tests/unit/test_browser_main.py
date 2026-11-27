"""Tests for the portal helper entry's failure line (D169)."""

from __future__ import annotations

import json
from collections.abc import Coroutine
from types import SimpleNamespace
from typing import Any

import pytest

from invoicebot.fetchers.browser import __main__ as browser_main
from invoicebot.fetchers.browser.protocol import BadRequest


def _run_raising_value_error(coro: Coroutine[Any, Any, None]) -> None:
    """Close the coroutine and fail the way a page error would (D169)."""
    coro.close()
    raise ValueError("secret page text")


def test_main_prints_the_failure_class_without_page_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D169: a top-level failure prints technical with its class and no page text."""
    monkeypatch.setattr(browser_main, "parse_helper_args", lambda argv: False)
    monkeypatch.setattr(browser_main, "_read_request", lambda: SimpleNamespace(max_bytes=object()))
    monkeypatch.setattr(browser_main, "limit_resources", lambda max_bytes: None)
    monkeypatch.setattr(browser_main, "asyncio", SimpleNamespace(run=_run_raising_value_error))

    assert browser_main.main() == 0

    printed = capsys.readouterr().out
    assert json.loads(printed) == {
        "reason": "technical",
        "files": [],
        "error_class": "ValueError",
        "captcha": None,
    }
    assert "secret page text" not in printed


def test_main_prints_no_error_class_for_a_bad_request(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D169: a bad request keeps the technical line with a null error class."""
    monkeypatch.setattr(browser_main, "parse_helper_args", lambda argv: False)

    def _raise_bad_request() -> object:
        raise BadRequest

    monkeypatch.setattr(browser_main, "_read_request", _raise_bad_request)

    assert browser_main.main() == 0

    assert json.loads(capsys.readouterr().out) == {
        "reason": "technical",
        "files": [],
        "error_class": None,
        "captcha": None,
    }
