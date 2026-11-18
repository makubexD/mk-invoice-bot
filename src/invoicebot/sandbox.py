"""Build the `unshare` prefixes that isolate the helpers in the image (D147)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

#: Runs inside the first namespace: an empty `/proc`, the hidden dirs masked, no network.
IMAGE_SCRIPT = """\
set -e
echo 1000 > /proc/self/oom_score_adj
mount -t tmpfs -o size=4k,mode=0555 none /proc
while [ "$1" != "--" ]; do mount -t tmpfs -o size=4k,mode=0555 none "$1"; shift; done
shift
exec "$@"
"""

#: Runs inside the first namespace: a capped `$TMPDIR`, the hidden dirs masked, the network kept.
BROWSER_SCRIPT = """\
set -e
echo 1000 > /proc/self/oom_score_adj
mount -t tmpfs -o "size=$1m,mode=0700" none "$TMPDIR"
shift
while [ "$1" != "--" ]; do mount -t tmpfs -o size=4k,mode=0555 none "$1"; shift; done
shift
exec unshare --user --map-user=1000 --map-group=1000 "$@"
"""


def image_prefix(hidden_dirs: Sequence[Path]) -> tuple[str, ...]:
    """Return the argv prefix that sandboxes the extract helper (D147)."""
    return (
        "unshare",
        "--user",
        "--map-root-user",
        "--mount",
        "--net",
        "--pid",
        "--fork",
        "--kill-child",
        "sh",
        "-c",
        IMAGE_SCRIPT,
        "sandbox",
        *map(str, hidden_dirs),
        "--",
    )


def browser_prefix(hidden_dirs: Sequence[Path], tmp_mb: int) -> tuple[str, ...]:
    """Return the argv prefix that sandboxes a browser helper (D147)."""
    if tmp_mb < 1:
        raise ValueError("tmp_mb must be at least 1")
    return (
        "unshare",
        "--user",
        "--map-root-user",
        "--mount",
        "--pid",
        "--fork",
        "--kill-child",
        "sh",
        "-c",
        BROWSER_SCRIPT,
        "sandbox",
        str(tmp_mb),
        *map(str, hidden_dirs),
        "--",
    )


def hidden_dirs(paths: Iterable[Path | None]) -> tuple[Path, ...]:
    """Return the sorted, de-duplicated parents of the given paths, never `/` (D147)."""
    parents = {path.parent for path in paths if path is not None}
    return tuple(sorted(parent for parent in parents if parent != Path("/")))
