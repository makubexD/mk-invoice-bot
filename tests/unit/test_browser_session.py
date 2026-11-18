"""Tests for the browser session's launch options and helper arguments (D147)."""

from __future__ import annotations

import pytest

from invoicebot.config.models import Portal
from invoicebot.fetchers.browser.protocol import BadRequest, parse_helper_args
from invoicebot.fetchers.browser.session import launch_options


def _portal() -> Portal:
    """A minimal portal; only the launch flags depend on it here."""
    return Portal(key="synthetic", adapter="generic_form")


@pytest.mark.parametrize("sandbox", [True, False])
def test_launch_options_pass_the_chromium_sandbox_flag(sandbox: bool) -> None:
    """D147: launch_options hands Chromium the requested sandbox setting."""
    options = launch_options(_portal(), 8080, chromium_sandbox=sandbox)

    assert options["chromium_sandbox"] is sandbox


@pytest.mark.parametrize(("argv", "expected"), [([], False), (["--chromium-sandbox"], True)])
def test_parse_helper_args_reads_the_sandbox_flag(argv: list[str], expected: bool) -> None:
    """D147: no arguments means off; the sandbox flag alone means on."""
    assert parse_helper_args(argv) is expected


@pytest.mark.parametrize("argv", [["--x"], ["--chromium-sandbox", "x"]])
def test_parse_helper_args_rejects_anything_else(argv: list[str]) -> None:
    """D147: an unknown argument or a value after the flag is a bad request."""
    with pytest.raises(BadRequest):
        parse_helper_args(argv)
