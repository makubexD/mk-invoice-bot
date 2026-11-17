"""The ``invoicebot`` command line: ``portal-test`` (S10.5), ``auth-microsoft`` (R-7.9),
``auth-google`` (R-6.4, R-6.5) and ``google-setup`` (R-6.2, D129).

``portal-test`` reads one sample invoice from a YAML file and runs the named portal through
the browser helper into a temporary folder. It prints the reason, each file's kind and
size, and whether the XML matches; it never prints an invoice field value or a file path
(R-9.16). ``auth-microsoft`` signs one account in with the device-code flow and then resumes
the invoices that were parked in ``PENDING_AUTH`` (BR-D01). ``auth-google`` runs the
installed-app sign-in with a loopback redirect and PKCE, stores the account's encrypted
refresh token and resumes the same waiting invoices (R-6.4, R-6.5, D128). ``google-setup``
creates the account's Drive folder and invoices spreadsheet with its header and prints both
ids for the private config (R-6.2, D129).
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx
import yaml
from pydantic import TypeAdapter, ValidationError

from invoicebot.auth.google_consent import ConsentError, consent_url, parse_redirect, pkce_pair
from invoicebot.auth.google_oauth import TOKEN_URL, GoogleTokens, save_refresh_token
from invoicebot.auth.msal_cache import AuthRequired, TokenProvider
from invoicebot.auth.providers import (
    DRIVE_FILE_SCOPE,
    build_token_provider,
    delivery_accounts,
    google_accounts,
    google_scopes_for,
    google_token_path,
    microsoft_accounts,
    scopes_for,
)
from invoicebot.config.loader import load_config, load_directory
from invoicebot.config.models import SHEETS_KEY_COLUMN, Config, Google, GoogleSheetsSink, StoreDirectory
from invoicebot.fetchers.base import FetchInvoice, FetchReason, FetchResult
from invoicebot.fetchers.portal import DEFAULT_HELPER_COMMAND, PortalFetcher
from invoicebot.pipeline.deliver import DELIVER_STEP
from invoicebot.queue.jobs import enqueue
from invoicebot.secrets import SecretStore, load_identity
from invoicebot.settings import Settings
from invoicebot.sinks.google_api import GoogleError
from invoicebot.sinks.google_drive import GoogleDrive
from invoicebot.sinks.google_sheets import GoogleSheets
from invoicebot.store.db import create_pool
from invoicebot.store.invoices import resume_pending_auth

_SAMPLE = TypeAdapter(FetchInvoice)
#: How long the Google sign-in waits for the browser's redirect (D128).
SIGN_IN_TIMEOUT_S = 300
#: The request-line cap of the one-request loopback listener (R-6.5).
_REQUEST_LINE_LIMIT = 8192
#: The page the browser shows once the redirect arrives (R-6.5).
_CALLBACK_BODY = b"Sign-in received; you can close this tab.\n"
_CALLBACK_HEADERS = (
    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nConnection: close\r\nContent-Length: "
)
_CALLBACK_RESPONSE = (
    _CALLBACK_HEADERS + str(len(_CALLBACK_BODY)).encode("ascii") + b"\r\n\r\n" + _CALLBACK_BODY
)
#: The random bytes behind the Google sign-in's ``state`` (R-6.5).
_STATE_BYTES = 32
#: The age-store secret the Google sign-in reads (D128).
_CLIENT_SECRET_KEY = "google_client_secret"  # noqa: S105  # the secret's name, not its value (D128)

#: The root folder ``google-setup`` creates for flow A's invoices (D129).
GOOGLE_SETUP_FOLDER_NAME = "Comprobantes"
#: The spreadsheet title ``google-setup`` gives the invoices sheet (D129).
GOOGLE_SETUP_SHEET_TITLE = "Comprobantes"


class _SignInError(Exception):
    """The token endpoint did not hand back a usable refresh token (R-6.4)."""


#: The verdict line for each reason that says something about the XML itself.
_XML_VERDICT = {FetchReason.OK: "yes", FetchReason.XML_MISMATCH: "no"}

#: Builds the account's provider from settings, config, the account and its scopes (R-7.9).
type TokenProviderFactory = Callable[[Settings, Config, str, Sequence[str]], TokenProvider]


def _show(message: str) -> None:
    """Write the sign-in URL or instruction the command built for the person (R-7.9)."""
    sys.stdout.write(message + "\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    helper_command: Sequence[str] = DEFAULT_HELPER_COMMAND,
    token_provider_factory: TokenProviderFactory = build_token_provider,
    settings: Settings | None = None,
    show: Callable[[str], None] = _show,
    google_transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Parse ``argv`` and run the chosen command, returning its exit status (S10.5)."""
    parser = _parser()
    args = parser.parse_args(argv)
    resolved = settings if settings is not None else Settings()
    cfg = load_config(resolved.config_path)
    if args.command == "auth-microsoft":
        return _auth_microsoft(parser, resolved, cfg, args.account, token_provider_factory)
    if args.command == "auth-google":
        return _auth_google(parser, resolved, cfg, args.account, show, google_transport)
    if args.command == "google-setup":
        return _google_setup(parser, resolved, cfg, args, google_transport)
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
    google = subparsers.add_parser(
        "auth-google",
        help="sign a Google account in and resume the invoices waiting for it",
    )
    google.add_argument("--account", required=True, help="account name, e.g. owner")
    setup = subparsers.add_parser(
        "google-setup",
        help="create the account's Drive folder and invoices spreadsheet",
    )
    setup.add_argument("--account", required=True, help="account name, e.g. owner")
    setup.add_argument(
        "--folder-name",
        default=GOOGLE_SETUP_FOLDER_NAME,
        help="Drive folder to create under the account's root",
    )
    setup.add_argument("--sheet-title", default=GOOGLE_SETUP_SHEET_TITLE, help="spreadsheet title")
    return parser


