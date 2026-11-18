"""Unit tests for the portal fetcher's browser job (R-9.2)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from invoicebot.fetchers.base import FetchReason
from invoicebot.fetchers.browser.protocol import HelperOutput
from invoicebot.fetchers.portal import run_browser_job


async def test_run_browser_job_passes_the_fetch_folder_to_the_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R-9.2: the helper's env carries the fetch folder as HELPER_OUT_DIR (D150)."""
    seen: dict[str, str] = {}

    async def fake_run_helper(
        _argv: Sequence[str],
        _stdin: bytes,
        env: Mapping[str, str],
        _timeout_s: float,
        _max_output_bytes: int,
    ) -> bytes:
        seen.update(env)
        return HelperOutput(reason=FetchReason.NOT_FOUND_YET).model_dump_json().encode()

    monkeypatch.setattr("invoicebot.fetchers.portal.run_helper", fake_run_helper)
    out_dir = tmp_path / "out"

    result = await run_browser_job(("helper",), b"{}", 1.0, "portal", out_dir)

    assert isinstance(result, HelperOutput)
    assert seen["HELPER_OUT_DIR"] == str(out_dir)
