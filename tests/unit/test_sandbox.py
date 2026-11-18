"""Tests for the helper sandbox prefixes (D147)."""

from __future__ import annotations

from pathlib import Path

import pytest

from invoicebot.sandbox import (
    BROWSER_SCRIPT,
    IMAGE_SCRIPT,
    browser_prefix,
    hidden_dirs,
    image_prefix,
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