def _auth_microsoft(
    parser: argparse.ArgumentParser,
    settings: Settings,
    cfg: Config,
    account: str,
    provider_factory: TokenProviderFactory,
) -> int:
    """Sign the account in, then resume the invoices that waited for it (R-7.9, BR-D01).

    An account no flow uses exits 2 before signing in. Only a sign-in that delivers files
    or mail resumes the waiting invoices; a failed sign-in exits 1 and resumes nothing, so
    no invoice leaves ``PENDING_AUTH`` without a working token.
    """
    accounts = microsoft_accounts(cfg)
    if account not in accounts:
        configured = ", ".join(sorted(accounts)) or "none"
        parser.error(f"account {account!r} is not configured; configured accounts: {configured}")
    if settings.database_url is None:
        parser.error("DATABASE_URL must be set to resume waiting invoices")
    provider = provider_factory(settings, cfg, account, scopes_for(cfg, account))
    try:
        provider.login_device_code(_show)
    except AuthRequired as error:
        sys.stderr.write(f"sign-in failed: {error}\n")
        return 1
    if account not in delivery_accounts(cfg):
        sys.stdout.write("resumed 0 invoice(s)\n")
        return 0
    count = asyncio.run(_resume_pending_auth(settings.database_url))
    sys.stdout.write(f"resumed {count} invoice(s)\n")
    return 0


def _auth_google(
    parser: argparse.ArgumentParser,
    settings: Settings,
    cfg: Config,
    account: str,
    show: Callable[[str], None],
    transport: httpx.AsyncBaseTransport | None,
) -> int:
    """Sign the account in over the loopback flow, then resume its waiting invoices (R-6.4, BR-D01).

    An account no Google sink or ``gmail`` inbox names exits 2 before signing in. A failed
    sign-in exits 1, writes no token and resumes nothing, so no invoice leaves
    ``PENDING_AUTH`` without a working token.
    """
    accounts = google_accounts(cfg)
    if account not in accounts:
        configured = ", ".join(sorted(accounts)) or "none"
        parser.error(f"account {account!r} is not configured; configured accounts: {configured}")
    if settings.database_url is None:
        parser.error("DATABASE_URL must be set to resume waiting invoices")
    if settings.secrets_path is None or settings.age_identity_path is None:
        parser.error("SECRETS_PATH and AGE_IDENTITY_PATH must be set to sign in")
    client_secret = (
        SecretStore.load(settings.secrets_path, settings.age_identity_path)
        .scoped("auth_google", {_CLIENT_SECRET_KEY})
        .get(_CLIENT_SECRET_KEY)
    )
    try:
        asyncio.run(_google_sign_in(settings, cfg, account, client_secret, show, transport))
    except (ConsentError, TimeoutError, _SignInError, httpx.HTTPError) as error:
        sys.stderr.write(f"sign-in failed: {error}\n")
        return 1
    if DRIVE_FILE_SCOPE not in google_scopes_for(cfg, account):
        sys.stdout.write("resumed 0 invoice(s)\n")
        return 0
    count = asyncio.run(_resume_pending_auth(settings.database_url))
    sys.stdout.write(f"resumed {count} invoice(s)\n")
    return 0


