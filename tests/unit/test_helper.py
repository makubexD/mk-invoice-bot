"""Unit tests for the helper subprocess runner (R-9.2)."""

from __future__ import annotations

import sys

import pytest

from invoicebot.helper import run_helper

#: Several times a pipe buffer, so the write blocks once the helper has gone.
LARGE_INPUT = b"x" * (4 * 1024 * 1024)
#: The exit code the early-exiting helper returns.
EARLY_EXIT_CODE = 3


async def test_helper_exiting_before_reading_its_input_raises_its_exit_code() -> None:
    """R-9.2: a helper that exits unread surfaces its exit code, not a broken pipe."""
    argv = [sys.executable, "-c", f"import sys; sys.exit({EARLY_EXIT_CODE})"]

    with pytest.raises(RuntimeError, match=f"exited with code {EARLY_EXIT_CODE}"):
        await run_helper(argv, LARGE_INPUT, {}, timeout_s=10, max_output_bytes=1024)
