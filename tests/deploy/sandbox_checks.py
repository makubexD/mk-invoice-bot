"""Run the helper sandbox checks inside the app image (D147).

Every check runs the real argv prefix from :mod:`invoicebot.sandbox` and asserts what the
namespace wrapper isolates: no network, an empty ``/proc``, hidden config, a hard OOM score,
a capped ``TMPDIR``, Chromium's own sandbox, no orphaned browser and a working extract
helper. The script prints one ``ok <name>`` or ``FAIL <name>: <why>`` line per check and
exits non-zero when any of them failed; one broken check never stops the rest.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from invoicebot.config.loader import load_config
from invoicebot.pipeline.extract import PIXELS_PER_MEGAPIXEL, helper_argv
from invoicebot.sandbox import helper_prefixes, hidden_dirs

#: The dir the container mounts the throwaway config into; the prefix hides this parent.
RUNTIME_CONFIG = Path("/app/runtime-config")
#: The browser helper's capped scratch mount (D147).
TMP_MB = 64
#: Where this script and its read-only fixtures are mounted in the container.
CHECKS = Path(__file__).resolve().parent
CONFIG = CHECKS / "config"
GOLDEN = CHECKS / "golden"
#: A check must never hang the suite; the extract helper's own limit is 50 s.
SHORT_TIMEOUT_S = 20
BROWSER_TIMEOUT_S = 60
HELPER_TIMEOUT_S = 60


class CheckFailed(Exception):
    """A check ran but did not observe the isolation it asserts."""


#: The config dir is hidden the same way app.py hides it: from the marker file's parent.
PREFIXES = helper_prefixes(True, hidden_dirs([RUNTIME_CONFIG / "marker"]), TMP_MB)


def _base_env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}


def _browser_env(tmpdir: str) -> dict[str, str]:
    """Return the minimal browser env: PATH, the scratch mount and Playwright's browser dir."""
    return {
        **_base_env(),
        # Playwright's Node driver resolves the home dir at start (and Chromium writes there).
        "HOME": os.environ.get("HOME", "/home/bot"),
        "TMPDIR": tmpdir,
        # Playwright reads this at import time; the image sets it as ENV, but env= here replaces it.
        "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/ms-playwright"),
    }


def _image_env() -> dict[str, str]:
    """Return the env :func:`invoicebot.pipeline.extract._run_helper` gives the helper."""
    config = load_config(CONFIG / "example.yaml")
    return {
        **_base_env(),
        "OPENCV_IO_MAX_IMAGE_PIXELS": str(config.limits.media.max_megapixels * PIXELS_PER_MEGAPIXEL),
        "OMP_THREAD_LIMIT": "1",
    }


def _run(argv: Sequence[str], env: Mapping[str, str], timeout_s: float) -> subprocess.CompletedProcess[str]:
    """Run a fixed argv with a timeout, capturing its output."""
    return subprocess.run(  # noqa: S603 - fixed argv built here, never a shell
        list(argv),
        capture_output=True,
        check=False,
        env=dict(env),
        text=True,
        timeout=timeout_s,
    )


def _comm(pid: int) -> str:
    """Return a live process's comm, or an empty string when it is gone."""
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _tree(root_pid: int) -> list[int]:
    """Return the root pid and every live descendant in the container's pid namespace."""
    found = [root_pid]
    index = 0
    while index < len(found):
        pid = found[index]
        index += 1
        try:
            children = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="utf-8").split()
        except OSError:
            continue
        found.extend(int(child) for child in children)
    return found


def _all_comms() -> list[str]:
    """Return the comm of every process visible in the container."""
    return [_comm(int(entry.name)) for entry in Path("/proc").glob("[0-9]*")]


