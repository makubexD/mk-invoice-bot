#!/usr/bin/env python3
"""Validate the runtime config and the portal registry (R-11.6).

Usage: ``check_config.py [--pr] [path]``. The portal registry is read from
``portals.yaml`` next to the config file. Exits 1 on a load/validation error or
any blocking problem, 0 otherwise.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from invoicebot.config.checks import run_checks
from invoicebot.config.loader import load_config, load_portals

DEFAULT_CONFIG_PATH = "config/example.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the invoice bot configuration.")
    parser.add_argument(
        "--pr",
        action="store_true",
        help="treat todo/broken portals as blocking (pull requests only)",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=DEFAULT_CONFIG_PATH,
        help=f"config file to validate (default: {DEFAULT_CONFIG_PATH})",
    )
    args = parser.parse_args(argv)

    config_path = Path(args.path)
    portals_path = config_path.parent / "portals.yaml"
    try:
        cfg = load_config(config_path)
        portals = load_portals(portals_path)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    problems = run_checks(cfg, portals, pr_mode=args.pr)
    for problem in problems:
        print(f"{problem.code} {problem.path}: {problem.message}")
    return 1 if any(problem.blocking for problem in problems) else 0


if __name__ == "__main__":
    sys.exit(main())
