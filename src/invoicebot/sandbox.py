"""Build the `unshare` prefixes that isolate the helpers in the image (D147)."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from invoicebot.fetchers.browser.protocol import CHROMIUM_SANDBOX_FLAG
from invoicebot.helper import run_helper

#: The path the sandbox checks run with when the environment names none.
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"
#: A sandbox check prints nothing; anything larger is a broken wrapper, not output (R-9.2).
CHECK_OUTPUT_BYTES = 4 * 1024

#: Runs inside the first namespace: an empty `/proc`, the hidden dirs masked, no network.
#: The helper runs with no capabilities, so it cannot unmount a mask. This shell stays
#: PID 1 of the namespace instead of exec'ing: a helper that leaves the process group
#: still dies with it, because the kernel kills the whole namespace when PID 1 exits.
#: `<&0` keeps stdin, which a background command would otherwise read from /dev/null.
IMAGE_SCRIPT = """\
set -e
echo 1000 > /proc/self/oom_score_adj
mount -t tmpfs -o size=4k,mode=0555 none /proc
while [ "$1" != "--" ]; do mount -t tmpfs -o size=4k,mode=0555 none "$1"; shift; done
shift
setpriv --bounding-set=-all --inh-caps=-all --no-new-privs -- "$@" <&0 &
wait $!
"""

#: Runs inside the first namespace: a capped `$TMPDIR`, the hidden dirs masked, the network kept.
#: The helper runs as an unprivileged user in a nested namespace, and this shell stays PID 1,
#: for the same reasons as in `IMAGE_SCRIPT`.
BROWSER_SCRIPT = """\
set -e
echo 1000 > /proc/self/oom_score_adj
mount -t tmpfs -o "size=$1m,mode=0700" none "$TMPDIR"
shift
while [ "$1" != "--" ]; do mount -t tmpfs -o size=4k,mode=0555 none "$1"; shift; done
shift
unshare --user --map-user=1000 --map-group=1000 "$@" <&0 &
wait $!
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


@dataclass(frozen=True)
class HelperPrefixes:
    """The argv prefixes that sandbox each helper kind; both empty when the sandbox is off (D147)."""

    image: tuple[str, ...]
    browser: tuple[str, ...]


class SandboxUnavailable(RuntimeError):
    """The host cannot run a sandboxed helper, so the app must refuse to start (D147)."""


def helper_prefixes(enabled: bool, hidden: Sequence[Path], tmp_mb: int) -> HelperPrefixes:
    """Return the prefixes for the enabled sandbox, or empty ones when it is off (D147)."""
    if not enabled:
        return HelperPrefixes((), ())
    return HelperPrefixes(image_prefix(hidden), browser_prefix(hidden, tmp_mb))


def browser_command(prefixes: HelperPrefixes, base: Sequence[str]) -> tuple[str, ...]:
    """Return a browser helper's argv, asking for Chromium's sandbox when it is sandboxed (D147)."""
    if not prefixes.browser:
        return tuple(base)
    return (*prefixes.browser, *base, CHROMIUM_SANDBOX_FLAG)


def passfile_path(database_url: str | None) -> Path | None:
    """Return the ``passfile`` the database URL names, or None when it names none (D148).

    A repeated key gives its last value, the one libpq and the backup dump use.
    """
    if database_url is None:
        return None
    values = parse_qs(urlsplit(database_url).query).get("passfile")
    return Path(values[-1]) if values else None


async def check_sandbox(prefixes: HelperPrefixes, timeout_s: float) -> None:
    """Run each configured prefix once, refusing a host that cannot start it (D147).

    Raises:
        SandboxUnavailable: when a wrapper fails to run or outlives ``timeout_s``.
    """
    path = os.environ.get("PATH", DEFAULT_PATH)
    try:
        if prefixes.image:
            await _run_check([*prefixes.image, "true"], {"PATH": path}, timeout_s)
        if prefixes.browser:
            with tempfile.TemporaryDirectory() as tmpdir:
                await _run_check([*prefixes.browser, "true"], {"PATH": path, "TMPDIR": tmpdir}, timeout_s)
    except (RuntimeError, TimeoutError) as err:
        raise SandboxUnavailable(f"sandbox check failed: {err}") from err


async def _run_check(argv: Sequence[str], env: dict[str, str], timeout_s: float) -> None:
    """Run one sandbox check command, raising what ``run_helper`` raises."""
    await run_helper(argv, stdin=b"", env=env, timeout_s=timeout_s, max_output_bytes=CHECK_OUTPUT_BYTES)
