"""The ``invoicebot`` command line: today only ``portal-test`` (S10.5).

The command reads one sample invoice from a YAML file and runs the named portal through
the browser helper into a temporary folder. It prints the reason, each file's kind and
size, and whether the XML matches; it never prints an invoice field value or a file path
(R-9.16).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

import yaml
from pydantic import TypeAdapter, ValidationError

from invoicebot.config.loader import load_config, load_directory
from invoicebot.config.models import Config, StoreDirectory
from invoicebot.fetchers.base import FetchInvoice, FetchReason, FetchResult
from invoicebot.fetchers.portal import DEFAULT_HELPER_COMMAND, PortalFetcher
from invoicebot.settings import Settings

_SAMPLE = TypeAdapter(FetchInvoice)
#: The verdict line for each reason that says something about the XML itself.
_XML_VERDICT = {FetchReason.OK: "yes", FetchReason.XML_MISMATCH: "no"}


def main(
    argv: Sequence[str] | None = None,
    *,
    helper_command: Sequence[str] = DEFAULT_HELPER_COMMAND,
) -> int:
    """Parse ``argv`` and run the chosen command, returning its exit status (S10.5)."""
    parser = _parser()
    args = parser.parse_args(argv)
    settings = Settings()
    cfg = load_config(settings.config_path)
    directory = load_directory(settings.config_path.parent)
    return _portal_test(parser, directory, cfg, args.key, args.sample, helper_command)


def run() -> None:
    """Console-script entry point: exit with the command's status (S10.5)."""
    sys.exit(main())


def _parser() -> argparse.ArgumentParser:
    """Return the parser for ``invoicebot`` and its subcommands (S10.5)."""
    parser = argparse.ArgumentParser(prog="invoicebot")
    subparsers = parser.add_subparsers(dest="command", required=True)
    portal_test = subparsers.add_parser("portal-test", help="run one portal fetch on a sample invoice")
    portal_test.add_argument("key", help="portal key, its file name under portals/")
    portal_test.add_argument("--sample", required=True, type=Path, help="YAML file with the invoice fields")
    return parser


def _portal_test(
    parser: argparse.ArgumentParser,
    directory: StoreDirectory,
    cfg: Config,
    key: str,
    sample_path: Path,
    helper_command: Sequence[str],
) -> int:
    """Run the named portal on one sample invoice and print the outcome (S10.5)."""
    if key not in directory.portals:
        parser.error(f"unknown portal key {key!r}")
    invoice = _load_sample(parser, sample_path)
    store = directory.stores.get(invoice.ruc_emisor)
    if store is None:
        parser.error("the sample's ruc_emisor has no store file")
    test_directory = directory.model_copy(
        update={
            "portals": {
                **directory.portals,
                key: directory.portals[key].model_copy(update={"status": "active"}),
            },
            "stores": {
                **directory.stores,
                invoice.ruc_emisor: store.model_copy(update={"portal": key}),
            },
        }
    )
    fetcher = PortalFetcher(
        test_directory,
        cfg.limits.media.max_bytes,
        cfg.retrieval.portal_timeout_s,
        helper_command=helper_command,
    )
    with tempfile.TemporaryDirectory() as tmp_dir:
        result = asyncio.run(fetcher.fetch(invoice, Path(tmp_dir)))
    _print_result(result)
    return 0 if result.ok else 1


def _load_sample(parser: argparse.ArgumentParser, path: Path) -> FetchInvoice:
    """Read the sample YAML into ``FetchInvoice``; an error names fields, never values."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        parser.error("the sample file is not readable YAML")
    if not isinstance(document, dict):
        parser.error("the sample must be a YAML mapping of invoice fields")
    try:
        return _SAMPLE.validate_python(document)
    except ValidationError as error:
        parser.error(f"invalid sample: {_error_fields(error)}")


def _error_fields(error: ValidationError) -> str:
    """Name the fields pydantic rejected, never their values (S10.5)."""
    names = sorted({str(part) for item in error.errors() for part in item["loc"]})
    return ", ".join(names) or "unknown fields"


def _print_result(result: FetchResult) -> None:
    """Write the reason, file sizes and XML verdict, never a value or path (R-9.16)."""
    lines = [f"reason: {result.reason.value}"]
    lines.extend(f"{file.kind} {file.size} bytes" for file in result.files)
    lines.append(f"xml matches: {_XML_VERDICT.get(result.reason, 'n/a')}")
    sys.stdout.write("\n".join(lines) + "\n")
