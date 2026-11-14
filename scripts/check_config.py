#!/usr/bin/env python3
"""Validate the runtime config and the store directory (R-11.6).

Usage: ``check_config.py [path] [--pr [FILE ...]]``. The store directory is read
from the ``stores/`` and ``portals/`` folders next to the config file. Exits 1 on
a load/validation error or any blocking problem, 0 otherwise. On a pull request,
``--pr`` takes the files it changes: a todo/broken portal blocks only when its
own ``portals/<key>.yaml`` is one of them, so other portals' status never
freezes unrelated work.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from invoicebot.config.checks import run_checks
from invoicebot.config.loader import load_config, load_directory

DEFAULT_CONFIG_PATH = "config/example.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the invoice bot configuration.")
    parser.add_argument(
        "--pr",
        nargs="*",
        default=[],
        metavar="FILE",
        help="files the pull request changes; a todo/broken portal among them is blocking",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=DEFAULT_CONFIG_PATH,
        help=f"config file to validate (default: {DEFAULT_CONFIG_PATH})",
    )
    args = parser.parse_args(argv)

    config_path = Path(args.path)
    try:
        cfg = load_config(config_path)
        directory = load_directory(config_path.parent)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    problems = run_checks(cfg, directory, changed_portals=_portal_keys(args.pr))
    for problem in problems:
        print(f"{problem.code} {problem.path}: {problem.message}")
    return 1 if any(problem.blocking for problem in problems) else 0


def _portal_keys(changed_files: list[str]) -> frozenset[str]:
    """Return the portal keys of the changed files that are ``portals/<key>.yaml``."""
    paths = (Path(name) for name in changed_files)
    return frozenset(path.stem for path in paths if path.parent.name == "portals" and path.suffix == ".yaml")


if __name__ == "__main__":
    sys.exit(main())
