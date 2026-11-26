"""Tests for the browser session's browse, launch and helper options (D147)."""

from __future__ import annotations

import pytest

from invoicebot.config.models import Portal
from invoicebot.fetchers.browser.protocol import BadRequest, parse_helper_args
from invoicebot.fetchers.browser.session import IMAGES_OFF, LAUNCH_ARGS, launch_options, portal_options


def _portal(**fields: object) -> Portal:
    """A minimal synthetic portal, with ``fields`` overriding its defaults."""
    return Portal(key="synthetic", adapter="generic_form", **fields)


def test_portal_options_allow_the_one_http_host() -> None:
    """R-5.10, D193: an http portal's options hold exactly its one host."""
    portal = _portal(url="http://portal.test/form", allow_http=True)

    assert portal_options(portal).http_hosts == frozenset({"portal.test"})


def test_portal_options_allow_no_http_host_by_default() -> None:
    """R-5.10, D193: without allow_http no plain http host is reachable."""
    portal = _portal(url="http://portal.test/form")

    assert portal_options(portal).http_hosts == frozenset()


def test_portal_options_keep_images_off_without_a_captcha() -> None:
    """R-5.10, D193: a captcha-free portal loads no images."""
    portal = _portal(captcha="none")

    assert portal_options(portal).allow_images is False


def test_portal_options_load_images_for_a_captcha() -> None:
    """R-5.10, D193: a captcha portal loads images so the sender can read it."""
    portal = _portal(captcha="image")

    assert portal_options(portal).allow_images is True


@pytest.mark.parametrize("sandbox", [True, False])
def test_launch_options_pass_the_chromium_sandbox_flag(sandbox: bool) -> None:
    """D147: launch_options hands Chromium the requested sandbox setting."""
    options = launch_options(allow_images=False, proxy_port=8080, chromium_sandbox=sandbox)

    assert options["chromium_sandbox"] is sandbox


@pytest.mark.parametrize(("sandbox", "expected"), [(True, 0), (False, 1)])
def test_launch_options_drop_no_zygote_with_the_sandbox(sandbox: bool, expected: int) -> None:
    """D168: --no-zygote is absent with Chromium's sandbox on and present once with it off."""
    args = launch_options(allow_images=False, proxy_port=8080, chromium_sandbox=sandbox)["args"]

    assert args.count("--no-zygote") == expected


@pytest.mark.parametrize("sandbox", [True, False])
def test_launch_options_keep_the_other_launch_args(sandbox: bool) -> None:
    """D168: every other launch flag is present once whichever way the sandbox is set."""
    args = launch_options(allow_images=False, proxy_port=8080, chromium_sandbox=sandbox)["args"]

    for flag in LAUNCH_ARGS:
        assert args.count(flag) == 1


@pytest.mark.parametrize(("allow_images", "expected"), [(True, 0), (False, 1)])
def test_launch_options_disable_images_only_without_a_captcha(allow_images: bool, expected: int) -> None:
    """R-5.10, D193: IMAGES_OFF is in Chromium's flags exactly when images stay off."""
    args = launch_options(allow_images=allow_images, proxy_port=8080, chromium_sandbox=True)["args"]

    assert args.count(IMAGES_OFF) == expected


@pytest.mark.parametrize(("argv", "expected"), [([], False), (["--chromium-sandbox"], True)])
def test_parse_helper_args_reads_the_sandbox_flag(argv: list[str], expected: bool) -> None:
    """D147: no arguments means off; the sandbox flag alone means on."""
    assert parse_helper_args(argv) is expected


@pytest.mark.parametrize("argv", [["--x"], ["--chromium-sandbox", "x"]])
def test_parse_helper_args_rejects_anything_else(argv: list[str]) -> None:
    """D147: an unknown argument or a value after the flag is a bad request."""
    with pytest.raises(BadRequest):
        parse_helper_args(argv)
