#!/usr/bin/env python3
"""Import SUNAT's padrón ZIP into the database (R-2.5, D123).

Usage: ``import_padron.py <zip>``. The ZIP is the one the owner downloaded by hand
(``padron_reducido_ruc.zip``); nothing here downloads it. The import replaces the
whole padrón and prints only the row and skipped counts, never a RUC or a name.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from invoicebot.config.loader import load_config
from invoicebot.padron import ImportResult, import_padron
from invoicebot.settings import Settings
from invoicebot.store.db import create_pool


def main(argv: list[str] | None = None) -> int:
    """Import ``<zip>`` and print the row and skipped counts (R-2.5)."""
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.zip_path.is_file():
        print(f"error: not a file: {args.zip_path}", file=sys.stderr)
        return 1
    settings = Settings()
    try:
        load_config(settings.config_path)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if settings.database_url is None:
        print("error: DATABASE_URL must be set", file=sys.stderr)
        return 1
    try:
        result = asyncio.run(_import(settings.database_url, args.zip_path))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"rows: {result.rows} skipped: {result.skipped}")
    return 0


def _parser() -> argparse.ArgumentParser:
    """Return the parser for the padrón import (R-2.5)."""
    parser = argparse.ArgumentParser(description="Import SUNAT's padrón ZIP.")
    parser.add_argument("zip_path", type=Path, help="the downloaded padrón ZIP")
    return parser


async def _import(database_url: str, zip_path: Path) -> ImportResult:
    """Run the import over a short-lived pool, in autocommit for the COPY (R-2.5)."""
    pool = create_pool(database_url)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            await conn.set_autocommit(True)
            return await import_padron(conn, zip_path)
    finally:
        await pool.close()


if __name__ == "__main__":
    sys.exit(main())