def _wait_until(predicate: Callable[[], bool], timeout_s: float) -> bool:
    """Poll ``predicate`` until it holds or ``timeout_s`` passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def check_image_no_network() -> None:
    """The image prefix has no route out, so a TCP connect fails with ENETUNREACH."""
    snippet = "import socket; socket.create_connection(('1.1.1.1', 80), timeout=5)"
    done = _run([*PREFIXES.image, sys.executable, "-P", "-c", snippet], _base_env(), SHORT_TIMEOUT_S)
    if done.returncode == 0:
        raise CheckFailed("the connect succeeded")
    if "Network is unreachable" not in done.stderr:
        raise CheckFailed(f"no ENETUNREACH in stderr: {done.stderr.strip()!r}")


def check_image_no_proc() -> None:
    """The image prefix mounts an empty tmpfs over ``/proc``."""
    done = _run([*PREFIXES.image, "ls", "-A", "/proc"], _base_env(), SHORT_TIMEOUT_S)
    if done.returncode != 0:
        raise CheckFailed(f"ls exited {done.returncode}: {done.stderr.strip()!r}")
    if done.stdout.strip():
        raise CheckFailed(f"/proc is visible: {done.stdout.strip()!r}")


def check_image_config_hidden() -> None:
    """The image prefix mounts an empty tmpfs over the runtime-config dir."""
    done = _run([*PREFIXES.image, "ls", "-A", str(RUNTIME_CONFIG)], _base_env(), SHORT_TIMEOUT_S)
    if done.returncode != 0:
        raise CheckFailed(f"ls exited {done.returncode}: {done.stderr.strip()!r}")
    if done.stdout.strip():
        raise CheckFailed(f"{RUNTIME_CONFIG} is visible: {done.stdout.strip()!r}")


def check_image_oom() -> None:
    """The image prefix scores its helper 1000, so the kernel kills it before the app."""
    process = subprocess.Popen(  # noqa: S603 - fixed argv built here, never a shell
        [*PREFIXES.image, "sleep", "5"],
        env=_base_env(),
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )
    try:
        time.sleep(1)

        def _scored_sleep() -> bool:
            return any(
                _comm(pid) == "sleep"
                and Path(f"/proc/{pid}/oom_score_adj").read_text(encoding="utf-8").strip() == "1000"
                for pid in _tree(process.pid)
            )

        if not _wait_until(_scored_sleep, SHORT_TIMEOUT_S):
            raise CheckFailed("no sandboxed sleep with oom_score_adj 1000")
    finally:
        process.kill()
        process.wait(timeout=SHORT_TIMEOUT_S)


def check_browser_config_hidden_oom() -> None:
    """The browser prefix hides the config and scores its helper 1000 in one run."""
    with tempfile.TemporaryDirectory() as tmpdir:
        script = f"ls -A {RUNTIME_CONFIG}; cat /proc/self/oom_score_adj"
        done = _run([*PREFIXES.browser, "sh", "-c", script], _browser_env(tmpdir), SHORT_TIMEOUT_S)
    if done.returncode != 0:
        raise CheckFailed(f"sh exited {done.returncode}: {done.stderr.strip()!r}")
    if done.stdout.split() != ["1000"]:
        raise CheckFailed(f"expected only 1000, got {done.stdout.strip()!r}")


def check_browser_tmp_cap() -> None:
    """The browser prefix's ``$TMPDIR`` is a tmpfs that refuses more than 64 MiB."""
    with tempfile.TemporaryDirectory() as tmpdir:
        script = 'head -c 80M /dev/zero > "$TMPDIR/big"'
        done = _run([*PREFIXES.browser, "sh", "-c", script], _browser_env(tmpdir), SHORT_TIMEOUT_S)
    if done.returncode == 0:
        raise CheckFailed("writing 80 MiB to $TMPDIR succeeded")


def check_chromium_sandbox() -> None:
    """Chromium starts with its own sandbox inside the browser prefix and renders text."""
    snippet = (
        "from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as playwright:\n"
        "    browser = playwright.chromium.launch(chromium_sandbox=True)\n"
        "    page = browser.new_page()\n"
        "    page.set_content('<p>x</p>')\n"
        "    print(page.inner_text('p'))\n"
        "    browser.close()\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        done = _run(
            [*PREFIXES.browser, sys.executable, "-P", "-c", snippet],
            _browser_env(tmpdir),
            BROWSER_TIMEOUT_S,
        )
    if done.returncode != 0:
        raise CheckFailed(f"chromium exited {done.returncode}: {done.stderr.strip()!r}")
    if done.stdout.strip() != "x":
        raise CheckFailed(f"expected x, got {done.stdout.strip()!r}")


def check_no_orphan_chromium() -> None:
    """Killing the browser helper's process group leaves no Chromium process behind."""
    snippet = (
        "import time\n"
        "from playwright.sync_api import sync_playwright\n"
        "playwright = sync_playwright().start()\n"
        "playwright.chromium.launch(chromium_sandbox=True)\n"
        "time.sleep(60)\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        process = subprocess.Popen(  # noqa: S603 - fixed argv built here, never a shell
            [*PREFIXES.browser, sys.executable, "-P", "-c", snippet],
            env=_browser_env(tmpdir),
            start_new_session=True,
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
        try:
            if not _wait_until(lambda: any("chrome" in comm for comm in _all_comms()), BROWSER_TIMEOUT_S):
                raise CheckFailed("no chromium process started")
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=SHORT_TIMEOUT_S)
            time.sleep(2)
            if any("chrome" in comm for comm in _all_comms()):
                raise CheckFailed("a chromium process outlived its process group")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=SHORT_TIMEOUT_S)


def check_extract_helper() -> None:
    """The image prefix reads the golden set's first image and prints one JSON line."""
    config = load_config(CONFIG / "example.yaml")
    image = sorted(GOLDEN.glob("*.json"))[0].with_suffix(".jpg")
    argv = helper_argv(
        str(image), "photo", config.extraction, config.limits.media, CONFIG / "parsing_rules.yaml"
    )
    done = _run([*PREFIXES.image, *argv], _image_env(), HELPER_TIMEOUT_S)
    if done.returncode != 0:
        raise CheckFailed(f"helper exited {done.returncode}: {done.stderr.strip()!r}")
    try:
        json.loads(done.stdout)
    except json.JSONDecodeError:
        raise CheckFailed(f"stdout is not JSON: {done.stdout.strip()!r}") from None


CHECKS_BY_NAME: dict[str, Callable[[], None]] = {
    "image-no-network": check_image_no_network,
    "image-no-proc": check_image_no_proc,
    "image-config-hidden": check_image_config_hidden,
    "image-oom": check_image_oom,
    "browser-config-hidden-oom": check_browser_config_hidden_oom,
    "browser-tmp-cap": check_browser_tmp_cap,
    "chromium-sandbox": check_chromium_sandbox,
    "no-orphan-chromium": check_no_orphan_chromium,
    "extract-helper": check_extract_helper,
}


def main() -> int:
    """Run every check, print one result line each, and return the exit code."""
    failures = 0
    for name, check in CHECKS_BY_NAME.items():
        try:
            check()
        except Exception as err:  # every check reports on its own; one must not hide the rest
            failures += 1
            print(f"FAIL {name}: {err}", file=sys.stderr)  # noqa: T201 - the test reads this line
        else:
            print(f"ok {name}")  # noqa: T201 - the test reads this line
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