def _sheets_sink_for(cfg: Config, account: str) -> GoogleSheetsSink | None:
    """Return the first flow's sheets sink that names ``account``, or None (D129)."""
    for flow in cfg.flows.values():
        for sink in flow.sinks:
            if isinstance(sink, GoogleSheetsSink) and sink.account == account:
                return sink
    return None


def _google_setup(
    parser: argparse.ArgumentParser,
    settings: Settings,
    cfg: Config,
    args: argparse.Namespace,
    transport: httpx.AsyncBaseTransport | None,
) -> int:
    """Create the account's Drive folder and spreadsheet and print both ids (R-6.2, D129).

    The header comes from the first flow whose sheets sink names the account. An account
    no flow's sheets sink names exits 2 before any request; the command writes no config
    file, so the ids it prints go into the private config by hand.
    """
    sink = _sheets_sink_for(cfg, args.account)
    if sink is None:
        parser.error(f"account {args.account!r} has no Google sheets sink in any flow")
    if cfg.google is None:
        parser.error("google settings must be configured to run google-setup (D128)")
    if settings.secrets_path is None or settings.age_identity_path is None:
        parser.error("SECRETS_PATH and AGE_IDENTITY_PATH must be set to create the sheet")
    client_secret = (
        SecretStore.load(settings.secrets_path, settings.age_identity_path)
        .scoped("google_oauth", {_CLIENT_SECRET_KEY})
        .get(_CLIENT_SECRET_KEY)
    )
    try:
        folder_id, spreadsheet_id = asyncio.run(
            _create_google_setup(settings, cfg, args, sink, client_secret, transport)
        )
    except AuthRequired:
        sys.stderr.write(f"sign-in needed: run invoicebot auth-google --account {args.account}\n")
        return 1
    except (GoogleError, httpx.HTTPError) as error:
        sys.stderr.write(f"google-setup failed: {error}\n")
        return 1
    sys.stdout.write(f"root_folder_id: {folder_id}\nspreadsheet_id: {spreadsheet_id}\n")
    return 0


async def _create_google_setup(
    settings: Settings,
    cfg: Config,
    args: argparse.Namespace,
    sink: GoogleSheetsSink,
    client_secret: str,
    transport: httpx.AsyncBaseTransport | None,
) -> tuple[str, str]:
    """Create the Drive folder and the sheet with its header; return both ids (R-6.2, D129).

    Raises:
        AuthRequired: the account has no usable refresh token.
        GoogleError: Google refuses a request or answers without a usable id.
        httpx.HTTPError: a request never reached Google.
    """
    identity_path = settings.age_identity_path
    if identity_path is None:
        raise _SignInError("AGE_IDENTITY_PATH must be set to create the sheet")
    google = _google_settings(cfg)
    async with httpx.AsyncClient(transport=transport, timeout=google.timeout_s) as http:
        tokens = GoogleTokens(
            http,
            google.client_id,
            client_secret,
            args.account,
            google_token_path(settings.data_dir, args.account),
            load_identity(identity_path),
            google.timeout_s,
        )
        drive = GoogleDrive(http, tokens, google.timeout_s, account=args.account)
        sheets = GoogleSheets(
            http,
            tokens,
            google.timeout_s,
            account=args.account,
            max_rows=google.max_rows,
        )
        folder_id = await drive.ensure_folder("root", args.folder_name)
        spreadsheet_id = await sheets.create(
            args.sheet_title,
            sink.tab,
            [SHEETS_KEY_COLUMN, *sink.columns],
        )
    return folder_id, spreadsheet_id


def _google_settings(cfg: Config) -> Google:
    """Return the Google OAuth client settings, or fail when config has none (D128).

    Raises:
        _SignInError: no ``google`` block is configured, so no client id or timeout exists.
    """
    if cfg.google is None:
        raise _SignInError("google settings must be configured to sign in (D128)")
    return cfg.google


