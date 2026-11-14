#!/usr/bin/env python3
"""Sign a Microsoft account in and resume the invoices waiting for it (R-7.9, BR-D01).

Usage: ``auth_microsoft.py --account personal``. The name BR-D01's admin text gives
the admin; it only forwards its arguments to ``invoicebot auth-microsoft``.
"""

from __future__ import annotations

import sys

from invoicebot.cli import main as cli_main


def main() -> int:
    """Run the ``auth-microsoft`` command with this script's arguments."""
    return cli_main(["auth-microsoft", *sys.argv[1:]])


if __name__ == "__main__":
    sys.exit(main())
