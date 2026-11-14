"""The ``invoicebot`` command line: ``portal-test`` (S10.5) and ``auth-microsoft`` (R-7.9).

``portal-test`` reads one sample invoice from a YAML file and runs the named portal through
the browser helper into a temporary folder. It prints the reason, each file's kind and
size, and whether the XML matches; it never prints an invoice field value or a file path
(R-9.16). ``auth-microsoft`` signs one account in with the device-code flow and then resumes
the invoices that were parked in ``PENDING_AUTH`` (BR-D01).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

import yaml
from pydantic import TypeAdapter, ValidationError

from invoicebot.auth.msal_cache import AuthRequired, TokenProvider
from invoicebot.config.loader import load_config, load_directory
from invoicebot.config.models import Config, MailerSink, StoreDirectory
from invoicebot.fetchers.base import FetchInvoice, FetchReason, FetchResult
from invoicebot.fetchers.portal import DEFAULT_HELPER_COMMAND, PortalFetcher
from invoicebot.pipeline.deliver import DELIVER_STEP
from invoicebot.queue.jobs import enqueue
from invoicebot.secrets import load_identity
from invoicebot.settings import Settings
from invoicebot.store.db import create_pool
from invoicebot.store.invoices import resume_pending_auth

_SAMPLE = TypeAdapter(FetchInvoice)
#: The verdict line for each reason that says something about the XML itself.
_XML_VERDICT = {FetchReason.OK: "yes", FetchReason.XML_MISMATCH: "no"}
#: The scopes every account signs in with: token refresh and OneDrive writes (R-7.9).
BASE_SCOPES = ("offline_access", "Files.ReadWrite")
#: The scope the invoice email needs; only a Graph mailer from the account adds it (R-7.9).
MAIL_SEND_SCOPE = "Mail.Send"
#: The mail mode that sends through Graph (R-7.6).
GRAPH_MODE = "graph"
#: Where each account's encrypted MSAL cache lives under ``data_dir`` (D85).
TOKENS_DIRECTORY = "tokens"

#: Builds the account's provider from settings, config, the account and its scopes (R-7.9).
type TokenProviderFactory = Callable[[Settings, Config, str, Sequence[str]], TokenProvider]


def _build_token_provider(
    settings: Settings,
    cfg: Config,
    account: str,
    scopes: Sequence[str],
) -> TokenProvider:
    """Build the real provider: client id from config, cache under ``data_dir/tokens`` (D85)."""
    if settings.age_identity_path is None:
        raise ValueError("AGE_IDENTITY_PATH must be set to sign in (R-7.9)")
    return TokenProvider(
        client_id=cfg.microsoft.client_id,
        account=account,
        scopes=scopes,
        cache_path=settings.data_dir / TOKENS_DIRECTORY / f"{account}.msal.age",
        identity=load_identity(settings.age_identity_path),
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    helper_command: Sequence[str] = DEFAULT_HELPER_COMMAND,
    token_provider_factory: TokenProviderFactory = _build_token_provider,
    settings: Settings | None = None,
) -> int:
    """Parse ``argv`` and run the chosen command, returning its exit status (S10.5)."""
    parser = _parser()
    args = parser.parse_args(argv)
    resolved = settings if settings is not None else Settings()
    cfg = load_config(resolved.config_path)
    if args.command == "auth-microsoft":
        return _auth_microsoft(parser, resolved, cfg, args.account, token_provider_factory)
    directory = load_directory(resolved.config_path.parent)
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
    auth = subparsers.add_parser(
        "auth-microsoft",
        help="sign a Microsoft account in and resume the invoices waiting for it",
    )
    auth.add_argument("--account", required=True, help="account name, e.g. personal")
    return parser


def _auth_microsoft(
    parser: argparse.ArgumentParser,
    settings: Settings,
    cfg: Config,
    account: str,
    provider_factory: TokenProviderFactory,
) -> int:
    """Sign the account in, then resume the invoices that waited for it (R-7.9, BR-D01).

    A failed sign-in exits 1 and resumes nothing, so no invoice leaves ``PENDING_AUTH``
    without a working token.
    """
    if settings.database_url is None:
        parser.error("DATABASE_URL must be set to resume waiting invoices")
    provider = provider_factory(settings, cfg, account, _scopes(cfg, account))
    try:
        provider.login_device_code(_show)
    except AuthRequired as error:
        sys.stderr.write(f"sign-in failed: {error}\n")
        return 1
    count = asyncio.run(_resume_pending_auth(settings.database_url))
    sys.stdout.write(f"resumed {count} invoice(s)\n")
    return 0


def _scopes(cfg: Config, account: str) -> tuple[str, ...]:
    """Return the scopes the account needs; add ``Mail.Send`` only for a Graph mailer (R-7.9)."""
    scopes = list(BASE_SCOPES)
    if any(
        isinstance(sink, MailerSink) and sink.account == account and GRAPH_MODE in sink.modes
        for flow in cfg.flows.values()
        for sink in flow.sinks
    ):
        scopes.append(MAIL_SEND_SCOPE)
    return tuple(scopes)


def _show(message: str) -> None:
    """Write the device-code instruction the provider built for the person (R-7.9)."""
    sys.stdout.write(message + "\n")


async def _resume_pending_auth(database_url: str) -> int:
    """Move every waiting invoice to ``DELIVERING`` with a deliver job, in one transaction (BR-D01)."""
    pool = create_pool(database_url)
    await pool.open(wait=True)
    invoice_ids: list[int] = []
    try:
        async with pool.connection() as conn, conn.transaction():
            invoice_ids = await resume_pending_auth(conn)
            for invoice_id in invoice_ids:
                await enqueue(conn, invoice_id, DELIVER_STEP)
    finally:
        await pool.close()
    return len(invoice_ids)


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