async def _google_sign_in(
    settings: Settings,
    cfg: Config,
    account: str,
    client_secret: str,
    show: Callable[[str], None],
    transport: httpx.AsyncBaseTransport | None,
) -> None:
    """Run one loopback sign-in and save the account's refresh token (R-6.4, R-6.5, D128).

    Raises:
        ConsentError: the redirect's state does not match or carries no code.
        TimeoutError: no redirect arrives within ``SIGN_IN_TIMEOUT_S``.
        _SignInError: the token endpoint refuses the code or returns no refresh token.
    """
    identity_path = settings.age_identity_path
    if identity_path is None:
        raise _SignInError("AGE_IDENTITY_PATH must be set to sign in")
    google = _google_settings(cfg)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(_STATE_BYTES)
    redirect_path: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _answer_redirect(reader, writer, redirect_path)

    server = await asyncio.start_server(answer, "127.0.0.1", 0, limit=_REQUEST_LINE_LIMIT)
    async with server:
        sockets = server.sockets
        if not sockets:
            raise _SignInError("the sign-in listener did not bind")
        redirect_uri = f"http://127.0.0.1:{sockets[0].getsockname()[1]}"
        scopes = google_scopes_for(cfg, account)
        show(consent_url(google.client_id, redirect_uri, scopes, state, challenge))
        async with asyncio.timeout(SIGN_IN_TIMEOUT_S):
            path = await redirect_path
    code = parse_redirect(path, state)
    refresh_token = await _exchange_code(cfg, client_secret, code, verifier, redirect_uri, transport)
    save_refresh_token(
        google_token_path(settings.data_dir, account),
        load_identity(identity_path),
        refresh_token,
    )


async def _exchange_code(
    cfg: Config,
    client_secret: str,
    code: str,
    verifier: str,
    redirect_uri: str,
    transport: httpx.AsyncBaseTransport | None,
) -> str:
    """Swap the authorization code for a refresh token at Google's token endpoint (R-6.4).

    Raises:
        _SignInError: the endpoint answers non-200 or omits the refresh token.
        httpx.HTTPError: the request never reached Google.
    """
    google = _google_settings(cfg)
    async with httpx.AsyncClient(transport=transport, timeout=google.timeout_s) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "client_id": google.client_id,
                "client_secret": client_secret,
                "code": code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )
    if response.status_code != httpx.codes.OK:
        raise _SignInError(f"the token endpoint answered {response.status_code} (R-6.4)")
    return _refresh_token(response)


def _refresh_token(response: httpx.Response) -> str:
    """Return the refresh token in a token response, or say how to make Google issue one (R-6.4).

    Raises:
        _SignInError: the body is not a token response or carries no refresh token. The
            message never echoes a token or the body (R-9.9).
    """
    try:
        payload = response.json()
    except ValueError:
        payload = None
    refresh_token = payload.get("refresh_token") if isinstance(payload, dict) else None
    if not isinstance(refresh_token, str) or not refresh_token:
        raise _SignInError(
            "the token response has no refresh_token; remove this app's access at "
            "https://myaccount.google.com/permissions and sign in again"
        )
    return refresh_token


async def _answer_redirect(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    redirect_path: asyncio.Future[str],
) -> None:
    """Read one request line, answer the browser and hand the path to the sign-in (R-6.5).

    A connection that sends no ``GET`` line (a browser preconnect, a probe) is answered
    and ignored, so it cannot end the sign-in before the real redirect arrives.
    """
    try:
        line = await reader.readline()
    except (asyncio.LimitOverrunError, ValueError, asyncio.IncompleteReadError):
        line = b""
    path = _request_path(line)
    try:
        writer.write(_CALLBACK_RESPONSE)
        await writer.drain()
    finally:
        writer.close()
        if path and not redirect_path.done():
            redirect_path.set_result(path)
        await writer.wait_closed()


def _request_path(line: bytes) -> str:
    """Return the path of a ``GET <path> HTTP/1.1`` line, or "" for any other request (R-6.5)."""
    parts = line.decode("ascii", "replace").split()
    if len(parts) < 2 or parts[0] != "GET":
        return ""
    return parts[1]


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
