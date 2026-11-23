#!/usr/bin/env python3
"""Register the owner's existing sent, unpaid claim folders (R-7.16, S11.17, D110).

Usage: ``import_claims.py --profile <id> [--dry-run] <folder>…``. Each folder given is
the local copy of the flow's ``onedrive_folder`` sink root (the synced OneDrive on the
laptop, or a copy on the host), so no Graph sign-in is needed. It reads each folder's
name and its XML, registers the claims as DONE flow B invoices and never writes to those
folders. ``--dry-run`` prints what it would register.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from invoicebot.channels.base import ChannelName
from invoicebot.config.loader import load_config
from invoicebot.importer import (
    SKIP_EXISTS,
    Claim,
    ImportEntry,
    ImportReport,
    ImportTarget,
    Skipped,
    folder_sink_root,
    import_flow,
    import_origin_channel,
    read_claims,
    register_claims,
)
from invoicebot.settings import Settings
from invoicebot.store.db import create_pool


def main(argv: list[str] | None = None) -> int:
    """Read the folders, register their claims and print one line per folder (R-7.16)."""
    parser = _parser()
    args = parser.parse_args(argv)
    missing = [folder for folder in args.folders if not folder.is_dir()]
    if missing:
        print(f"error: not a folder: {', '.join(map(str, missing))}", file=sys.stderr)
        return 1
    settings = Settings()
    try:
        cfg = load_config(settings.config_path)
        flow_id = import_flow(cfg, args.profile)
        origin_channel = import_origin_channel(cfg)
        drive_root = folder_sink_root(cfg, flow_id)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if settings.database_url is None:
        print("error: DATABASE_URL must be set", file=sys.stderr)
        return 1

    readings = read_claims(args.folders)
    claims = [reading.result for reading in readings if isinstance(reading.result, Claim)]
    target = ImportTarget(flow_id, drive_root)
    report = asyncio.run(
        _register(settings.database_url, args.profile, target, claims, origin_channel, args.dry_run)
    )
    entries = {entry.claim.folder_path: entry for entry in report.entries}
    for reading in readings:
        if isinstance(reading.result, Skipped):
            print(f"skipped {reading.path}: {reading.result.reason}")
            continue
        print(_line(reading.path, entries[reading.result.folder_path]))
    return 0


def _parser() -> argparse.ArgumentParser:
    """Return the parser for the claim import (R-7.16)."""
    parser = argparse.ArgumentParser(description="Register sent, unpaid claim folders.")
    parser.add_argument("--profile", required=True, help="profile id the claims belong to")
    parser.add_argument("--dry-run", action="store_true", help="print what would be registered")
    parser.add_argument("folders", nargs="+", type=Path, help="folder holding the claim folders")
    return parser


async def _register(
    database_url: str,
    profile_id: str,
    target: ImportTarget,
    claims: list[Claim],
    origin_channel: ChannelName,
    dry_run: bool,
) -> ImportReport:
    """Register the claims over a short-lived pool, one transaction per claim (R-7.16)."""
    pool = create_pool(database_url)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            return await register_claims(
                conn, profile_id, target, claims, origin_channel=origin_channel, dry_run=dry_run
            )
    finally:
        await pool.close()


def _line(path: Path, entry: ImportEntry) -> str:
    """Return the line the import prints for one claim folder (R-7.16)."""
    if entry.status == "skipped":
        return f"skipped {path}: {SKIP_EXISTS}"
    if entry.status == "would_register":
        return f"would register {path}"
    return f"registered {path} as invoice {entry.invoice_id}"


if __name__ == "__main__":
    sys.exit(main())
