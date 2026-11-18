#!/usr/bin/env python3
"""Import SUNAT's padrón ZIP into the database (R-2.5, S12.4, D123).

Usage: ``import_padron.py <zip>`` or ``import_padron.py --download``. The first form
imports the ZIP the owner downloaded by hand (``padron_reducido_ruc.zip``); the second
lets the app's own refresh download it from ``sunat.padron.url`` under the same size cap.
The import replaces the whole padrón and prints only the row and skipped counts, never a
RUC or a name.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

from invoicebot.channels.whatsapp.client import REQUEST_TIMEOUT_SECONDS
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config
from invoicebot.net.guard import SystemResolver
from invoicebot.net.http import GuardedTransport
from invoicebot.ops.padron_refresh import PadronRefreshError, refresh_padron
from invoicebot.padron import ImportResult, import_padron
from invoicebot.settings import Settings
from invoicebot.store.db import create_pool


def main(argv: list[str] | None = None) -> int:
    """Import a local ``<zip>`` or download the padrón and import it (R-2.5, S12.4)."""
    parser = _parser()
    args = parser.parse_args(argv)
    if (args.zip_path is None) != args.download:
        parser.error("give exactly one of <zip> or --download")
    if args.zip_path is not None and not args.zip_path.is_file():
        print(f"error: not a file: {args.zip_path}", file=sys.stderr)
        return 1
    settings = Settings()
    try:
        cfg = load_config(settings.config_path)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    database_url = settings.database_url
    if database_url is None:
        print("error: DATABASE_URL must be set", file=sys.stderr)
        return 1
    try:
        if args.download:
            result = asyncio.run(_download_and_import(database_url, cfg, settings.data_dir))
        else:
            result = asyncio.run(_import(database_url, args.zip_path))
    except PadronRefreshError as error:
        print(f"error: padron refresh failed: {error.reason}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"rows: {result.rows} skipped: {result.skipped}")
    return 0


def _parser() -> argparse.ArgumentParser:
    """Return the parser for the padrón import (R-2.5, S12.4)."""
    parser = argparse.ArgumentParser(description="Import SUNAT's padrón ZIP.")
    parser.add_argument("zip_path", type=Path, nargs="?", help="the downloaded padrón ZIP")
    parser.add_argument(
        "--download",
        action="store_true",
        help="download the padrón from sunat.padron.url before importing it",
    )
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


async def _download_and_import(database_url: str, cfg: Config, data_dir: Path) -> ImportResult:
    """Download the padrón with a guarded client and import it over a short-lived pool (S12.4)."""
    async with httpx.AsyncClient(
        transport=GuardedTransport(
            httpx.AsyncHTTPTransport(limits=httpx.Limits(max_keepalive_connections=0)),
            SystemResolver(),
        ),
        follow_redirects=False,
        timeout=REQUEST_TIMEOUT_SECONDS,
    ) as http:
        pool = create_pool(database_url)
        await pool.open(wait=True)
        try:
            return await refresh_padron(pool, http, cfg.sunat.padron, data_dir)
        finally:
            await pool.close()


if __name__ == "__main__":
    sys.exit(main())
