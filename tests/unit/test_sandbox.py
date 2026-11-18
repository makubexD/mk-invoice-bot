"""Tests for the helper sandbox prefixes (D147)."""

from __future__ import annotations

from pathlib import Path

import pytest

from invoicebot.sandbox import (
    BROWSER_SCRIPT,
    IMAGE_SCRIPT,
    HelperPrefixes,
    SandboxUnavailable,
    browser_command,
    browser_prefix,
    check_sandbox,
    helper_prefixes,
    hidden_dirs,
    image_prefix,
    passfile_path,
)


def test_image_prefix_hides_dirs_and_drops_the_network() -> None:
    """D147: the extract helper gets user, mount, net and pid namespaces."""
    prefix = image_prefix([Path("/a"), Path("/b")])

    assert prefix[-4:] == ("sandbox", "/a", "/b", "--")
    assert "--net" in prefix


def test_browser_prefix_keeps_the_network_and_caps_tmp() -> None:
    """D147: the browser helper keeps the network and gets its tmpfs size."""
    prefix = browser_prefix([Path("/a")], 256)

    assert "--net" not in prefix
    assert prefix[-4:] == ("sandbox", "256", "/a", "--")


def test_browser_prefix_rejects_a_zero_tmp_size() -> None:
    """D147: a tmpfs cap below one megabyte is invalid."""
    with pytest.raises(ValueError, match="tmp_mb"):
        browser_prefix([], 0)


def test_hidden_dirs_are_sorted_deduplicated_parents() -> None:
    """D147: the runtime files are hidden by mounting over their parent directories."""
    paths = [Path("/x/c.yaml"), None, Path("/x/s.age"), Path("/y/p")]

    assert hidden_dirs(paths) == (Path("/x"), Path("/y"))


def test_hidden_dirs_never_returns_root() -> None:
    """D147: a file directly under `/` must not hide the whole filesystem."""
    assert hidden_dirs([Path("/top")]) == ()


@pytest.mark.parametrize("script", [IMAGE_SCRIPT, BROWSER_SCRIPT])
def test_script_sets_oom_score_before_mounting(script: str) -> None:
    """D147: the helper is killed before the bot under memory pressure."""
    assert script.startswith("set -e")
    assert script.index("oom_score_adj") < script.index("mount")


def test_helper_prefixes_off_leaves_the_commands_bare() -> None:
    """D147: with the sandbox off the helpers run as they always did."""
    prefixes = helper_prefixes(False, [Path("/c")], 256)

    assert prefixes == HelperPrefixes((), ())
    assert browser_command(prefixes, ("py", "-m", "x")) == ("py", "-m", "x")


def test_helper_prefixes_on_builds_both_prefixes() -> None:
    """D147: the image helper loses the network and the browser helper asks for Chromium's sandbox."""
    prefixes = helper_prefixes(True, [Path("/c")], 256)

    assert "--net" in prefixes.image
    assert "--net" not in prefixes.browser
    assert browser_command(prefixes, ("py", "-m", "x"))[-4:] == ("py", "-m", "x", "--chromium-sandbox")


async def test_check_sandbox_without_prefixes_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """D147: a host with the sandbox off skips the startup check."""

    async def boom(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("run_helper must not run")

    monkeypatch.setattr("invoicebot.sandbox.run_helper", boom)

    await check_sandbox(HelperPrefixes((), ()), 0.1)


async def test_check_sandbox_raises_when_a_wrapper_fails() -> None:
    """D147: a host that cannot start the wrapper refuses to run the app."""
    with pytest.raises(SandboxUnavailable):
        await check_sandbox(HelperPrefixes(image=("false",), browser=()), 5.0)


def test_passfile_path_reads_the_query_key() -> None:
    """D148: the database URL's passfile names the file the connection reads."""
    assert passfile_path("postgresql://u@h/db?passfile=/run/x/pgpass") == Path("/run/x/pgpass")


def test_passfile_path_repeated_key_gives_the_last_value() -> None:
    """D148: a repeated passfile gives the last value, the one libpq and the dump read."""
    url = "postgresql://u@h/db?passfile=/a/decoy&passfile=/run/x/pgpass"

    assert passfile_path(url) == Path("/run/x/pgpass")


@pytest.mark.parametrize(
    "url",
    [None, "postgresql://u@h/db", "postgresql://u@h/db?sslmode=require"],
)
def test_passfile_path_absent_gives_none(url: str | None) -> None:
    """D148: without a passfile key the URL gives no path."""
    assert passfile_path(url) is None
