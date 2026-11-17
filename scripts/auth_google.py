#!/usr/bin/env python3
"""Sign a Google account in and resume the invoices waiting for it (R-6.4, R-6.5, BR-D01).

Usage: ``auth_google.py --account owner``. The name D128's admin text gives the
admin; it only forwards its arguments to ``invoicebot auth-google``.
"""

from __future__ import annotations

import sys

from invoicebot.cli import main as cli_main


def main() -> int:
    """Run the ``auth-google`` command with this script's arguments."""
    return cli_main(["auth-google", *sys.argv[1:]])


if __name__ == "__main__":
    sys.exit(main())
